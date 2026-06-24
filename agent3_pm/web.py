import os
import uuid
import hashlib
import datetime
from pathlib import Path
from fastapi import FastAPI, Request, Depends, HTTPException, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from agent3_pm.config import config
from agent3_pm.database import AsyncSessionLocal
from agent3_pm.models import (
    TaskStatus, POSITIONS, POSITION_GROUPS, PRIORITY_LEVELS, DEFAULT_PRIORITY, is_level_1,
)
from agent3_pm import repository as repo

BASE_DIR = Path(__file__).parent
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="Agent3 PM Tracker")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")


async def get_session():
    async with AsyncSessionLocal() as session:
        yield session


# ── Labels ──

STATUS_LABEL_MAP = {
    "backlog": "Бэклог", "planning": "Планирование", "todo": "К выполнению",
    "wip": "В работе", "done": "Готово", "approved": "Принято", "hold": "На паузе",
}
KANBAN_COLUMNS = ["backlog", "planning", "todo", "wip", "done", "approved", "hold"]
STATUS_ENUM_MAP = {s.value: s for s in TaskStatus}
PRIORITY_LABEL_MAP = {0: "P0 — срочно", 1: "P1", 2: "P2", 3: "P3"}


# ── Identity (cookie set from bot link) ──

def _make_token(user_id: int) -> str:
    return hashlib.sha256(f"{user_id}:{config.SECRET_KEY}".encode()).hexdigest()


async def _current_user(request: Request, session: AsyncSession):
    uid = request.cookies.get("uid")
    token = request.cookies.get("auth")
    if not uid or not token:
        return None
    try:
        user = await repo.get_user_by_id(session, int(uid))
    except (ValueError, TypeError):
        return None
    if not user or _make_token(user.id) != token:
        return None
    return user


def _can_manage(user) -> bool:
    return bool(user and is_level_1(user.position))


def _task_to_dict(task) -> dict:
    return {
        "id": task.id,
        "title": task.title,
        "description": task.description,
        "status": task.status.value if hasattr(task.status, "value") else task.status,
        "status_label": STATUS_LABEL_MAP.get(
            task.status.value if hasattr(task.status, "value") else task.status, ""),
        "priority": task.priority,
        "is_bug": task.is_bug,
        "is_red": task.is_red,
        "assignee": task.assignee.name if task.assignee else "—",
        "assignee_id": task.assignee_id,
        "project": task.project.name if task.project else "—",
        "project_id": task.project_id,
        "estimated_hours": float(task.estimated_hours) if task.estimated_hours else None,
        "due_date": task.due_date,
        "is_overdue": task.is_overdue,
        "is_hot": task.is_hot,
        "comments_count": len(task.comments) if task.comments else 0,
    }


# ── Entry / identity ──

@app.get("/")
async def index(request: Request, session: AsyncSession = Depends(get_session)):
    user = await _current_user(request, session)
    if user:
        return RedirectResponse("/board")
    return templates.TemplateResponse(request, "welcome.html", {})


@app.get("/enter/{user_id}")
async def enter(user_id: int, next: str | None = None,
                session: AsyncSession = Depends(get_session)):
    user = await repo.get_user_by_id(session, user_id)
    if not user:
        raise HTTPException(404, "Пользователь не найден")
    redirect_to = next if next and next.startswith("/") else "/board"
    response = RedirectResponse(redirect_to, status_code=303)
    response.set_cookie("uid", str(user.id), max_age=86400 * 30)
    response.set_cookie("auth", _make_token(user.id), max_age=86400 * 30)
    return response


@app.get("/logout")
async def logout():
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie("uid")
    response.delete_cookie("auth")
    return response


# ── Boards (kanban) ──

