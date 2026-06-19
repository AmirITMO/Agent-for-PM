import logging
import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from telegram import Bot

from agent3_pm.config import config
from agent3_pm.database import AsyncSessionLocal
from agent3_pm import repository as repo
from agent3_pm.formatter import format_morning_summary, format_deadline_warning

logger = logging.getLogger(__name__)


async def send_morning_summary(bot: Bot):
    logger.info("Sending morning summary")
    async with AsyncSessionLocal() as session:
        managers = await repo.get_managers(session)
        if not managers:
            logger.warning("No managers found, skipping morning summary")
            return

        summary = await repo.get_team_summary(session)
        text = format_morning_summary(summary, config.WEB_BASE_URL)

        for manager in managers:
            if not manager.telegram_id:
                continue
            try:
                await bot.send_message(
                    chat_id=manager.telegram_id,
                    text=text,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
                logger.info(f"Morning summary sent to {manager.name} ({manager.telegram_id})")
            except Exception:
                logger.exception(f"Failed to send morning summary to {manager.name}")


async def check_deadlines(bot: Bot):
    logger.info("Checking deadlines")
    async with AsyncSessionLocal() as session:
        hot_tasks = await repo.get_hot_tasks(session, config.DEADLINE_WARNING_HOURS)
        overdue_tasks = await repo.get_overdue_tasks(session)

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
                session, task.assignee.id, task.id, notification_type
            )
            if already_sent:
                continue

            text = format_deadline_warning(task)
            web_link = f"\n\n{config.WEB_BASE_URL}/task/{task.id}"

            try:
                await bot.send_message(
                    chat_id=task.assignee.telegram_id,
                    text=text + web_link,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
                await repo.log_notification(session, task.assignee.id, task.id, notification_type)
                logger.info(f"Deadline warning sent to {task.assignee.name} for task #{task.id}")
            except Exception:
                logger.exception(f"Failed to send deadline warning for task #{task.id}")


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

    scheduler.add_job(
        archive_tasks,
        trigger=CronTrigger(hour=3, minute=0, timezone=tz),
        id="archive_tasks",
        name="Archive Old Tasks",
        replace_existing=True,
    )

    return scheduler


async def archive_tasks():
    logger.info("Running task archiver")
    async with AsyncSessionLocal() as session:
        count = await repo.archive_old_tasks(session, days=90)
        if count:
            logger.info(f"Archived {count} tasks")
