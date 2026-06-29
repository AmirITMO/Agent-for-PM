"""
Knowledge Base watcher — polls GitHub commits API every 30 seconds.

Tracks last processed commit timestamp + SHA set to avoid duplicates.
Timestamp updated ONLY after successful processing of ALL files.
Also supports webhook as alternative trigger (/webhook/github).
"""
import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timedelta, timezone

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
KB_COMMITS_API = f"https://api.github.com/repos/{KB_REPO}/commits"

_GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
_WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET", "")

# TODO: временно только Амир для тестов
_TEST_ONLY_IDS: set[int] | None = {1086780711}

_DB_KEY_TIMESTAMP = "kb_last_check"
_DB_KEY_SEEN_SHAS = "kb_seen_shas"

_last_check: str | None = None  # ISO timestamp
_seen_shas: set[str] = set()
_loaded = False


def _gh_headers() -> dict:
    h = {"Accept": "application/vnd.github.v3+json"}
    if _GITHUB_TOKEN:
        h["Authorization"] = f"token {_GITHUB_TOKEN}"
    return h


# Pending approval batches
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


async def _load_state():
    """Load timestamp and seen SHAs from DB."""
    global _last_check, _seen_shas, _loaded
    if _loaded:
        return
    try:
        async with AsyncSessionLocal() as session:
            _last_check = await repo.get_setting(session, _DB_KEY_TIMESTAMP) or None
            raw_shas = await repo.get_setting(session, _DB_KEY_SEEN_SHAS)
            if raw_shas:
                _seen_shas = set(json.loads(raw_shas))
        _loaded = True
        logger.info(f"KB watcher loaded: last_check={_last_check}, seen_shas={len(_seen_shas)}")
    except Exception:
        logger.exception("Failed to load KB watcher state")
        _loaded = True


async def _save_state():
    """Persist timestamp and seen SHAs to DB."""
    try:
        async with AsyncSessionLocal() as session:
            if _last_check:
                await repo.set_setting(session, _DB_KEY_TIMESTAMP, _last_check)
            # Keep only last 200 SHAs to prevent unbounded growth
            trimmed = list(_seen_shas)[-200:]
            await repo.set_setting(session, _DB_KEY_SEEN_SHAS, json.dumps(trimmed))
    except Exception:
        logger.exception("Failed to save KB watcher state")


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
        return None  # None = error, [] = no tasks found


def _extract_source_info(content: str, filename: str) -> dict:
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


