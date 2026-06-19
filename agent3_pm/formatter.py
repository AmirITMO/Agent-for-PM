import datetime
from agent3_pm.models import Task, TaskStatus


STATUS_LABEL = {
    TaskStatus.BACKLOG: "Бэклог",
    TaskStatus.PLANNING: "Планирование",
    TaskStatus.TODO: "К выполнению",
    TaskStatus.WIP: "В работе",
    TaskStatus.DONE: "Готово",
    TaskStatus.APPROVED: "Принято",
    TaskStatus.HOLD: "На паузе",
}


def _status_label(status) -> str:
    return STATUS_LABEL.get(status, status.value if hasattr(status, "value") else str(status))


def format_task_short(task: Task) -> str:
    parts = []
    if task.is_bug:
        parts.append("[Баг]")
    elif task.priority == 0:
        parts.append("[P0]")

    parts.append(task.title)

    if task.estimated_hours:
        parts.append(f"({task.estimated_hours}ч)")

    if task.due_date:
        if task.is_overdue:
            parts.append(f"— просрочено ({task.due_date.strftime('%d.%m')})")
        elif task.is_due_today:
            parts.append("— дедлайн сегодня")
        else:
            parts.append(f"— до {task.due_date.strftime('%d.%m')}")

    if task.assignee:
        parts.append(f"-> {task.assignee.name}")

    return " ".join(parts)


def format_today_tasks(tasks: list[Task]) -> str:
    if not tasks:
        return "На сегодня задач нет."

    overdue = [t for t in tasks if t.is_overdue]
    wip = [t for t in tasks if t.status == TaskStatus.WIP and not t.is_overdue]
    todo = [t for t in tasks if t.status == TaskStatus.TODO and not t.is_overdue]
    other = [t for t in tasks if t not in overdue and t not in wip and t not in todo]

    lines = [f"<b>Задачи на сегодня ({len(tasks)}):</b>\n"]
    if overdue:
        lines.append("<b>Просрочено:</b>")
        lines.extend(format_task_short(t) for t in overdue)
        lines.append("")
    if wip:
        lines.append("<b>В работе:</b>")
        lines.extend(format_task_short(t) for t in wip)
        lines.append("")
    if todo:
        lines.append("<b>К выполнению:</b>")
        lines.extend(format_task_short(t) for t in todo)
        lines.append("")
    if other:
        lines.append("<b>Прочие:</b>")
        lines.extend(format_task_short(t) for t in other)
    return "\n".join(lines).strip()


def format_overdue_block(overdue: list[Task], hot: list[Task], bugs: list[Task]) -> str:
    """Просрочки пользователя + скоро просрочатся + баги/срочные."""
    lines = []
    if overdue:
        lines.append(f"<b>Просрочено ({len(overdue)}):</b>")
        lines.extend(format_task_short(t) for t in overdue)
        lines.append("")
    if hot:
        lines.append(f"<b>Скоро просрочатся ({len(hot)}):</b>")
        lines.extend(format_task_short(t) for t in hot)
        lines.append("")
    if bugs:
        lines.append(f"<b>Баги и срочные ({len(bugs)}):</b>")
        lines.extend(format_task_short(t) for t in bugs)
    if not lines:
        return "Просрочек, горящих задач и багов нет."
    return "\n".join(lines).strip()


def format_overdue_list(tasks: list[Task]) -> str:
    if not tasks:
        return "Просроченных задач нет."
    lines = [f"<b>Просроченные задачи ({len(tasks)}):</b>\n"]
    lines.extend(format_task_short(t) for t in tasks)
    return "\n".join(lines)


def format_task_detail(task: Task, web_url: str = "") -> str:
    lines = [
        f"<b>{task.title}</b>",
        "",
        f"Статус: {_status_label(task.status)}",
        f"Приоритет: {'Баг' if task.is_bug else 'P' + str(task.priority)}",
    ]
    if task.project:
        lines.append(f"Проект: {task.project.name}")
    if task.assignee:
        lines.append(f"Исполнитель: {task.assignee.name}")
    if task.estimated_hours:
        lines.append(f"Оценка: {task.estimated_hours}ч")
    if task.due_date:
        overdue_mark = " (ПРОСРОЧЕНО)" if task.is_overdue else ""
        lines.append(f"Дедлайн: {task.due_date.strftime('%d.%m.%Y')}{overdue_mark}")
    if task.description:
        lines.append(f"\n{task.description}")
    return "\n".join(lines)


def format_project_status(project_name: str, status: dict) -> str:
    lines = [
        f"<b>Проект: {project_name}</b>\n",
        f"Прогресс: {status['done']}/{status['total']} ({status['progress_pct']}%)",
    ]
    bar_len = 20
    filled = round(status['progress_pct'] / 100 * bar_len)
    lines.append(f"[{'█' * filled}{'░' * (bar_len - filled)}]")
    lines.append("")
    for st_enum, label in STATUS_LABEL.items():
        count = status['status_counts'].get(st_enum, 0)
        if count:
            lines.append(f"  {label}: {count}")
    if status['overdue_count']:
        lines.append(f"\nПросрочено: {status['overdue_count']}")
    if status['next_tasks']:
        lines.append("\n<b>Следующие шаги:</b>")
        lines.extend(format_task_short(t) for t in status['next_tasks'])
    return "\n".join(lines)


def format_morning_summary(summary: dict, web_url: str = "") -> str:
    lines = [
        f"<b>Утренняя сводка — {summary['date'].strftime('%d.%m.%Y')}</b>\n",
        f"Открытых задач: {summary['open_count']}",
    ]
    if summary['overdue']:
        lines.append(f"\n<b>Просрочено ({len(summary['overdue'])}):</b>")
        lines.extend(format_task_short(t) for t in summary['overdue'][:10])
        if len(summary['overdue']) > 10:
            lines.append(f"  ...и ещё {len(summary['overdue']) - 10}")
    if summary['hot_today']:
        lines.append(f"\n<b>Горящие сегодня ({len(summary['hot_today'])}):</b>")
        lines.extend(format_task_short(t) for t in summary['hot_today'][:10])
    if summary['tasks_by_user']:
        lines.append("\n<b>Задачи по сотрудникам:</b>")
        for name, count in summary['tasks_by_user'].items():
            lines.append(f"  {name}: {count}")
    return "\n".join(lines)


def format_deadline_warning(task: Task) -> str:
    days_left = (task.due_date - datetime.date.today()).days if task.due_date else None
    if days_left is not None and days_left < 0:
        urgency = f"Просрочено на {abs(days_left)} дн."
    elif days_left == 0:
        urgency = "Дедлайн СЕГОДНЯ"
    elif days_left == 1:
        urgency = "Дедлайн ЗАВТРА"
    else:
        urgency = f"До дедлайна: {days_left} дн."
    return f"{urgency}\n{task.title}\nСтатус: {_status_label(task.status)}"