async def _render_board(request, session, current, project_id, only_mine):
    all_projects = await repo.get_all_projects(session)
    users = await repo.get_all_users(session)

    # Filter projects by board access (Level 1 sees all, others only with checkbox)
    if current and not only_mine:
        if is_level_1(current.position):
            projects = all_projects
        else:
            my_board_ids = set()
            for p in all_projects:
                members = await repo.get_board_member_ids(session, p.id)
                if current.id in members:
                    my_board_ids.add(p.id)
            projects = [p for p in all_projects if p.id in my_board_ids]
    else:
        projects = all_projects

    pid = project_id
    if not pid and projects and not only_mine:
        pid = projects[0].id if projects else None
    current_project = next((p for p in projects if p.id == pid), None)

    columns = {}
    for col in KANBAN_COLUMNS:
        status_enum = STATUS_ENUM_MAP[col]
        kwargs = {"status": status_enum}
        if only_mine:
            kwargs["assignee_id"] = current.id if current else -1
        else:
            kwargs["project_id"] = pid
        tasks = await repo.get_all_tasks(session, **kwargs)
        columns[col] = [_task_to_dict(t) for t in tasks]

    members = await repo.get_board_members(session, pid) if pid and not only_mine else []

    return templates.TemplateResponse(request, "board.html", {
        "columns": columns,
        "kanban_columns": KANBAN_COLUMNS,
        "status_label": STATUS_LABEL_MAP,
        "projects": projects,
        "users": users,
        "members": members,
        "selected_project_id": pid,
        "current_project": current_project,
        "current_user": current,
        "can_manage": _can_manage(current),
        "only_mine": only_mine,
        "statuses": KANBAN_COLUMNS,
        "priorities": PRIORITY_LEVELS,
        "priority_label": PRIORITY_LABEL_MAP,
        "default_priority": DEFAULT_PRIORITY,
    })


@app.get("/board", response_class=HTMLResponse)
async def board(request: Request, project_id: str | None = None,
                session: AsyncSession = Depends(get_session)):
    current = await _current_user(request, session)
    if not current:
        return RedirectResponse("/")
    pid = int(project_id) if project_id and project_id.strip() else None
    return await _render_board(request, session, current, pid, only_mine=False)


@app.get("/my", response_class=HTMLResponse)
async def my_tasks(request: Request, session: AsyncSession = Depends(get_session)):
    current = await _current_user(request, session)
    if not current:
        return RedirectResponse("/")
    return await _render_board(request, session, current, None, only_mine=True)


# ── Employees (general list with board checkboxes) ──

@app.get("/employees", response_class=HTMLResponse)
async def employees(request: Request, session: AsyncSession = Depends(get_session)):
    current = await _current_user(request, session)
    if not _can_manage(current):
        return RedirectResponse("/board")
    users = await repo.get_all_users(session)
    projects = await repo.get_all_projects(session)
    access = {}
    for p in projects:
        access[p.id] = await repo.get_board_member_ids(session, p.id)
    return templates.TemplateResponse(request, "employees.html", {
        "users": users, "projects": projects, "access": access,
        "positions": POSITIONS, "position_groups": POSITION_GROUPS,
        "current_user": current, "can_manage": True,
    })


@app.post("/api/board-access")
async def toggle_board_access(request: Request, session: AsyncSession = Depends(get_session)):
    current = await _current_user(request, session)
    if not _can_manage(current):
        raise HTTPException(403, "Недостаточно прав")
    form = await request.form()
    project_id = int(form.get("project_id"))
    user_id = int(form.get("user_id"))
    allowed = form.get("allowed") == "1"
    await repo.set_board_access(session, project_id, user_id, allowed)
    return {"ok": True}


@app.post("/api/users", response_class=RedirectResponse)
async def create_user_api(request: Request, session: AsyncSession = Depends(get_session)):
    if not _can_manage(await _current_user(request, session)):
        raise HTTPException(403, "Недостаточно прав")
    form = await request.form()
    name = form.get("name", "").strip()
    position = form.get("position", "").strip() or None
    username = form.get("telegram_username", "").strip().lstrip("@") or None
    await repo.create_user(session, name=name, position=position, telegram_username=username)
    return RedirectResponse("/employees", status_code=303)