async def check_kb_updates(bot):
    """Poll GitHub commits API for new files in watched folders."""
    global _last_check, _batch_counter

    await _load_state()

    # First run: start checking from 1 hour ago
    if not _last_check:
        _last_check = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        await _save_state()
        logger.info(f"KB watcher first run, starting from {_last_check}")
        return

    # Query commits since last check (with 2 sec overlap for safety)
    try:
        check_dt = datetime.fromisoformat(_last_check.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        check_dt = datetime.now(timezone.utc) - timedelta(hours=1)
    since = (check_dt - timedelta(seconds=2)).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        async with aiohttp.ClientSession() as http:
            async with http.get(
                KB_COMMITS_API,
                headers=_gh_headers(),
                params={"since": since, "per_page": 100},
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"KB commits API returned {resp.status}")
                    return
                commits = await resp.json()
    except Exception:
        logger.exception("Failed to fetch KB commits")
        return

    if not commits:
        return

    # Extract new files from unseen commits
    new_files: list[dict] = []
    seen_paths = set()
    latest_commit_date = _last_check

    for commit in commits:
        sha = commit.get("sha", "")
        if sha in _seen_shas:
            continue

        commit_date = commit.get("commit", {}).get("committer", {}).get("date", "")
        if commit_date > latest_commit_date:
            latest_commit_date = commit_date

        # Fetch commit details to get file list
        try:
            async with aiohttp.ClientSession() as http:
                async with http.get(
                    f"https://api.github.com/repos/{KB_REPO}/commits/{sha}",
                    headers=_gh_headers(),
                ) as resp:
                    if resp.status != 200:
                        continue
                    detail = await resp.json()
        except Exception:
            logger.exception(f"Failed to fetch commit {sha[:8]}")
            continue

        for f in detail.get("files", []):
            filepath = f.get("filename", "")
            if filepath in seen_paths:
                continue

            parts = filepath.rsplit("/", 1)
            if len(parts) != 2:
                continue
            folder, filename = parts

            if folder not in KB_WATCHED_FOLDERS:
                continue
            if filename.startswith("."):
                continue
            if f.get("status") not in ("added", "modified"):
                continue

            seen_paths.add(filepath)
            new_files.append({"path": filepath, "folder": folder, "filename": filename, "sha": sha})

    if not new_files:
        # No new files but mark commits as seen
        for commit in commits:
            _seen_shas.add(commit.get("sha", ""))
        _last_check = latest_commit_date
        await _save_state()
        return

    logger.info(f"KB watcher: {len(new_files)} new files from {len(commits)} commits")

    # Parse files
    all_tasks = []
    source_info = None
    gpt_failed = False
    processed_shas = set()

    for f in new_files:
        content = await _fetch_file_content(f["path"])
        if not content or len(content.strip()) < 20:
            logger.info(f"Skipping empty/short file {f['path']}")
            processed_shas.add(f["sha"])
            continue

        info = _extract_source_info(content, f["filename"])
        if not source_info:
            source_info = info

        prompt = PARSE_PROMPT_BUGS if "bugs" in f["folder"] else PARSE_PROMPT_CALLS
        tasks = await _parse_tasks_from_content(content, f["filename"], prompt)

        if tasks is None:
            # GPT error — don't mark this commit as seen, retry next scan
            gpt_failed = True
            logger.warning(f"GPT failed for {f['path']} — will retry next scan")
            continue

        if not tasks:
            logger.info(f"No tasks found in {f['path']}")
            processed_shas.add(f["sha"])
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
        processed_shas.add(f["sha"])
        logger.info(f"Parsed {len(tasks)} tasks from {f['path']}")

    # Update state — only mark successfully processed commits
    for sha in processed_shas:
        _seen_shas.add(sha)
    if not gpt_failed:
        _last_check = latest_commit_date
    await _save_state()

    # Create batch and notify
    if all_tasks:
        _batch_counter += 1
        batch_id = f"kb_{_batch_counter}"
        _approval_batches[batch_id] = {
            "locked_by": None,
            "tasks": all_tasks,
            "current_idx": 0,
            "source_info": source_info or {"filename": "", "date": "", "author": "", "author_name": ""},
            "file_url": "",
            "folder": "polling",
        }
        await _notify_top1_new_batch(bot, batch_id, all_tasks, source_info or {})
        logger.info(f"KB batch {batch_id}: {len(all_tasks)} tasks")


# ── Webhook (alternative trigger) ──

def verify_github_signature(payload: bytes, signature: str) -> bool:
    if not _WEBHOOK_SECRET:
        return True
    expected = "sha256=" + hmac.new(
        _WEBHOOK_SECRET.encode(), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


async def handle_webhook_push(payload: dict, bot) -> dict:
    """Process GitHub push webhook — same logic as polling but triggered instantly."""
    global _batch_counter

    commits = payload.get("commits", [])
    if not commits:
        return {"status": "no_commits"}

    new_files: list[dict] = []
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
            if folder not in KB_WATCHED_FOLDERS or filename.startswith("."):
                continue
            new_files.append({"path": filepath, "folder": folder, "filename": filename})

    if not new_files:
        return {"status": "no_watched_files"}

    all_tasks = []
    source_info = None

    for f in new_files:
        content = await _fetch_file_content(f["path"])
        if not content or len(content.strip()) < 20:
            continue
        info = _extract_source_info(content, f["filename"])
        if not source_info:
            source_info = info
        prompt = PARSE_PROMPT_BUGS if "bugs" in f["folder"] else PARSE_PROMPT_CALLS
        tasks = await _parse_tasks_from_content(content, f["filename"], prompt)
        if not tasks:
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

    if not all_tasks:
        return {"status": "no_tasks_found"}

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
    return {"status": "ok", "batch_id": batch_id, "tasks_count": len(all_tasks)}


async def _notify_top1_new_batch(bot, batch_id: str, tasks: list[dict], source: dict):
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
