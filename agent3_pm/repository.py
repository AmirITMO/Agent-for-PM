import datetime
from sqlalchemy import select, or_, func, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from agent3_pm.models import (
    User, Project, Task, TaskComment, Attachment, NotificationLog, Settings,
    TaskStatus, UserRole, board_members, LEVEL_1_POSITIONS,
    ACTIVE_STATUSES, CLOSED_STATUSES, DEFAULT_PRIORITY,
)

# Должности уровня 1 получают утреннюю сводку
MANAGER_POSITIONS = set(LEVEL_1_POSITIONS)


# ── Users ──

async def get_user_by_telegram_id(session: AsyncSession, telegram_id: int) -> User | None:
    result = await session.execute(select(User).where(User.telegram_id == telegram_id))
    return result.scalar_one_or_none()


async def get_user_by_telegram_username(session: AsyncSession, username: str) -> User | None:
    result = await session.execute(
        select(User).where(func.lower(User.telegram_username) == username.lower())
    )
    return result.scalar_one_or_none()


async def get_user_by_id(session: AsyncSession, user_id: int) -> User | None:
    result = await session.execute(
        select(User).options(selectinload(User.boards)).where(User.id == user_id)
    )
    return result.scalar_one_or_none()


async def get_all_users(session: AsyncSession, include_blocked: bool = False) -> list[User]:
    q = select(User).options(selectinload(User.boards))
    if not include_blocked:
        q = q.where(User.is_active == True)  # noqa: E712
    q = q.order_by(User.name)
    result = await session.execute(q)
    return list(result.scalars().all())


async def get_managers(session: AsyncSession) -> list[User]:
    result = await session.execute(
        select(User).where(User.position.in_(MANAGER_POSITIONS))
    )
    return list(result.scalars().all())


