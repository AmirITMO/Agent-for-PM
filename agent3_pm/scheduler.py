import logging
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from telegram import Bot

from agent3_pm.config import config
from agent3_pm.database import AsyncSessionLocal
from agent3_pm import repository as repo
from agent3_pm.formatter import format_morning_summary, format_deadline_warning
from agent3_pm.models import ACTIVE_STATUSES

logger = logging.getLogger(__name__)


async def send_morning_summary(bot: Bot):
    """Send morning summary: full report to Level 1, personal to everyone."""
    logger.info("Sending morning summaries")
    async with AsyncSessionLocal() as session:
        all_users = await repo.get_all_users(session)
        managers = await repo.get_managers(session)
        summary = await repo.get_team_summary(session)
        base = config.WEB_BASE_URL.rstrip("/")

        # Level 1 — full team summary
        full_text = format_morning_summary(summary, base)
        for manager in managers:
            if not manager.telegram_id:
                continue
            try:
                await bot.send_message(chat_id=manager.telegram_id, text=full_text,
                                       parse_mode="HTML", disable_web_page_preview=True)
                logger.info(f"Full summary sent to {manager.name}")
            except Exception:
                logger.exception(f"Failed to send summary to {manager.name}")

        # Everyone with telegram — personal report
        for user in all_users:
            if not user.telegram_id:
                continue
            my_tasks = await repo.get_all_tasks(session, assignee_id=user.id)
            active = [t for t in my_tasks if t.status in ACTIVE_STATUSES]
            if not active:
                continue

            overdue = await repo.get_overdue_tasks(session, user_id=user.id)
            hot = await repo.get_hot_tasks(session, config.DEADLINE_WARNING_HOURS, user_id=user.id)

            lines = [f"<b>Отчет по твоим задачам:</b>\n"]
            if overdue:
                lines.append(f"<b>Просрочки ({len(overdue)}):</b>")
                for t in overdue:
                    lines.append(f'  {t.title} — {t.due_date.strftime("%d.%m.%Y")} <a href="{base}/enter/{user.id}?next=/task/{t.id}">ссылка</a>')
            else:
                lines.append("Просрочки — нет")
            lines.append("")
            if hot:
                lines.append(f"<b>Дедлайны скоро ({len(hot)}):</b>")
                for t in hot:
                    dd = t.due_date.strftime('%d.%m.%Y') if t.due_date else ""
                    lines.append(f'  {t.title} — {dd} <a href="{base}/enter/{user.id}?next=/task/{t.id}">ссылка</a>')
            else:
                lines.append("Ближайших дедлайнов нет")

            text = "\n".join(lines)
            try:
                await bot.send_message(chat_id=user.telegram_id, text=text,
                                       parse_mode="HTML", disable_web_page_preview=True)
                logger.info(f"Personal summary sent to {user.name}")
            except Exception:
                logger.exception(f"Failed to send personal summary to {user.name}")


async def check_deadlines(bot: Bot):
    """Check deadlines with escalation: the more overdue, the more reminders."""
    logger.info("Checking deadlines")
    import datetime
    today = datetime.date.today()

    async with AsyncSessionLocal() as session:
        hot_tasks = await repo.get_hot_tasks(session, config.DEADLINE_WARNING_HOURS)
        overdue_tasks = await repo.get_overdue_tasks(session)
        managers = await repo.get_managers(session)
        base = config.WEB_BASE_URL.rstrip("/")

        all_tasks = []
        seen_ids = set()
        for t in overdue_tasks + hot_tasks:
            if t.id not in seen_ids:
                all_tasks.append(t)
                seen_ids.add(t.id)

        escalated = []  # для менеджеров: просрочки >2 дней

        for task in all_tasks:
            if not task.assignee or not task.assignee.telegram_id:
                continue

            if task.is_overdue and task.due_date:
                days_over = (today - task.due_date).days
                # Уникальный тип уведомления per day → позволяет 1 напоминание в день
                # на каждый «уровень» просрочки
                ntype = f"overdue_d{days_over}"
                if days_over >= 3:
                    escalated.append(task)
            else:
                ntype = "deadline_warning"
                days_over = 0

            already_sent = await repo.was_notified_today(
                session, task.assignee.id, task.id, ntype)
            if already_sent:
                continue

            if task.is_overdue and task.due_date:
                days_over_actual = (today - task.due_date).days
                if days_over_actual >= 3:
                    urgency = f"Просрочено {days_over_actual} дн.!"
                elif days_over_actual >= 1:
                    urgency = f"Просрочено {days_over_actual} дн."
                else:
                    urgency = "Просрочено"
            elif task.due_date and task.due_date == today:
                urgency = "Дедлайн СЕГОДНЯ"
            elif task.due_date and (task.due_date - today).days == 1:
                urgency = "Дедлайн ЗАВТРА"
            else:
                days_left = (task.due_date - today).days if task.due_date else 0
                urgency = f"До дедлайна {days_left} дн."

            text = (f"<b>{urgency}</b>\n\n{task.title}"
                    f"\nP{task.priority}"
                    f'\n\n<a href="{base}/enter/{task.assignee.id}?next=/task/{task.id}">Открыть задачу</a>')

            try:
                await bot.send_message(chat_id=task.assignee.telegram_id, text=text,
                                       parse_mode="HTML", disable_web_page_preview=True)
                await repo.log_notification(session, task.assignee.id, task.id, ntype)
                logger.info(f"Deadline ({ntype}) sent to {task.assignee.name} for task #{task.id}")
            except Exception:
                logger.exception(f"Failed to send deadline for task #{task.id}")

        # Эскалация менеджерам: задачи просроченные >2 дней
        if escalated:
            lines = ["<b>Эскалация: критические просрочки</b>\n"]
            for t in escalated:
                days = (today - t.due_date).days
                who = t.assignee.name if t.assignee else "не назначен"
                lines.append(f"• {t.title} — {who} — {days} дн. просрочки"
                             f' <a href="{base}/task/{t.id}">открыть</a>')
            esc_text = "\n".join(lines)
            for m in managers:
                if not m.telegram_id:
                    continue
                already = await repo.was_notified_today(session, m.id, 0, "escalation_daily")
                if already:
                    continue
                try:
                    await bot.send_message(chat_id=m.telegram_id, text=esc_text,
                                           parse_mode="HTML", disable_web_page_preview=True)
                    await repo.log_notification(session, m.id, 0, "escalation_daily")
                    logger.info(f"Escalation sent to manager {m.name}")
                except Exception:
                    logger.exception(f"Failed escalation to {m.name}")


