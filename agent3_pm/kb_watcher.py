"""
Knowledge Base watcher — GitHub webhook triggered.

When Jarvis pushes new files to the KB repo, GitHub sends a webhook to
/webhook/github → we parse tasks/bugs from new files → notify Level 1.

No polling. Instant. Reliable.
"""
import hashlib
import hmac
import json
import logging
import os

import aiohttp
from agent3_pm.database import AsyncSessionLocal
from agent3_pm import repository as repo
from agent3_pm.models import LEVEL_1_POSITIONS
from agent3_pm.task_agent import _get_client
from agent3_pm.config import config

logger = logging.getLogger(__name__)

KB_REPO = "Deci1337/mai-knowledge-base"
KB_WATCHED_FOLDERS = {"user/calls", "user/bugs", "calls/tasks"}
KB_RAW = f"https://raw.githubusercontent.com/{KB_REPO}/main"

_GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
_WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET", "")

# TODO: временно только Амир для тестов
_TEST_ONLY_IDS: set[int] | None = {1086780711}


def _gh_headers() -> dict:
    h = {"Accept": "application/vnd.github.v3+json"}
    if _GITHUB_TOKEN:
        h["Authorization"] = f"token {_GITHUB_TOKEN}"
    return h


# Pending approval batches (in memory — lost on restart, but webhooks re-trigger)
_approval_batches: dict[str, dict] = {}
_batch_counter = 0


PARSE_PROMPT_CALLS = """Из текста созвона/записи извлеки ВСЕ задачи и поручения которые были упомянуты.
Это могут быть как обычные задачи, так и баги.

Для каждой задачи верни JSON-объект:
{
  "title": "краткое название",
  "description": "подробности — что нужно сделать, кому, когда, KPI",
  "assignee_name": "кому назначено (если упомянуто)" или null,
  "priority": 0-3 (0=срочно, 2=обычно),
  "is_bug": true если это баг/ошибка/поломка, false если обычная задача,
  "due_date": "YYYY-MM-DD" или null,
  "project_name": null,
  "status": null
}

Верни JSON объект с ключом "tasks": {"tasks": [задача1, задача2, ...]}
Если задач нет — {"tasks": []}.
Извлекай ТОЛЬКО конкретные задачи/поручения, не общие обсуждения.
НЕ придумывай задач — только из текста."""

PARSE_PROMPT_BUGS = """Из текста извлеки ВСЕ баги и ошибки.

Для каждого бага верни JSON-объект:
{
  "title": "краткое название бага",
  "description": "что сломалось, как воспроизвести",
  "assignee_name": null,
  "priority": 0-1 (0=критичный, 1=важный),
  "is_bug": true,
  "due_date": null,
  "project_name": null,
  "status": null
}

Верни JSON объект с ключом "tasks": {"tasks": [баг1, баг2, ...]}
Если багов нет — {"tasks": []}.
Извлекай ТОЛЬКО баги/ошибки, не обычные задачи."""


async def _fetch_file_content(path: str) -> str:
    """Download raw file content from GitHub."""
    try:
        async with aiohttp.ClientSession() as http:
            async with http.get(f"{KB_RAW}/{path}", headers=_gh_headers()) as resp:
                if resp.status != 200:
                    logger.warning(f"Failed to fetch {path}: {resp.status}")
                    return ""
                return await resp.text()
    except Exception:
        logger.exception(f"Failed to fetch file {path}")
        return ""


async def _parse_tasks_from_content(content: str, filename: str,
                                    prompt: str = PARSE_PROMPT_CALLS) -> list[dict]:
    """Use GPT to extract tasks from file content."""
    try:
        client = _get_client()
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": f"Файл: {filename}\n\n{content[:6000]}"},
            ],
            temperature=0,
            max_tokens=2000,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content.strip()
        result = json.loads(raw)
        if isinstance(result, dict):
            for key in ("tasks", "bugs", "items", "data"):
                if key in result and isinstance(result[key], list):
                    return result[key]
            return []
        return result if isinstance(result, list) else []
    except Exception:
        logger.exception(f"Failed to parse tasks from {filename}")
        return []


def _extract_source_info(content: str, filename: str) -> dict:
    """Extract metadata from file frontmatter."""
    info = {"filename": filename, "date": "", "author": "", "author_name": ""}
    for line in content.split("\n")[:15]:
        line = line.strip()
        if line.startswith("date:"):
            info["date"] = line.split(":", 1)[1].strip()
        elif line.startswith("author_name:"):
            info["author_name"] = line.split(":", 1)[1].strip().strip('"')
        elif line.startswith("author:"):
            info["author"] = line.split(":", 1)[1].strip()
        elif "Сотрудник:" in line:
            info["author_name"] = line.split("Сотрудник:")[1].strip().split("(")[0].strip()
    return info


