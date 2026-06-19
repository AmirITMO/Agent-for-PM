"""Watches a GitHub folder for new bug reports and creates tasks on kanban."""
import logging
import aiohttp
from agent3_pm.database import AsyncSessionLocal
from agent3_pm import repository as repo
from agent3_pm.models import TaskStatus

logger = logging.getLogger(__name__)

GITHUB_REPO = "AmirITMO/Autogent_Roman"
GITHUB_PATH = "user/bugs"
GITHUB_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_PATH}"
GITHUB_WEB_URL = f"https://github.com/{GITHUB_REPO}/tree/main/{GITHUB_PATH}"

_seen_files: set[str] = set()


async def check_github_bugs():
    """Check GitHub folder for new bug files and create tasks."""
    global _seen_files
    logger.info("Checking GitHub bugs folder...")

    try:
        async with aiohttp.ClientSession() as http:
            async with http.get(GITHUB_API_URL, headers={"Accept": "application/vnd.github.v3+json"}) as resp:
                if resp.status != 200:
                    logger.warning(f"GitHub API returned {resp.status}")
                    return
                files = await resp.json()
    except Exception:
        logger.exception("Failed to fetch GitHub bugs")
        return

    if not isinstance(files, list):
        return

    # First run — just populate seen set
    if not _seen_files:
        _seen_files = {f["name"] for f in files}
        logger.info(f"GitHub watcher initialized with {len(_seen_files)} files")
        return

    new_files = [f for f in files if f["name"] not in _seen_files]
    if not new_files:
        return

    async with AsyncSessionLocal() as session:
        # Get first project for bugs (MarketAI Dev)
        projects = await repo.get_all_projects(session)
        project_id = projects[0].id if projects else None

        for f in new_files:
            filename = f["name"]
            file_url = f.get("html_url", f"{GITHUB_WEB_URL}/{filename}")
            title = f"Баг: {filename.replace('.md', '').replace('_', ' ').replace('-', ' ')}"

            task = await repo.create_task(
                session, title=title, project_id=project_id,
                status=TaskStatus.BACKLOG, priority=0, is_bug=True,
            )

            await repo.add_comment(
                session, task.id, None,
                f"Автоматически создано из GitHub.\nФайл: {filename}\n{file_url}",
            )

            _seen_files.add(filename)
            logger.info(f"Created bug task from GitHub: {title}")
