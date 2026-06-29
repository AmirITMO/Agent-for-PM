import logging
import time
import hmac
import hashlib
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from telegram import Bot

from agent3_pm.config import config
from agent3_pm.database import AsyncSessionLocal
from agent3_pm import repository as repo
from agent3_pm.formatter import format_morning_summary, format_deadline_warning, format_evening_summary
from agent3_pm.models import ACTIVE_STATUSES, TaskStatus

logger = logging.getLogger(__name__)


def _make_enter_token(user_id: int) -> str:
    ts = str(int(time.time()))
    sig = hmac.new(config.SECRET_KEY.encode(),
                   f"{user_id}:{ts}".encode(),
                   hashlib.sha256).hexdigest()[:16]
    return f"{ts}.{sig}"


def _enter_link(user_id: int, path: str, label: str = "ссылка") -> str:
    base = config.WEB_BASE_URL.rstrip("/")
    tok = _make_enter_token(user_id)
    return f'<a href="{base}/enter/{user_id}?tok={tok}&amp;next={path}">{label}</a>'


async def send_morning_summary(bot: Bot):
    """Send morning summary: detailed per-employee report to Level 1, personal to everyone."""
    logger.info("Sending morning summaries")
    import datetime
    today = datetime.date.today()

    async with AsyncSessionLocal() as session:
        all_users = await repo.get_all_users(session)
        managers = await repo.get_managers(session)
        base = config.WEB_BASE_URL.rstrip("/")

        STATUS_LABELS = {
            TaskStatus.BACKLOG: "Бэклог", TaskStatus.PLANNING: "Планирование",
            TaskStatus.TODO: "К выполнению", TaskStatus.WIP: "В работе",
            TaskStatus.DONE: "Готово", TaskStatus.APPROVED: "Принято",
            TaskStatus.HOLD: "На паузе",
        }

        # Build detailed report for Level 1
        overdue_all = await repo.get_overdue_tasks(session)
        hot_all = await repo.get_hot_tasks(session, config.DEADLINE_WARNING_HOURS)

        header = [f"<b>Утренняя сводка — {today.strftime('%d.%m.%Y')}</b>\n"]
        if overdue_all:
            header.append(f"<b>Просрочки ({len(overdue_all)}):</b>")
            for t in overdue_all[:10]:
                days = (today - t.due_date).days if t.due_date else 0
                who = t.assignee.name if t.assignee else "не назначен"
                dn = t.display_number or t.id
                header.append(f"  #{dn} {t.title} — {who} — {days} дн.")
        if hot_all:
            header.append(f"\n<b>Горящие дедлайны ({len(hot_all)}):</b>")
            for t in hot_all[:10]:
                who = t.assignee.name if t.assignee else "не назначен"
                dd = t.due_date.strftime('%d.%m') if t.due_date else ""
                dn = t.display_number or t.id
                header.append(f"  #{dn} {t.title} — {who} — {dd}")

        blocks = ["\n".join(header)]

        for u in sorted(all_users, key=lambda x: x.name):
            tasks = await repo.get_all_tasks(session, assignee_id=u.id)
            active = [t for t in tasks if not t.archived_at and t.status in ACTIVE_STATUSES]
            if not active:
                continue

            by_status = {}
            for t in active:
                sl = STATUS_LABELS.get(t.status, str(t.status))
                by_status.setdefault(sl, []).append(t)

            overdue_count = sum(1 for t in active if t.is_overdue)
            lines = [f"\n<b>{u.name}</b> ({u.position or '—'}) — {len(active)} задач" +
                     (f", просрочено: {overdue_count}" if overdue_count else "")]

            for status_label, status_tasks in by_status.items():
                lines.append(f"  <b>{status_label}:</b>")
                for t in status_tasks:
                    dn = t.display_number or t.id
                    bug = "[Баг] " if t.is_bug else ""
                    overdue_mark = " ⚠️" if t.is_overdue else ""
                    dd = f" — до {t.due_date.strftime('%d.%m')}" if t.due_date else ""
                    lines.append(f"    #{dn} {bug}P{t.priority} {t.title}{dd}{overdue_mark}")

            blocks.append("\n".join(lines))

        # Send to Level 1 — split by 4000 chars
        for manager in managers:
            if not manager.telegram_id:
                continue
            chunk, size = [], 0
            for b in blocks:
                if size + len(b) > 3500 and chunk:
                    try:
                        await bot.send_message(chat_id=manager.telegram_id, text="\n".join(chunk),
                                               parse_mode="HTML", disable_web_page_preview=True)
                    except Exception:
                        logger.exception(f"Failed to send summary chunk to {manager.name}")
                    chunk, size = [], 0
                chunk.append(b)
                size += len(b) + 2
            if chunk:
                try:
                    await bot.send_message(chat_id=manager.telegram_id, text="\n".join(chunk),
                                           parse_mode="HTML", disable_web_page_preview=True)
                    logger.info(f"Full summary sent to {manager.name}")
                except Exception:
                    logger.exception(f"Failed to send summary to {manager.name}")

        # Everyone — personal report
        for user in all_users:
            if not user.telegram_id:
                continue
            my_tasks = await repo.get_all_tasks(session, assignee_id=user.id)
            active = [t for t in my_tasks if not t.archived_at and t.status in ACTIVE_STATUSES]
            if not active:
                continue

            overdue = [t for t in active if t.is_overdue]
            lines = [f"<b>Твои задачи на сегодня ({len(active)}):</b>\n"]
            if overdue:
                lines.append(f"<b>Просрочено ({len(overdue)}):</b>")
                for t in overdue:
                    dn = t.display_number or t.id
                    lines.append(f'  #{dn} {t.title} — {t.due_date.strftime("%d.%m.%Y")} {_enter_link(user.id, f"/task/{t.id}")}')
                lines.append("")

            for t in active:
                if t not in overdue:
                    dn = t.display_number or t.id
                    sl = STATUS_LABELS.get(t.status, "")
                    dd = f" — до {t.due_date.strftime('%d.%m')}" if t.due_date else ""
                    lines.append(f'  #{dn} {t.title} — {sl}{dd} {_enter_link(user.id, f"/task/{t.id}")}')

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
                    f"\n\n{_enter_link(task.assignee.id, f'/task/{task.id}', 'Открыть задачу')}")

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
                lines.append(f'{t.title} — {dd}{overdue} {_enter_link(user.id, f"/task/{t.id}")}')

            try:
                await bot.send_message(chat_id=user.telegram_id, text="\n".join(lines),
                                       parse_mode="HTML", disable_web_page_preview=True)
                logger.info(f"Reminder sent to {user.name}")
            except Exception:
                logger.exception(f"Failed reminder to {user.name}")