async def send_deadline_reminders(bot: Bot):
    """Send deadline reminders to ALL users (day/evening)."""
    logger.info("Sending deadline reminders")
    async with AsyncSessionLocal() as session:
        all_users = await repo.get_all_users(session)
        base = config.WEB_BASE_URL.rstrip("/")

        for user in all_users:
            if not user.telegram_id:
                continue
            tasks = await repo.get_all_tasks(session, assignee_id=user.id)
            active = [t for t in tasks if t.status in ACTIVE_STATUSES and t.due_date]
            if not active:
                continue

            lines = ["Напоминание о дедлайнах:\n"]
            for t in sorted(active, key=lambda x: x.due_date):
                dd = t.due_date.strftime('%d.%m.%Y')
                overdue = " (ПРОСРОЧЕНО)" if t.is_overdue else ""
                lines.append(f'{t.title} — {dd}{overdue} <a href="{base}/enter/{user.id}?next=/task/{t.id}">ссылка</a>')

            try:
                await bot.send_message(chat_id=user.telegram_id, text="\n".join(lines),
                                       parse_mode="HTML", disable_web_page_preview=True)
                logger.info(f"Reminder sent to {user.name}")
            except Exception:
                logger.exception(f"Failed reminder to {user.name}")


async def archive_tasks():
    logger.info("Running task archiver")
    async with AsyncSessionLocal() as session:
        count = await repo.archive_old_tasks(session, days=90)
        if count:
            logger.info(f"Archived {count} tasks")


def create_scheduler(bot: Bot) -> AsyncIOScheduler:
    tz = ZoneInfo(config.TIMEZONE)
    scheduler = AsyncIOScheduler(timezone=tz)

    scheduler.add_job(
        send_morning_summary,
        trigger=CronTrigger(
            hour=config.MORNING_SUMMARY_HOUR,
            minute=config.MORNING_SUMMARY_MINUTE,
            timezone=tz,
        ),
        args=[bot],
        id="morning_summary",
        name="Morning Summary",
        replace_existing=True,
    )

    scheduler.add_job(
        check_deadlines,
        trigger=IntervalTrigger(minutes=config.DEADLINE_CHECK_INTERVAL_MINUTES),
        args=[bot],
        id="deadline_check",
        name="Deadline Check",
        replace_existing=True,
    )

    # Deadline reminders 3x/day (morning handled by send_morning_summary)
    for hour, label in [(13, "day"), (18, "evening")]:
        scheduler.add_job(
            send_deadline_reminders,
            trigger=CronTrigger(hour=hour, minute=0, timezone=tz),
            args=[bot],
            id=f"reminder_{label}",
            name=f"Deadline Reminder ({label})",
            replace_existing=True,
        )

    from agent3_pm.github_watcher import check_github_bugs
    scheduler.add_job(
        check_github_bugs,
        trigger=IntervalTrigger(minutes=10),
        id="github_bugs",
        name="GitHub Bugs Watcher",
        replace_existing=True,
    )

    from agent3_pm.kb_watcher import check_kb_updates
    scheduler.add_job(
        check_kb_updates,
        trigger=IntervalTrigger(minutes=5),
        args=[bot],
        id="kb_watcher",
        name="Knowledge Base Watcher",
        replace_existing=True,
    )

    scheduler.add_job(
        archive_tasks,
        trigger=CronTrigger(hour=3, minute=0, timezone=tz),
        id="archive_tasks",
        name="Archive Old Tasks",
        replace_existing=True,
    )

    return scheduler