@app.post("/api/users/{user_id}", response_class=RedirectResponse)
async def update_user_api(user_id: int, request: Request,
                          session: AsyncSession = Depends(get_session)):
    if not _can_manage(await _current_user(request, session)):
        raise HTTPException(403, "Недостаточно прав")
    form = await request.form()
    name = form.get("name", "").strip()
    position = form.get("position", "").strip() or None
    username = form.get("telegram_username", "").strip().lstrip("@") or None
    await repo.update_user(session, user_id, name=name, position=position,
                           telegram_username=username)
    return RedirectResponse("/employees", status_code=303)


@app.post("/api/users/{user_id}/delete", response_class=RedirectResponse)
async def delete_user_api(user_id: int, request: Request,
                          session: AsyncSession = Depends(get_session)):
    if not _can_manage(await _current_user(request, session)):
        raise HTTPException(403, "Недостаточно прав")
    await repo.delete_user(session, user_id)
    return RedirectResponse("/employees", status_code=303)


# ── Task detail (shareable) ──

@app.get("/task/{task_id}", response_class=HTMLResponse)
async def task_detail(request: Request, task_id: int, back: str | None = None,
                      session: AsyncSession = Depends(get_session)):
    current = await _current_user(request, session)
    task = await repo.get_task_by_id(session, task_id)
    if not task:
        raise HTTPException(404, "Задача не найдена")
    users = await repo.get_all_users(session)
    projects = await repo.get_all_projects(session)
    back_url = back if back else "/board"
    return templates.TemplateResponse(request, "task_detail.html", {
        "task": task,
        "users": users,
        "projects": projects,
        "current_user": current,
        "can_manage": _can_manage(current),
        "status_label": STATUS_LABEL_MAP,
        "statuses": KANBAN_COLUMNS,
        "priorities": PRIORITY_LEVELS,
        "priority_label": PRIORITY_LABEL_MAP,
        "share_url": _abs_url(request, f"/task/{task.id}"),
        "back_url": back_url,
    })


def _abs_url(request: Request, path: str) -> str:
    base = config.WEB_BASE_URL.rstrip("/")
    return f"{base}{path}"


# ── Task API ──

@app.post("/api/tasks", response_class=RedirectResponse)
async def create_task_api(request: Request, session: AsyncSession = Depends(get_session)):
    current = await _current_user(request, session)
    form = await request.form()
    title = form.get("title", "").strip()
    description = form.get("description", "").strip() or None
    status = form.get("status", "backlog")
    priority = int(form.get("priority", DEFAULT_PRIORITY))
    is_bug = form.get("is_bug") == "1"
    proj_raw = form.get("project_id", "")
    assign_raw = form.get("assignee_id", "")
    hours_raw = form.get("estimated_hours", "")
    due_raw = form.get("due_date", "")
    redirect = form.get("redirect", "/board")

    proj_id = int(proj_raw) if proj_raw and proj_raw.strip() else None
    assign_id = int(assign_raw) if assign_raw and assign_raw.strip() else None
    estimated_hours = float(hours_raw) if hours_raw and hours_raw.strip() else None
    dd = datetime.date.fromisoformat(due_raw) if due_raw and due_raw.strip() else None

    task = await repo.create_task(
        session, title=title, project_id=proj_id, description=description,
        status=TaskStatus(status), priority=priority, is_bug=is_bug,
        assignee_id=assign_id, creator_id=current.id if current else None,
        estimated_hours=estimated_hours, due_date=dd,
    )
    if assign_id:
        task = await repo.get_task_by_id(session, task.id)
        creator_name = current.name if current else None
        await _notify_assignee(session, task, creator_name)
    return RedirectResponse(redirect, status_code=303)