def verify_github_signature(payload: bytes, signature: str) -> bool:
    """Verify GitHub webhook signature (HMAC-SHA256)."""
    if not _WEBHOOK_SECRET:
        return True  # no secret configured — accept all (dev mode)
    expected = "sha256=" + hmac.new(
        _WEBHOOK_SECRET.encode(), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


async def handle_webhook_push(payload: dict, bot) -> dict:
    """Process GitHub push webhook — extract new files from watched folders,
    parse tasks/bugs, notify Level 1.

    Returns summary dict for HTTP response.
    """
    global _batch_counter

    commits = payload.get("commits", [])
    if not commits:
        return {"status": "no_commits"}

    # Collect all new/modified files from watched folders
    new_files: list[dict] = []  # [{"path": ..., "folder": ...}]
    seen_paths = set()

    for commit in commits:
        for filepath in commit.get("added", []) + commit.get("modified", []):
            if filepath in seen_paths:
                continue
            seen_paths.add(filepath)

            parts = filepath.rsplit("/", 1)
            if len(parts) != 2:
                continue
            folder, filename = parts

            if folder not in KB_WATCHED_FOLDERS:
                continue
            if filename.startswith("."):
                continue

            new_files.append({"path": filepath, "folder": folder, "filename": filename})

    if not new_files:
        return {"status": "no_watched_files"}

    logger.info(f"Webhook: {len(new_files)} new files in watched folders")

    # Parse each file
    all_tasks = []
    source_info = None

    for f in new_files:
        content = await _fetch_file_content(f["path"])
        if not content or len(content.strip()) < 20:
            logger.info(f"Skipping empty/short file {f['path']}")
            continue

        info = _extract_source_info(content, f["filename"])
        if not source_info:
            source_info = info

        prompt = PARSE_PROMPT_BUGS if "bugs" in f["folder"] else PARSE_PROMPT_CALLS
        tasks = await _parse_tasks_from_content(content, f["filename"], prompt)
        if not tasks:
            logger.info(f"No tasks found in {f['path']}")
            continue

        if "bugs" in f["folder"]:
            for t in tasks:
                t["is_bug"] = True
                if t.get("priority", 2) > 1:
                    t["priority"] = 1

        file_url = f"https://github.com/{KB_REPO}/blob/main/{f['path']}"
        for t in tasks:
            t["_source_url"] = file_url

        all_tasks.extend(tasks)
        logger.info(f"Parsed {len(tasks)} tasks from {f['path']}")

    if not all_tasks:
        return {"status": "no_tasks_found", "files_checked": len(new_files)}

    # Create batch and notify
    _batch_counter += 1
    batch_id = f"kb_{_batch_counter}"
    _approval_batches[batch_id] = {
        "locked_by": None,
        "tasks": all_tasks,
        "current_idx": 0,
        "source_info": source_info or {"filename": "", "date": "", "author": "", "author_name": ""},
        "file_url": "",
        "folder": "webhook",
    }

    await _notify_top1_new_batch(bot, batch_id, all_tasks, source_info or {})
    logger.info(f"Webhook batch {batch_id}: {len(all_tasks)} tasks from {len(new_files)} files")

    return {"status": "ok", "batch_id": batch_id, "tasks_count": len(all_tasks)}


async def _notify_top1_new_batch(bot, batch_id: str, tasks: list[dict], source: dict):
    """Send notification to Level 1 users about new tasks batch."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    author = source.get("author_name") or source.get("author") or "неизвестно"
    date = source.get("date") or "—"

    lines = [f"Новые задачи из базы знаний ({date})"]
    lines.append(f"Источник: {author}\n")
    for i, t in enumerate(tasks, 1):
        bug = " [Баг]" if t.get("is_bug") else ""
        lines.append(f"{i}. {t.get('title', '—')}{bug}")

    text = "\n".join(lines)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Взять на утверждение", callback_data=f"approve_take_{batch_id}")]
    ])

    async with AsyncSessionLocal() as session:
        all_users = await repo.get_all_users(session)
        top1 = [u for u in all_users if u.position in LEVEL_1_POSITIONS and u.telegram_id]
        if _TEST_ONLY_IDS is not None:
            top1 = [u for u in top1 if u.telegram_id in _TEST_ONLY_IDS]

    for user in top1:
        try:
            await bot.send_message(chat_id=user.telegram_id, text=text, reply_markup=kb)
            logger.info(f"Notified {user.name} about batch {batch_id}")
        except Exception:
            logger.exception(f"Failed to notify {user.name} about batch {batch_id}")


def get_batch(batch_id: str) -> dict | None:
    return _approval_batches.get(batch_id)


def remove_batch(batch_id: str):
    _approval_batches.pop(batch_id, None)