async def register_user(session: AsyncSession, telegram_id: int, telegram_username: str | None,
                        name: str, position: str | None = None) -> User:
    user = User(
        name=name, telegram_id=telegram_id, telegram_username=telegram_username,
        position=position, role=UserRole.EMPLOYEE,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    await _auto_grant_boards(session, user)
    return user


async def create_user(session: AsyncSession, name: str, position: str | None = None,
                      telegram_username: str | None = None, telegram_id: int | None = None,
                      role: UserRole = UserRole.EMPLOYEE) -> User:
    user = User(name=name, position=position, telegram_username=telegram_username,
                telegram_id=telegram_id, role=role)
    session.add(user)
    await session.commit()
    await session.refresh(user)
    await _auto_grant_boards(session, user)
    return user


async def _auto_grant_boards(session: AsyncSession, user: User):
    """Level 1 gets access to all boards automatically."""
    from agent3_pm.models import is_level_1
    if is_level_1(user.position):
        projects = await get_all_projects(session)
        for p in projects:
            await set_board_access(session, p.id, user.id, True)


async def update_user(session: AsyncSession, user_id: int, **kwargs) -> User | None:
    user = await get_user_by_id(session, user_id)
    if not user:
        return None
    for key, value in kwargs.items():
        if hasattr(user, key):
            setattr(user, key, value)
    await session.commit()
    await session.refresh(user)
    return user


async def bind_telegram_id(session: AsyncSession, user_id: int, telegram_id: int) -> User | None:
    user = await get_user_by_id(session, user_id)
    if not user:
        return None
    user.telegram_id = telegram_id
    await session.commit()
    await session.refresh(user)
    return user


async def delete_user(session: AsyncSession, user_id: int) -> bool:
    """Soft-delete: deactivate user and archive their tasks."""
    user = await get_user_by_id(session, user_id)
    if not user:
        return False
    user.is_active = False
    now = datetime.datetime.now()
    tasks = await get_all_tasks(session, assignee_id=user_id)
    for t in tasks:
        if t.status not in CLOSED_STATUSES:
            t.archived_at = now
    await session.commit()
    return True


async def restore_user(session: AsyncSession, user_id: int) -> User | None:
    """Restore soft-deleted user (unarchive tasks stays manual)."""
    user = await get_user_by_id(session, user_id)
    if not user:
        return None
    user.is_active = True
    await session.commit()
    await session.refresh(user)
    return user


# ── Board membership ──

async def set_board_access(session: AsyncSession, project_id: int, user_id: int, allowed: bool):
    exists = await session.execute(
        select(board_members).where(
            board_members.c.project_id == project_id,
            board_members.c.user_id == user_id,
        )
    )
    has = exists.first() is not None
    if allowed and not has:
        await session.execute(board_members.insert().values(project_id=project_id, user_id=user_id))
        await session.commit()
    elif not allowed and has:
        await session.execute(
            delete(board_members).where(
                board_members.c.project_id == project_id,
                board_members.c.user_id == user_id,
            )
        )
        await session.commit()


async def get_board_member_ids(session: AsyncSession, project_id: int) -> set[int]:
    result = await session.execute(
        select(board_members.c.user_id).where(board_members.c.project_id == project_id)
    )
    return {row[0] for row in result.all()}


async def get_board_members(session: AsyncSession, project_id: int) -> list[User]:
    result = await session.execute(
        select(User).join(board_members, board_members.c.user_id == User.id)
        .where(board_members.c.project_id == project_id).order_by(User.name)
    )
    return list(result.scalars().all())


# ── Projects ──

async def get_project_by_name(session: AsyncSession, name: str) -> Project | None:
    result = await session.execute(
        select(Project).where(func.lower(Project.name) == name.lower())
    )
    return result.scalar_one_or_none()


async def get_project_by_id(session: AsyncSession, project_id: int) -> Project | None:
    result = await session.execute(select(Project).where(Project.id == project_id))
    return result.scalar_one_or_none()


async def get_all_projects(session: AsyncSession) -> list[Project]:
    result = await session.execute(select(Project).order_by(Project.id))
    return list(result.scalars().all())


async def create_project(session: AsyncSession, name: str, description: str | None = None) -> Project:
    project = Project(name=name, description=description)
    session.add(project)
    await session.commit()
    await session.refresh(project)
    return project


# ── Tasks ──

async def get_task_by_id(session: AsyncSession, task_id: int) -> Task | None:
    result = await session.execute(
        select(Task)
        .options(
            selectinload(Task.assignee),
            selectinload(Task.creator),
            selectinload(Task.project),
            selectinload(Task.comments).selectinload(TaskComment.user),
            selectinload(Task.comments).selectinload(TaskComment.attachments),
        )
        .where(Task.id == task_id)
    )
    return result.scalar_one_or_none()


async def search_tasks_by_title(session: AsyncSession, query: str, limit: int = 10) -> list[Task]:
    result = await session.execute(
        select(Task)
        .options(selectinload(Task.assignee), selectinload(Task.project))
        .where(Task.title.ilike(f"%{query}%"))
        .order_by(Task.updated_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def get_tasks_for_user_today(session: AsyncSession, user_id: int) -> list[Task]:
    today = datetime.date.today()
    result = await session.execute(
        select(Task)
        .options(selectinload(Task.project))
        .where(
            Task.assignee_id == user_id,
            Task.status.in_([s.value for s in ACTIVE_STATUSES]),
        )
        .where(
            or_(
                Task.due_date == today,
                Task.due_date < today,
                Task.status.in_([TaskStatus.TODO.value, TaskStatus.WIP.value]),
            )
        )
        .order_by(Task.priority.asc(), Task.due_date.asc().nullslast())
    )
    return list(result.scalars().all())


async def get_overdue_tasks(session: AsyncSession, project_id: int | None = None,
                            user_id: int | None = None) -> list[Task]:
    today = datetime.date.today()
    q = (
        select(Task)
        .options(selectinload(Task.assignee), selectinload(Task.project))
        .where(
            Task.due_date < today,
            Task.status.in_([s.value for s in ACTIVE_STATUSES]),
        )
        .order_by(Task.due_date.asc())
    )
    if project_id:
        q = q.where(Task.project_id == project_id)
    if user_id:
        q = q.where(Task.assignee_id == user_id)
    result = await session.execute(q)
    return list(result.scalars().all())


async def get_hot_tasks(session: AsyncSession, warning_hours: int = 24,
                        user_id: int | None = None) -> list[Task]:
    today = datetime.date.today()
    deadline_threshold = today + datetime.timedelta(hours=warning_hours)
    q = (
        select(Task)
        .options(selectinload(Task.assignee), selectinload(Task.project))
        .where(
            Task.due_date != None,  # noqa: E711
            Task.due_date <= deadline_threshold,
            Task.due_date >= today,
            Task.status.in_([s.value for s in ACTIVE_STATUSES]),
        )
        .order_by(Task.due_date.asc())
    )
    if user_id:
        q = q.where(Task.assignee_id == user_id)
    result = await session.execute(q)
    return list(result.scalars().all())


async def get_user_bugs(session: AsyncSession, user_id: int) -> list[Task]:
    """Баги и срочные (priority 0) задачи пользователя."""
    result = await session.execute(
        select(Task)
        .options(selectinload(Task.project))
        .where(
            Task.assignee_id == user_id,
            Task.status.in_([s.value for s in ACTIVE_STATUSES]),
            or_(Task.is_bug == True, Task.priority == 0),  # noqa: E712
        )
        .order_by(Task.due_date.asc().nullslast())
    )
    return list(result.scalars().all())


async def get_tasks_due_today(session: AsyncSession) -> list[Task]:
    today = datetime.date.today()
    result = await session.execute(
        select(Task)
        .options(selectinload(Task.assignee), selectinload(Task.project))
        .where(Task.due_date == today, Task.status.in_([s.value for s in ACTIVE_STATUSES]))
        .order_by(Task.priority.asc())
    )
    return list(result.scalars().all())


async def get_project_status(session: AsyncSession, project_id: int) -> dict:
    result = await session.execute(
        select(Task.status, func.count(Task.id))
        .where(Task.project_id == project_id)
        .group_by(Task.status)
    )
    status_counts = {row[0]: row[1] for row in result.all()}
    total = sum(status_counts.values())
    done = status_counts.get(TaskStatus.DONE, 0) + status_counts.get(TaskStatus.APPROVED, 0)

    next_tasks_result = await session.execute(
        select(Task)
        .options(selectinload(Task.assignee))
        .where(
            Task.project_id == project_id,
            Task.status.in_([TaskStatus.WIP.value, TaskStatus.TODO.value]),
        )
        .order_by(Task.priority.asc(), Task.due_date.asc().nullslast())
        .limit(3)
    )
    next_tasks = list(next_tasks_result.scalars().all())

    overdue_result = await session.execute(
        select(func.count(Task.id)).where(
            Task.project_id == project_id,
            Task.due_date < datetime.date.today(),
            Task.status.in_([s.value for s in ACTIVE_STATUSES]),
        )
    )
    overdue_count = overdue_result.scalar() or 0

    return {
        "status_counts": status_counts,
        "total": total,
        "done": done,
        "progress_pct": round(done / total * 100) if total > 0 else 0,
        "next_tasks": next_tasks,
        "overdue_count": overdue_count,
    }


async def get_team_summary(session: AsyncSession) -> dict:
    today = datetime.date.today()
    overdue = await get_overdue_tasks(session)
    hot = await get_tasks_due_today(session)

    open_result = await session.execute(
        select(func.count(Task.id)).where(Task.status.in_([s.value for s in ACTIVE_STATUSES]))
    )
    open_count = open_result.scalar() or 0

    by_user_result = await session.execute(
        select(User.name, func.count(Task.id))
        .join(Task, Task.assignee_id == User.id)
        .where(Task.status.in_([s.value for s in ACTIVE_STATUSES]))
        .group_by(User.name)
        .order_by(func.count(Task.id).desc())
    )
    tasks_by_user = {row[0]: row[1] for row in by_user_result.all()}

    return {
        "overdue": overdue, "hot_today": hot, "open_count": open_count,
        "tasks_by_user": tasks_by_user, "date": today,
    }


async def archive_old_tasks(session: AsyncSession, days: int = 90) -> int:
    """Archive Done/Approved tasks older than N days."""
    cutoff = datetime.datetime.now() - datetime.timedelta(days=days)
    result = await session.execute(
        select(Task).where(
            Task.status.in_([TaskStatus.DONE.value, TaskStatus.APPROVED.value]),
            Task.updated_at < cutoff,
            Task.archived_at == None,  # noqa: E711
        )
    )
    tasks = list(result.scalars().all())
    now = datetime.datetime.now()
    for t in tasks:
        t.archived_at = now
    if tasks:
        await session.commit()
    return len(tasks)


async def get_all_tasks(session: AsyncSession, project_id: int | None = None,
                        status: TaskStatus | None = None,
                        assignee_id: int | None = None,
                        include_archived: bool = False,
                        priority: int | None = None,
                        is_bug: bool | None = None,
                        overdue: bool = False) -> list[Task]:
    q = (
        select(Task)
        .options(selectinload(Task.assignee), selectinload(Task.project), selectinload(Task.comments))
        .order_by(Task.status, Task.priority.asc(), Task.due_date.asc().nullslast())
    )
    if not include_archived:
        q = q.where(Task.archived_at == None)  # noqa: E711
    if project_id:
        q = q.where(Task.project_id == project_id)
    if status:
        q = q.where(Task.status == status)
    if assignee_id:
        q = q.where(Task.assignee_id == assignee_id)
    if priority is not None:
        q = q.where(Task.priority == priority)
    if is_bug is not None:
        q = q.where(Task.is_bug == is_bug)
    if overdue:
        q = q.where(Task.due_date < datetime.date.today())
    result = await session.execute(q)
    return list(result.scalars().all())


async def create_task(session: AsyncSession, title: str, project_id: int | None = None,
                      description: str | None = None, status: TaskStatus = TaskStatus.BACKLOG,
                      priority: int = DEFAULT_PRIORITY, is_bug: bool = False,
                      assignee_id: int | None = None, creator_id: int | None = None,
                      estimated_hours: float | None = None,
                      due_date: datetime.date | None = None) -> Task:
    task = Task(
        title=title, project_id=project_id, description=description,
        status=status, priority=priority, is_bug=is_bug,
        assignee_id=assignee_id, creator_id=creator_id,
        estimated_hours=estimated_hours, due_date=due_date,
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


async def update_task(session: AsyncSession, task_id: int, **kwargs) -> Task | None:
    task = await get_task_by_id(session, task_id)
    if not task:
        return None
    for key, value in kwargs.items():
        if hasattr(task, key):
            setattr(task, key, value)
    task.updated_at = datetime.datetime.now()
    await session.commit()
    await session.refresh(task)
    return task


async def delete_task(session: AsyncSession, task_id: int) -> bool:
    task = await get_task_by_id(session, task_id)
    if not task:
        return False
    # Удаляем notification_log вручную (FK без CASCADE в существующих БД)
    await session.execute(
        delete(NotificationLog).where(NotificationLog.task_id == task_id)
    )
    await session.delete(task)
    await session.commit()
    return True


async def update_task_status(session: AsyncSession, task_id: int, new_status: TaskStatus) -> Task | None:
    return await update_task(session, task_id, status=new_status)


# ── Comments & attachments ──

async def add_comment(session: AsyncSession, task_id: int, user_id: int | None,
                      text: str | None) -> TaskComment:
    comment = TaskComment(task_id=task_id, user_id=user_id, text=text)
    session.add(comment)
    await session.commit()
    await session.refresh(comment)
    return comment


async def add_attachment(session: AsyncSession, comment_id: int, filename: str,
                         stored_name: str, content_type: str | None) -> Attachment:
    att = Attachment(comment_id=comment_id, filename=filename,
                     stored_name=stored_name, content_type=content_type)
    session.add(att)
    await session.commit()
    await session.refresh(att)
    return att


# ── Notifications ──

async def log_notification(session: AsyncSession, user_id: int, task_id: int,
                           notification_type: str) -> NotificationLog:
    log = NotificationLog(user_id=user_id, task_id=task_id, notification_type=notification_type,
                          sent_at=datetime.datetime.now())
    session.add(log)
    await session.commit()
    return log


async def was_notified_today(session: AsyncSession, user_id: int, task_id: int,
                             notification_type: str) -> bool:
    today_start = datetime.datetime.combine(datetime.date.today(), datetime.time.min)
    result = await session.execute(
        select(func.count(NotificationLog.id)).where(
            NotificationLog.user_id == user_id,
            NotificationLog.task_id == task_id,
            NotificationLog.notification_type == notification_type,
            NotificationLog.sent_at >= today_start,
        )
    )
    return (result.scalar() or 0) > 0


# ── Settings ──

async def get_setting(session: AsyncSession, key: str) -> str:
    result = await session.execute(select(Settings).where(Settings.key == key))
    row = result.scalar_one_or_none()
    return row.value if row else Settings.DEFAULTS.get(key, "")


async def get_all_settings(session: AsyncSession) -> dict[str, str]:
    result = await session.execute(select(Settings))
    saved = {row.key: row.value for row in result.scalars().all()}
    merged = dict(Settings.DEFAULTS)
    merged.update(saved)
    return merged


async def set_setting(session: AsyncSession, key: str, value: str):
    result = await session.execute(select(Settings).where(Settings.key == key))
    row = result.scalar_one_or_none()
    if row:
        row.value = value
    else:
        session.add(Settings(key=key, value=value))
    await session.commit()