@app.post("/api/tasks/{task_id}", response_class=RedirectResponse)
async def update_task_api(task_id: int, request: Request,
                          session: AsyncSession = Depends(get_session)):
    form = await request.form()
    title = form.get("title", "").strip()
    description = form.get("description", "").strip() or None
    priority = int(form.get("priority", DEFAULT_PRIORITY))
    is_bug = form.get("is_bug") == "1"
    status = form.get("status")
    assign_raw = form.get("assignee_id", "")
    proj_raw = form.get("project_id", "")
    hours_raw = form.get("estimated_hours", "")
    due_raw = form.get("due_date", "")
    redirect = form.get("redirect", "/board")

    fields = {
        "title": title, "description": description,
        "priority": priority, "is_bug": is_bug,
        "assignee_id": int(assign_raw) if assign_raw and assign_raw.strip() else None,
        "estimated_hours": float(hours_raw) if hours_raw and hours_raw.strip() else None,
        "due_date": datetime.date.fromisoformat(due_raw) if due_raw and due_raw.strip() else None,
    }
    if proj_raw and proj_raw.strip():
        fields["project_id"] = int(proj_raw)
    if status:
        fields["status"] = TaskStatus(status)
    task_before = await repo.get_task_by_id(session, task_id)
    old_assignee_id = task_before.assignee_id if task_before else None

    await repo.update_task(session, task_id, **fields)

    # Уведомить ответственного об обновлении (если обновил не он сам)
    current = await _current_user(request, session)
    updater_id = current.id if current else None
    updater_name = current.name if current else "Кто-то"
    task_after = await repo.get_task_by_id(session, task_id)
    if task_after:
        await _notify_task_updated(session, task_after, updater_id, updater_name, old_assignee_id)

    return RedirectResponse(redirect, status_code=303)


@app.post("/api/tasks/{task_id}/status", response_class=RedirectResponse)
async def update_task_status_api(task_id: int, request: Request,
                                 session: AsyncSession = Depends(get_session)):
    form = await request.form()
    status = form.get("status")
    redirect = form.get("redirect", "/board")
    await repo.update_task_status(session, task_id, TaskStatus(status))
    return RedirectResponse(redirect, status_code=303)


@app.post("/api/tasks/{task_id}/done", response_class=RedirectResponse)
async def mark_done_api(task_id: int, request: Request,
                        session: AsyncSession = Depends(get_session)):
    form = await request.form()
    redirect = form.get("redirect", "/board")
    task = await repo.update_task_status(session, task_id, TaskStatus.DONE)
    if task:
        await _notify_managers_done(session, task)
    return RedirectResponse(redirect, status_code=303)


@app.post("/api/tasks/{task_id}/delete", response_class=RedirectResponse)
async def delete_task_api(task_id: int, request: Request,
                          session: AsyncSession = Depends(get_session)):
    form = await request.form()
    redirect = form.get("redirect", "/board")
    if not redirect or redirect.startswith("http"):
        redirect = "/board"
    try:
        await repo.delete_task(session, task_id)
    except Exception:
        import logging
        logging.getLogger(__name__).exception(f"Failed to delete task {task_id}")
    return RedirectResponse(redirect, status_code=303)


# ── Comments + attachments ──

@app.post("/api/tasks/{task_id}/comment", response_class=RedirectResponse)
async def add_comment_api(task_id: int, request: Request,
                          session: AsyncSession = Depends(get_session)):
    current = await _current_user(request, session)
    form = await request.form()
    text = (form.get("text") or "").strip() or None
    files = form.getlist("files")

    has_file = any(getattr(f, "filename", "") for f in files)
    if not text and not has_file:
        return RedirectResponse(f"/task/{task_id}", status_code=303)

    comment = await repo.add_comment(session, task_id, current.id if current else None, text)

    for f in files:
        filename = getattr(f, "filename", "")
        if not filename:
            continue
        ext = os.path.splitext(filename)[1]
        stored = f"{uuid.uuid4().hex}{ext}"
        content = await f.read()
        (UPLOAD_DIR / stored).write_bytes(content)
        await repo.add_attachment(session, comment.id, filename, stored, f.content_type)

    return RedirectResponse(f"/task/{task_id}", status_code=303)