async def send_evening_summary(bot: Bot):
    """Send evening summary to Level 1: per-employee report of the day."""
    logger.info("Sending evening summaries")
    import datetime
    today = datetime.date.today()

    STATUS_LABELS = {
        TaskStatus.BACKLOG: "Бэклог", TaskStatus.PLANNING: "Планирование",
        TaskStatus.TODO: "К выполнению", TaskStatus.WIP: "В работе",
        TaskStatus.DONE: "Готово", TaskStatus.APPROVED: "Принято",
        TaskStatus.HOLD: "На паузе",
    }

    async with AsyncSessionLocal() as session:
        all_users = await repo.get_all_users(session)
        managers = await repo.get_managers(session)

        all_completed = []
        all_in_work = []
        all_done_earlier = []
        today_start = datetime.datetime.combine(today, datetime.time.min)
        completed_ids = set()

        for u in sorted(all_users, key=lambda x: x.name):
            completed = await repo.get_tasks_completed_today(session, user_id=u.id)
            for t in completed:
                dn = t.display_number or t.id
                completed_ids.add(t.id)
                all_completed.append(f"  {u.name} — #{dn} {t.title} — {_enter_link(u.id, f'/task/{t.id}', 'открыть')}")

            tasks = await repo.get_all_tasks(session, assignee_id=u.id)
            active = [t for t in tasks if not t.archived_at and t.status in ACTIVE_STATUSES]
            for t in active:
                dn = t.display_number or t.id
                sl = STATUS_LABELS.get(t.status, str(t.status))
                all_in_work.append(f"  {u.name} — #{dn} {t.title} — {sl} — P{t.priority} — {_enter_link(u.id, f'/task/{t.id}', 'открыть')}")

            done_old = [t for t in tasks if not t.archived_at
                        and t.status in (TaskStatus.DONE, TaskStatus.APPROVED)
                        and t.id not in completed_ids]
            for t in done_old:
                dn = t.display_number or t.id
                proj = t.project.name if t.project else "—"
                all_done_earlier.append(f"  {u.name} — #{dn} {t.title} — {proj} — {_enter_link(u.id, f'/task/{t.id}', 'открыть')}")

        blocks = [f"<b>Вечерняя сводка — {today.strftime('%d.%m.%Y')}</b>"]

        if all_completed:
            blocks.append(f"\n<b>Что сделано сегодня ({len(all_completed)}):</b>")
            blocks.append("\n".join(all_completed))
        else:
            blocks.append("\n<b>Что сделано сегодня:</b>\n  Нет выполненных задач за сегодня.")

        if all_in_work:
            blocks.append(f"\n<b>Сейчас в работе ({len(all_in_work)}):</b>")
            blocks.append("\n".join(all_in_work))
        else:
            blocks.append("\n<b>Сейчас в работе:</b>\n  Нет активных задач.")

        if all_done_earlier:
            blocks.append(f"\n<b>Выполненные ранее ({len(all_done_earlier)}):</b>")
            blocks.append("\n".join(all_done_earlier))

        # Send to managers — split by Telegram limit
        for manager in managers:
            if not manager.telegram_id:
                continue
            chunk, size = [], 0
            for b in blocks:
                if size + len(b) > 3500 and chunk:
                    try:
                        await bot.send_message(chat_id=manager.telegram_id, text="\n".join(chunk),
                                               parse_mode="HTML", disable_web_page_preview=True)
                    except Exception:
                        logger.exception(f"Failed evening chunk to {manager.name}")
                    chunk, size = [], 0
                chunk.append(b)
                size += len(b) + 2
            if chunk:
                try:
                    await bot.send_message(chat_id=manager.telegram_id, text="\n".join(chunk),
                                           parse_mode="HTML", disable_web_page_preview=True)
                    logger.info(f"Evening summary sent to {manager.name}")
                except Exception:
                    logger.exception(f"Failed evening summary to {manager.name}")


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
        send_evening_summary,
        trigger=CronTrigger(
            hour=config.EVENING_SUMMARY_HOUR,
            minute=config.EVENING_SUMMARY_MINUTE,
            timezone=tz,
        ),
        args=[bot],
        id="evening_summary",
        name="Evening Summary",
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

    from agent3_pm.kb_watcher import check_kb_updates
    scheduler.add_job(
        check_kb_updates,
        trigger=IntervalTrigger(seconds=30),
        args=[bot],
        id="kb_watcher",
        name="Knowledge Base Watcher",
        replace_existing=True,
        max_instances=1,
    )

    scheduler.add_job(
        archive_tasks,
        trigger=CronTrigger(hour=3, minute=0, timezone=tz),
        id="archive_tasks",
        name="Archive Old Tasks",
        replace_existing=True,
    )

    async def sync_settings_to_scheduler():
        """Re-read settings from DB and update scheduler job times."""
        try:
            async with AsyncSessionLocal() as session:
                settings = await repo.get_all_settings(session)
            m_hour = int(settings.get("morning_summary_hour", "9"))
            m_min = int(settings.get("morning_summary_minute", "0"))
            e_hour = int(settings.get("evening_summary_hour", "19"))
            e_min = int(settings.get("evening_summary_minute", "0"))
            tz_name = settings.get("timezone", "Europe/Moscow")
            stz = ZoneInfo(tz_name)

            job_m = scheduler.get_job("morning_summary")
            if job_m:
                cur = job_m.trigger
                if cur.fields[5].expressions[0].first != m_hour or cur.fields[6].expressions[0].first != m_min:
                    scheduler.reschedule_job("morning_summary",
                        trigger=CronTrigger(hour=m_hour, minute=m_min, timezone=stz))
                    logger.info(f"Morning summary rescheduled to {m_hour}:{m_min:02d}")

            job_e = scheduler.get_job("evening_summary")
            if job_e:
                cur = job_e.trigger
                if cur.fields[5].expressions[0].first != e_hour or cur.fields[6].expressions[0].first != e_min:
                    scheduler.reschedule_job("evening_summary",
                        trigger=CronTrigger(hour=e_hour, minute=e_min, timezone=stz))
                    logger.info(f"Evening summary rescheduled to {e_hour}:{e_min:02d}")
        except Exception:
            logger.exception("Failed to sync settings to scheduler")

    scheduler.add_job(
        sync_settings_to_scheduler,
        trigger=IntervalTrigger(minutes=5),
        id="sync_settings",
        name="Sync Settings",
        replace_existing=True,
    )

    return scheduler
