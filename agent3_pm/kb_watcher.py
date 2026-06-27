"""
Knowledge Base watcher — monitors GitHub repo for new calls/bugs/tasks,
parses tasks via GPT, sends to TOP-1 for approval before creating on kanban.

Persistent state: seen files stored in DB (Settings table) to survive restarts.
"""
import json
import logging
import aiohttp
from agent3_pm.database import AsyncSessionLocal
from agent3_pm import repository as repo
from agent3_pm.models import LEVEL_1_POSITIONS
from agent3_pm.task_agent import _get_client

logger = logging.getLogger(__name__)

KB_REPO = "Deci1337/mai-knowledge-base"
KB_FOLDERS = ["user/calls", "user/bugs", "calls/tasks"]
KB_API = f"https://api.github.com/repos/{KB_REPO}/contents"
KB_RAW = f"https://raw.githubusercontent.com/{KB_REPO}/main"

_seen_files: dict[str, set[str]] = {}
_seen_global: set[str] = set()

# Pending approval batches: {batch_id: {locked_by, tasks, current_idx, source_info}}
_approval_batches: dict[str, dict] = {}
_batch_counter = 0

_SEEN_DB_KEY = "kb_seen_files"


async def _load_seen_from_db():
    """Load seen files from DB to survive container restarts."""
    global _seen_files, _seen_global
    try:
        async with AsyncSessionLocal() as session:
            raw = await repo.get_setting(session, _SEEN_DB_KEY)
            if raw:
                data = json.loads(raw)
                _seen_files = {k: set(v) for k, v in data.get("folders", {}).items()}
                _seen_global = set(data.get("global", []))
                logger.info(f"Loaded seen files from DB: {sum(len(v) for v in _seen_files.values())} entries")
    except Exception:
        logger.exception("Failed to load seen files from DB")


async def _save_seen_to_db():
    """Persist seen files to DB."""
    try:
        data = {
            "folders": {k: list(v) for k, v in _seen_files.items()},
            "global": list(_seen_global),
        }
        async with AsyncSessionLocal() as session:
            await repo.set_setting(session, _SEEN_DB_KEY, json.dumps(data, ensure_ascii=False))
    except Exception:
        logger.exception("Failed to save seen files to DB")


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

Верни массив JSON: [задача1, задача2, ...]
Если задач нет — верни пустой массив [].
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

Верни массив JSON. Если багов нет — [].
Извлекай ТОЛЬКО баги/ошибки, не обычные задачи."""


async def _fetch_folder(folder: str) -> list[dict]:
    """Get file list from GitHub folder."""
    try:
        async with aiohttp.ClientSession() as http:
            async with http.get(f"{KB_API}/{folder}",
                                headers={"Accept": "application/vnd.github.v3+json"}) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                return data if isinstance(data, list) else []
    except Exception:
        logger.exception(f"Failed to fetch {folder}")
        return []


async def _fetch_file_content(path: str) -> str:
    """Download raw file content."""
    try:
        async with aiohttp.ClientSession() as http:
            async with http.get(f"{KB_RAW}/{path}") as resp:
                if resp.status != 200:
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
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
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


async def check_kb_updates(bot):
    """Main watcher: check for new files, parse tasks, notify TOP-1."""
    global _batch_counter
    logger.info("Checking knowledge base for updates...")

    # Load from DB on first run
    if not _seen_files:
        await _load_seen_from_db()

    all_new_tasks = []
    all_source_info = None
    changed = False

    for folder in KB_FOLDERS:
        files = await _fetch_folder(folder)
        if not files:
            continue

        if folder not in _seen_files:
            _seen_files[folder] = {f["name"] for f in files}
            logger.info(f"KB watcher initialized {folder}: {len(_seen_files[folder])} files")
            changed = True
            continue

        new_files = [f for f in files if f["name"] not in _seen_files[folder]]
        if not new_files:
            continue

        for f in new_files:
            _seen_files[folder].add(f["name"])
            changed = True

            if f["name"] in _seen_global:
                logger.info(f"Skipping duplicate file {f['name']} in {folder}")
                continue
            _seen_global.add(f["name"])

            filepath = f"{folder}/{f['name']}"
            content = await _fetch_file_content(filepath)
            if not content or len(content.strip()) < 20:
                logger.info(f"Skipping empty/short file {filepath}")
                continue

            source_info = _extract_source_info(content, f["name"])
            if not all_source_info:
                all_source_info = source_info

            prompt = PARSE_PROMPT_BUGS if "bugs" in folder else PARSE_PROMPT_CALLS
            tasks = await _parse_tasks_from_content(content, f["name"], prompt=prompt)
            if not tasks:
                logger.info(f"No tasks found in {filepath}")
                continue

            if "bugs" in folder:
                for t in tasks:
                    t["is_bug"] = True
                    if t.get("priority", 2) > 1:
                        t["priority"] = 1

            file_url = f"https://github.com/{KB_REPO}/blob/main/{filepath}"
            for t in tasks:
                t["_source_url"] = file_url

            all_new_tasks.extend(tasks)
            logger.info(f"Parsed {len(tasks)} tasks from {filepath}")

    # Save to DB after changes
    if changed:
        await _save_seen_to_db()

    # Merge all new tasks into ONE batch
    if all_new_tasks:
        # Deduplicate by title
        seen_titles = set()
        unique_tasks = []
        for t in all_new_tasks:
            title_key = t.get("title", "").lower().strip()
            if title_key and title_key not in seen_titles:
                seen_titles.add(title_key)
                unique_tasks.append(t)

        if unique_tasks:
            _batch_counter += 1
            batch_id = f"kb_{_batch_counter}"
            _approval_batches[batch_id] = {
                "locked_by": None,
                "tasks": unique_tasks,
                "current_idx": 0,
                "source_info": all_source_info or {"filename": "", "date": "", "author": "", "author_name": ""},
                "file_url": "",
                "folder": "mixed",
            }
            await _notify_top1_new_batch(bot, batch_id, unique_tasks, all_source_info or {})
            logger.info(f"New batch {batch_id}: {len(unique_tasks)} tasks (merged from all folders)")


async def _notify_top1_new_batch(bot, batch_id: str, tasks: list[dict], source: dict):
    """Send notification to all TOP-1 users about new tasks batch."""
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