async def _notify_managers_done(session, task):
    try:
        from telegram import Bot
        if not config.TELEGRAM_BOT_TOKEN:
            return
        bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
        managers = await repo.get_managers(session)
        assignee = task.assignee.name if task.assignee else "не назначен"
        project = task.project.name if task.project else "—"
        text = (f"Задача выполнена\n\n{task.title}\nПроект: {project}\n"
                f"Исполнитель: {assignee}\n\n{_abs_url_simple(f'/task/{task.id}')}")
        for m in managers:
            if m.telegram_id:
                await bot.send_message(chat_id=m.telegram_id, text=text)
    except Exception:
        import logging
        logging.getLogger(__name__).exception("notify done failed")


def _abs_url_simple(path: str) -> str:
    return f"{config.WEB_BASE_URL.rstrip('/')}{path}"


async def _notify_task_updated(session, task, updater_id: int | None,
                               updater_name: str, old_assignee_id: int | None = None):
    """Notify assignee when their task is updated by someone else."""
    try:
        if not config.TELEGRAM_BOT_TOKEN:
            return
        from telegram import Bot
        bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
        notified = set()

        # Notify current assignee (if different from updater)
        if task.assignee and task.assignee.telegram_id and task.assignee_id != updater_id:
            text = (f"Твоя задача обновлена пользователем {updater_name}\n\n"
                    f"<b>{task.title}</b>\n"
                    f'<a href="{_abs_url_simple(f"/task/{task.id}")}">Открыть задачу</a>')
            await bot.send_message(chat_id=task.assignee.telegram_id, text=text,
                                   parse_mode="HTML", disable_web_page_preview=True)
            notified.add(task.assignee_id)

        # If assignee changed, notify the OLD assignee too
        if old_assignee_id and old_assignee_id != task.assignee_id and old_assignee_id not in notified:
            old_user = await repo.get_user_by_id(session, old_assignee_id)
            if old_user and old_user.telegram_id and old_user.id != updater_id:
                text = (f"Задача переназначена пользователем {updater_name}\n\n"
                        f"<b>{task.title}</b>\n"
                        f'<a href="{_abs_url_simple(f"/task/{task.id}")}">Открыть задачу</a>')
                await bot.send_message(chat_id=old_user.telegram_id, text=text,
                                       parse_mode="HTML", disable_web_page_preview=True)
    except Exception:
        import logging
        logging.getLogger(__name__).exception("notify update failed")


async def _notify_assignee(session, task, creator_name: str | None = None):
    """Notify assignee when task is assigned to them."""
    try:
        if not task.assignee or not task.assignee.telegram_id:
            return
        from telegram import Bot
        if not config.TELEGRAM_BOT_TOKEN:
            return
        bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
        by = f" от {creator_name}" if creator_name else ""
        project = task.project.name if task.project else "—"
        text = (f"Тебе назначена задача{by}\n\n"
                f"{task.title}\n"
                f"Проект: {project}\n"
                f"Приоритет: P{task.priority}\n"
                f"{_abs_url_simple(f'/task/{task.id}')}")
        if task.due_date:
            text += f"\nДедлайн: {task.due_date.strftime('%d.%m.%Y')}"
        await bot.send_message(chat_id=task.assignee.telegram_id, text=text)
    except Exception:
        import logging
        logging.getLogger(__name__).exception("notify assignee failed")


# ── Settings ──

@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, session: AsyncSession = Depends(get_session)):
    current = await _current_user(request, session)
    from agent3_pm.models import Settings
    settings = await repo.get_all_settings(session)
    return templates.TemplateResponse(request, "settings.html", {
        "settings": settings, "labels": Settings.LABELS,
        "keys": list(Settings.LABELS.keys()), "current_user": current,
        "can_manage": _can_manage(current),
    })


@app.post("/api/settings", response_class=RedirectResponse)
async def update_settings_api(request: Request, session: AsyncSession = Depends(get_session)):
    form = await request.form()
    from agent3_pm.models import Settings
    for key in Settings.LABELS:
        val = form.get(key)
        if val is not None:
            await repo.set_setting(session, key, str(val).strip())
    return RedirectResponse("/settings", status_code=303)
