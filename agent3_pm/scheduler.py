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
                    lines.append(f"  {t.title} — {t.due_date.strftime('%d.%m.%Y')} {base}/task/{t.id}")
            else:
                lines.append("Просрочки — нет")
            lines.append("")
            if hot:
                lines.append(f"<b>Дедлайны скоро ({len(hot)}):</b>")
                for t in hot:
                    dd = t.due_date.strftime('%d.%m.%Y') if t.due_date else ""
                    lines.append(f"  {t.title} — {dd} {base}/task/{t.id}")
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
    """Check deadlines and notify assignees."""
    logger.info("Checking deadlines")
    async with AsyncSessionLocal() as session:
        hot_tasks = await repo.get_hot_tasks(session, config.DEADLINE_WARNING_HOURS)
        overdue_tasks = await repo.get_overdue_tasks(session)
        base = config.WEB_BASE_URL.rstrip("/")

        all_tasks = []
        seen_ids = set()
        for t in overdue_tasks + hot_tasks:
            if t.id not in seen_ids:
                all_tasks.append(t)
                seen_ids.add(t.id)

        for task in all_tasks:
            if not task.assignee or not task.assignee.telegram_id:
                continue

            notification_type = "overdue" if task.is_overdue else "deadline_warning"
            already_sent = await repo.was_notified_today(
                session, task.assignee.id, task.id, notification_type)
            if already_sent:
                continue

            text = format_deadline_warning(task) + f"\n\n{base}/task/{task.id}"

            try:
                await bot.send_message(chat_id=task.assignee.telegram_id, text=text,
                                       parse_mode="HTML", disable_web_page_preview=True)
                await repo.log_notification(session, task.assignee.id, task.id, notification_type)
                logger.info(f"Deadline warning sent to {task.assignee.name} for task #{task.id}")
            except Exception:
                logger.exception(f"Failed to send deadline warning for task #{task.id}")


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
                lines.append(f"{t.title} — {dd}{overdue}\n{base}/task/{t.id}")

            try:
                await bot.send_message(chat_id=user.telegram_id, text="\n".join(lines),
                                       disable_web_page_preview=True)
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

    scheduler.add_job(
        archive_tasks,
        trigger=CronTrigger(hour=3, minute=0, timezone=tz),
        id="archive_tasks",
        name="Archive Old Tasks",
        replace_existing=True,
    )

    return scheduler
