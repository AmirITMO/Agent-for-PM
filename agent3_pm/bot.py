import json
import os
import tempfile
import logging
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton,
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters,
)
from telegram.constants import ParseMode

from agent3_pm.config import config
from agent3_pm.database import AsyncSessionLocal
from agent3_pm.models import POSITIONS, TaskStatus, DEFAULT_PRIORITY
from agent3_pm.repository import (
    get_user_by_telegram_id, get_user_by_telegram_username,
    register_user, bind_telegram_id,
    get_tasks_for_user_today, get_overdue_tasks, get_hot_tasks, get_user_bugs,
    get_all_tasks, get_all_users, get_all_projects,
    get_project_by_name, get_project_by_id,
    create_task, update_task, add_comment, add_attachment,
    search_tasks_by_title,
)
from agent3_pm.formatter import format_today_tasks, format_overdue_block
from agent3_pm.task_agent import smart_assistant, transcribe_voice

logger = logging.getLogger(__name__)


def _enter_url(user_id: int) -> str:
    return f"{config.WEB_BASE_URL.rstrip('/')}/enter/{user_id}"


def _link_board(user_id: int) -> str:
    return f'<a href="{config.WEB_BASE_URL.rstrip("/")}/enter/{user_id}">Доска</a>'


def _link_task(task_id: int, label: str = "Открыть задачу", user_id: int | None = None) -> str:
    base = config.WEB_BASE_URL.rstrip("/")
    if user_id:
        return f'<a href="{base}/enter/{user_id}?next=/task/{task_id}">{label}</a>'
    return f'<a href="{base}/task/{task_id}">{label}</a>'


def _link_new_task(task_id: int, user_id: int | None = None) -> str:
    return _link_task(task_id, "Новая задача", user_id)


def _fuzzy_match_user(name: str, users: list):
    name_lower = name.lower().strip()
    for u in users:
        if name_lower in u.name.lower():
            return u
    first = name_lower.split()[0] if name_lower else ""
    if len(first) >= 3:
        for u in users:
            if first in u.name.lower():
                return u
    return None


# Уменьшительные имена → канонический корень
_NICKNAMES = {
    "ван": "иван", "вань": "иван", "ваня": "иван",
    "миш": "михаил", "миха": "михаил",
    "саш": "александр", "шур": "александр",
    "дим": "дмитрий",
    "лёш": "алексей", "леш": "алексей", "алёш": "алексей", "алеш": "алексей",
    "кол": "николай",
    "серёж": "сергей", "сереж": "сергей", "серёг": "сергей", "серег": "сергей", "сер": "сергей",
    "вов": "владимир", "волод": "владимир",
    "жен": "евгений",
    "паш": "павел",
    "ром": "роман",
    "кост": "константин",
    "петь": "пётр", "пет": "пётр",
    "юр": "юрий",
    "андрюш": "андрей",
    "вит": "виктор",
    "тол": "анатолий",
    "бор": "борис",
    "макс": "максим",
    "арс": "арсений",
    "русл": "руслан",
}


def _name_stems(word: str) -> set[str]:
    word = word.lower().strip(" ?.,!»«\"'")
    stems = set()
    if len(word) >= 3:
        stems.update({word[:5], word[:4], word[:3]})
    for nick, full in _NICKNAMES.items():
        if word.startswith(nick):
            stems.update({full, full[:4]})
    return {s for s in stems if len(s) >= 3}


def _match_user_genitive(cand: str, users: list):
    """Match a name in genitive/diminutive form (ромы→Роман, вани→Иван, арсения→арсений)."""
    cand = cand.lower().strip(" ?.,!»«\"'")
    if len(cand) < 3:
        return None
    exact = _fuzzy_match_user(cand, users)
    if exact:
        return exact
    stems = _name_stems(cand)
    for u in users:
        nl = u.name.lower()
        words = nl.split()
        for st in stems:
            if st in nl or any(w.startswith(st) for w in words):
                return u
    return None


# Слова-уточнения: если есть в запросе — это НЕ простой кейс, отдаём GPT
_QUALIFIERS = (
    "кроме", "которы", "по ", " про ", "статус", "приоритет", "p0", "p1", "p2", "p3",
    "срочн", "сегодня", "завтра", "вчера", "недел", "проект", "доск", "в работе",
    "готов", "бэклог", "backlog", "todo", "wip", "за прошл", "только", "кроме",
)
# Местоимения/вопросительные — не имена
_NOT_NAMES = {"кого", "него", "неё", "нее", "них", "кому", "всех", "каждого", "меня", "себя", "тебя", "кто"}


# Глаголы создания/изменения — это НЕ запрос списка, отдаём GPT
_CREATE_VERBS = (
    "задай", "задать", "поставь", "постав", "создай", "создать", "создаём", "создаем",
    "добавь", "добав", "новая задача", "новую задачу", "сделай задачу", "напомни",
    "перенеси", "перемести", "измени", "помен", "назначь", "отметь",
)


def _task_intent(text: str, users: list):
    """Detect ('list'|'delete', user) ТОЛЬКО для простых 'задачи у X' / 'удали задачи X'.
    Любой нюанс (фильтры, статусы, 'кроме', 'по сайту') → (None, None), пусть решает GPT."""
    import re
    low = text.lower()
    if "задач" not in low:
        return (None, None)
    if any(w in low for w in ("просроч", "баг", "сколько", "мои задач")):
        return (None, None)
    # команда создания/изменения задачи — не наш кейс списка
    is_delete = any(w in low for w in ("удали", "удал", "снеси", "убери", "почист"))
    if not is_delete and any(v in low for v in _CREATE_VERBS):
        return (None, None)
    # Массовое удаление — ТОЛЬКО при явном «все/всё». Иначе GPT разберётся с нюансом.
    if is_delete and not any(w in low for w in ("все ", "всё ", "все\n", "всё\n")):
        return (None, None)
    # есть уточнения — не наш простой кейс
    if any(q in low for q in _QUALIFIERS):
        return (None, None)
    m = re.search(r"\bу\s+([а-яёa-zA-Z]{3,})", low) or re.search(r"задач[аиу]?\s+([а-яёa-zA-Z]{3,})\b", low)
    if not m:
        return (None, None)
    cand = m.group(1)
    if cand in _NOT_NAMES:
        return (None, None)
    target = _match_user_genitive(cand, users)
    if not target:
        return (None, None)
    return ("delete" if is_delete else "list", target)


def _is_team_report(text: str) -> bool:
    low = text.lower()
    if "задач" not in low and "отчет" not in low and "отчёт" not in low:
        return False
    return any(p in low for p in (
        "отчет", "отчёт", "у кого", "все задачи всех", "задачи всех",
        "у всех", "по всем", "каждого сотрудник", "по сотрудник", "кто чем занят",
    ))


async def _send_team_report(update, session, asker):
    """Деттерминированный отчёт по всем сотрудникам: группировка, замаскированные ссылки, разбивка."""
    users = await get_all_users(session)
    base = config.WEB_BASE_URL.rstrip("/")
    blocks = []
    for u in sorted(users, key=lambda x: x.name):
        tasks = [t for t in await get_all_tasks(session, assignee_id=u.id) if not t.archived_at]
        if not tasks:
            continue
        lines = [f"<b>{u.name}</b> ({u.position or '—'}) — задач: {len(tasks)}"]
        for t in tasks:
            st = t.status.value if hasattr(t.status, "value") else t.status
            bug = "[Баг] " if t.is_bug else ""
            link = f'<a href="{base}/enter/{asker.id}?next=/task/{t.id}">открыть</a>'
            lines.append(f"• {bug}{t.title} — {st} — {link}")
        blocks.append("\n".join(lines))

    if not blocks:
        await _reply(update, "Ни у кого нет активных задач.")
        return

    # разбивка по лимиту Telegram (~4096), собираем блоки целиком
    chunk, size = [], 0
    for b in blocks:
        if size + len(b) > 3500 and chunk:
            await _reply(update, "\n\n".join(chunk))
            chunk, size = [], 0
        chunk.append(b)
        size += len(b) + 2
    if chunk:
        await _reply(update, "\n\n".join(chunk))


async def _format_user_tasks(session, target, asker) -> str:
    """Deterministic list of target's tasks (all statuses, non-archived) with auto-login links."""
    tasks = await get_all_tasks(session, assignee_id=target.id)
    tasks = [t for t in tasks if not t.archived_at]
    if not tasks:
        return f"У {target.name} нет задач."
    base = config.WEB_BASE_URL.rstrip("/")
    lines = [f"<b>Задачи {target.name} ({len(tasks)}):</b>\n"]
    for i, t in enumerate(tasks, 1):
        st = t.status.value if hasattr(t.status, "value") else t.status
        bug = "[Баг] " if t.is_bug else ""
        link = f'<a href="{base}/enter/{asker.id}?next=/task/{t.id}">открыть</a>'
        lines.append(f"{i}. {bug}{t.title} — P{t.priority} — {st} — {link}")
    return "\n".join(lines)


def _menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([
        [KeyboardButton("Мои задачи"), KeyboardButton("Просрочки")],
        [KeyboardButton("Задать задачу"), KeyboardButton("Спросить по задачам")],
        [KeyboardButton("Инструкции")],
    ], resize_keyboard=True)


def _positions_kb() -> InlineKeyboardMarkup:
    from agent3_pm.models import POSITION_GROUPS
    rows = []
    for group_name, items in POSITION_GROUPS:
        rows.append([InlineKeyboardButton(f"── {group_name} ──", callback_data="noop")])
        row = []
        for pos in items:
            idx = POSITIONS.index(pos)
            row.append(InlineKeyboardButton(pos, callback_data=f"pos_{idx}"))
            if len(row) == 3:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
    return InlineKeyboardMarkup(rows)


def _clean_html(text: str) -> str:
    """Remove HTML tags not supported by Telegram (only b, i, a, code, pre allowed)."""
    import re
    # Блочные теги → перенос строки (Telegram HTML не поддерживает <br>/<p>/<li>)
    text = re.sub(r'<\s*br\s*/?>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</\s*(p|li|div|tr)\s*>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<\s*li\s*>', '• ', text, flags=re.IGNORECASE)
    text = re.sub(r'</?(?!b|/b|i|/i|a|/a|code|/code|pre|/pre)[^>]+>', '', text)
    # Схлопываем 3+ переносов
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text


async def _reply(update: Update, text: str, reply_markup=None):
    clean = _clean_html(text)

    # Callback + inline-кнопки → редактируем то же сообщение (не спамим новыми)
    if update.callback_query and isinstance(reply_markup, InlineKeyboardMarkup):
        try:
            await update.callback_query.edit_message_text(
                clean, parse_mode=ParseMode.HTML,
                reply_markup=reply_markup, disable_web_page_preview=True)
            return
        except Exception:
            pass

    # Callback без inline-кнопок → убираем старые кнопки
    if update.callback_query:
        try:
            await update.callback_query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

    msg = update.message or (update.callback_query and update.callback_query.message)
    if msg:
        try:
            await msg.reply_text(clean, parse_mode=ParseMode.HTML,
                                 reply_markup=reply_markup, disable_web_page_preview=True)
        except Exception:
            await msg.reply_text(clean, reply_markup=reply_markup,
                                 disable_web_page_preview=True)


async def _get_context_data(session, user) -> dict:
    """Build context for the smart assistant."""
    projects = await get_all_projects(session)
    users = await get_all_users(session)
    # ВСЕ незаархивированные задачи (включая done/approved/hold), чтобы агент
    # отвечал полно и удалял/менял любые задачи с канбана.
    all_tasks = await get_all_tasks(session)
    base_url = config.WEB_BASE_URL.rstrip("/")

    def _task_dict(t):
        return {
            "id": t.id, "title": t.title,
            "status": t.status.value if hasattr(t.status, "value") else t.status,
            "priority": t.priority, "is_bug": t.is_bug,
            "due_date": t.due_date.isoformat() if t.due_date else None,
            "project": t.project.name if t.project else None,
            "assignee": t.assignee.name if t.assignee else "не назначен",
            # автологин-ссылка для того, кто спрашивает
            "link": f"{base_url}/enter/{user.id}?next=/task/{t.id}",
        }

    return {
        "projects": [{"id": p.id, "name": p.name} for p in projects],
        "users": [{"id": u.id, "name": u.name, "position": u.position} for u in users],
        "current_user": {"id": user.id, "name": user.name, "position": user.position},
        "all_tasks": [_task_dict(t) for t in all_tasks],
        "web_base_url": base_url,
    }


# ── Registration ──

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with AsyncSessionLocal() as session:
        user = await get_user_by_telegram_id(session, update.effective_user.id)
    if user:
        pos = f" ({user.position})" if user.position else ""
        await _reply(update, f"Привет, {user.name}{pos}!\n\n{_link_board(user.id)}", _menu_kb())
        return
    await update.message.reply_text(
        "Привет! Это трекер задач MarketAI.\nЧтобы начать — зарегистрируйся.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Зарегистрироваться", callback_data="register")]]))


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    if data == "noop":
        await query.answer()
        return

    if data == "register":
        await query.answer()
        username = update.effective_user.username
        if username:
            async with AsyncSessionLocal() as session:
                existing = await get_user_by_telegram_username(session, username)
                if existing and not existing.telegram_id:
                    await bind_telegram_id(session, existing.id, update.effective_user.id)
                    await query.message.reply_text(f"Аккаунт найден: {existing.name}")
                    await _reply(update, f"{_link_board(existing.id)}", _menu_kb())
                    return
        context.user_data["reg_step"] = "name"
        await query.message.reply_text("Как тебя зовут? Напиши имя и фамилию.")

    elif data.startswith("pos_"):
        await query.answer()
        idx = int(data.split("_")[1])
        position = POSITIONS[idx] if 0 <= idx < len(POSITIONS) else None
        name = context.user_data.get("reg_name")
        if not name:
            await query.message.reply_text("Нажми /start заново.")
            return
        username = update.effective_user.username
        async with AsyncSessionLocal() as session:
            user = await register_user(session, telegram_id=update.effective_user.id,
                                       telegram_username=username, name=name, position=position)
        context.user_data.clear()
        await query.message.reply_text(f"Зарегистрирован: {user.name} ({position})")
        await _reply(update, f"{_link_board(user.id)}", _menu_kb())

    elif data == "files_yes":
        await query.answer()
        context.user_data["waiting_files"] = True
        await query.edit_message_text("Отправь файлы. Когда закончишь — нажми кнопку.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Готово", callback_data="files_done")]]))

    elif data == "files_no":
        await query.answer()
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await _execute_create(update, context)

    elif data == "files_done":
        await query.answer()
        context.user_data["waiting_files"] = False
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await _execute_create(update, context)

    # Pick buttons — feed answer back to smart assistant
    elif data.startswith("pick_proj_"):
        await query.answer()
        proj_id = int(data.split("_")[2])
        async with AsyncSessionLocal() as session:
            proj = await get_project_by_id(session, proj_id)
            user = await get_user_by_telegram_id(session, update.effective_user.id)
            if proj and user:
                await _process_smart(update, context, proj.name, session, user)

    elif data.startswith("pick_user_"):
        await query.answer()
        uid = int(data.split("_")[2])
        async with AsyncSessionLocal() as session:
            u = await get_user_by_telegram_id(session, update.effective_user.id)
            from agent3_pm.repository import get_user_by_id as get_uid
            target = await get_uid(session, uid)
            if target and u:
                await _process_smart(update, context, target.name, session, u)

    elif data.startswith("pick_status_"):
        await query.answer()
        status = data.replace("pick_status_", "")
        async with AsyncSessionLocal() as session:
            user = await get_user_by_telegram_id(session, update.effective_user.id)
            if user:
                await _process_smart(update, context, status, session, user)

    elif data.startswith("pick_prio_"):
        await query.answer()
        prio = data.replace("pick_prio_", "")
        async with AsyncSessionLocal() as session:
            user = await get_user_by_telegram_id(session, update.effective_user.id)
            if user:
                await _process_smart(update, context, f"P{prio}", session, user)

    # ── KB approval flow ──
    elif data.startswith("approve_take_"):
        await query.answer()
        batch_id = data.replace("approve_take_", "")
        from agent3_pm.kb_watcher import get_batch
        batch = get_batch(batch_id)
        if not batch:
            try:
                await query.edit_message_text("Этот пакет задач уже обработан.")
            except Exception:
                pass
            return
        if batch["locked_by"] is not None:
            try:
                await query.edit_message_text("Задачи уже взял другой сотрудник.")
            except Exception:
                pass
            return
        batch["locked_by"] = update.effective_user.id
        batch["current_idx"] = 0
        context.user_data["approval_batch"] = batch_id
        await _send_approval_card(query, batch_id, batch)

    elif data.startswith("approve_ok_"):
        await query.answer()
        batch_id = data.replace("approve_ok_", "")
        from agent3_pm.kb_watcher import get_batch
        batch = get_batch(batch_id)
        if not batch or batch["locked_by"] != update.effective_user.id:
            return
        batch["current_idx"] += 1
        if batch["current_idx"] >= len(batch["tasks"]):
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            await _finalize_approval(query.message, context, batch_id, batch)
        else:
            await _send_approval_card(query, batch_id, batch)

    elif data.startswith("approve_edit_"):
        await query.answer()
        batch_id = data.replace("approve_edit_", "")
        from agent3_pm.kb_watcher import get_batch as _gb
        b = _gb(batch_id)
        if not b or b["locked_by"] != update.effective_user.id:
            try:
                await query.edit_message_text("Только взявший на утверждение может редактировать.")
            except Exception:
                pass
            return
        context.user_data["editing_batch"] = batch_id
        try:
            await query.edit_message_text("Опиши изменения текстом или голосовым.")
        except Exception:
            pass


# ── KB Approval helpers ──

async def _send_approval_card(query_or_msg, batch_id: str, batch: dict):
    """Show task card for approval — edit existing message if from callback."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    idx = batch["current_idx"]
    task = batch["tasks"][idx]
    total = len(batch["tasks"])

    lines = [f"<b>Задача {idx + 1}/{total}</b>\n"]
    lines.append(f"<b>{task.get('title', '—')}</b>\n")
    if task.get("description"):
        lines.append(f"{task['description']}\n")
    lines.append(f"Исполнитель: {task.get('assignee_name') or 'не назначен'}")
    lines.append(f"Приоритет: P{task.get('priority') if task.get('priority') is not None else DEFAULT_PRIORITY}")
    if task.get("is_bug"):
        lines.append("Тип: Баг")
    if task.get("due_date"):
        lines.append(f"Дедлайн: {task['due_date']}")
    lines.append(f"Проект: {task.get('project_name') or 'не указан'}")
    lines.append(f"Этап: {task.get('status') or 'не указан'}")

    text = "\n".join(lines)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Редактировать", callback_data=f"approve_edit_{batch_id}"),
         InlineKeyboardButton("Утвердить", callback_data=f"approve_ok_{batch_id}")],
    ])
    # Если вызвано из callback → редактируем то же сообщение
    if hasattr(query_or_msg, "edit_message_text"):
        try:
            await query_or_msg.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
            return
        except Exception:
            pass
    await query_or_msg.reply_text(text, parse_mode="HTML", reply_markup=kb)


async def _finalize_approval(message, context, batch_id: str, batch: dict):
    """All tasks approved — create on kanban and notify assignees."""
    from agent3_pm.kb_watcher import remove_batch
    created = 0

    async with AsyncSessionLocal() as session:
        user = await get_user_by_telegram_id(session, batch["locked_by"])
        all_users = await get_all_users(session)

        for td in batch["tasks"]:
            assignee_id = None
            if td.get("assignee_name"):
                match = _fuzzy_match_user(td["assignee_name"], all_users)
                if match:
                    assignee_id = match.id

            project_id = None
            if td.get("project_name"):
                proj = await get_project_by_name(session, td["project_name"])
                if proj:
                    project_id = proj.id

            import datetime as dt
            due_date = None
            if td.get("due_date"):
                try:
                    due_date = dt.date.fromisoformat(str(td["due_date"])[:10])
                except (ValueError, TypeError):
                    pass

            status = TaskStatus.BACKLOG
            if td.get("status"):
                try:
                    status = TaskStatus(td["status"])
                except ValueError:
                    pass

            task = await create_task(
                session, title=td.get("title", "Без названия"),
                description=td.get("description"),
                project_id=project_id,
                priority=int(td.get("priority", 2)),
                is_bug=bool(td.get("is_bug", False)),
                assignee_id=assignee_id,
                creator_id=user.id if user else None,
                due_date=due_date, status=status,
            )

            # Add source link as comment
            source_url = td.get("_source_url")
            if source_url:
                await add_comment(session, task.id, None, f"Источник: {source_url}")

            # Notify assignee
            if assignee_id:
                task = await repo_get_task(session, task.id)
                if task and task.assignee and task.assignee.telegram_id:
                    from telegram import Bot
                    try:
                        bot_inst = Bot(token=config.TELEGRAM_BOT_TOKEN)
                        text = (f"Тебе назначена задача\n\n{task.title}\n"
                                f"P{task.priority}\n{_link_task(task.id, user_id=task.assignee.id)}")
                        await bot_inst.send_message(chat_id=task.assignee.telegram_id, text=text, parse_mode="HTML")
                    except Exception:
                        pass

            created += 1

    remove_batch(batch_id)
    context.user_data.pop("approval_batch", None)
    context.user_data.pop("editing_batch", None)
    await message.reply_text(f"Утверждено и создано {created} задач на канбане.", reply_markup=_menu_kb())


async def repo_get_task(session, task_id):
    from agent3_pm.repository import get_task_by_id
    return await get_task_by_id(session, task_id)


async def _handle_approval_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text/voice edit of a task during approval flow."""
    from agent3_pm.kb_watcher import get_batch
    from agent3_pm.task_agent import smart_assistant

    batch_id = context.user_data.get("editing_batch")
    batch = get_batch(batch_id)
    if not batch:
        context.user_data.pop("editing_batch", None)
        await _reply(update, "Пакет задач уже обработан.", _menu_kb())
        return

    text = (update.message.text or "").strip()
    if not text:
        return

    idx = batch["current_idx"]
    task = batch["tasks"][idx]

    # Use GPT to apply edits
    edit_prompt = f"""Текущая задача:
{json.dumps(task, ensure_ascii=False)}

Пользователь просит изменить: {text}

Верни обновлённый JSON задачи (тот же формат). Только JSON."""

    try:
        client = _get_client_openai()
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": edit_prompt}],
            temperature=0, max_tokens=500,
        )
        raw = resp.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        updated = json.loads(raw)
        updated["_source_url"] = task.get("_source_url")
        batch["tasks"][idx] = updated
    except Exception:
        await _reply(update, "Не удалось обработать изменения. Попробуй ещё раз.")
        return

    context.user_data.pop("editing_batch", None)
    await _send_approval_card(update.message, batch_id, batch)


def _get_client_openai():
    from agent3_pm.task_agent import _get_client
    return _get_client()


async def _edit_pending_task(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """Edit pending task card before creation using GPT."""
    td = context.user_data.get("pending_task")
    if not td:
        return

    edit_prompt = f"""Текущая задача (ещё не создана):
{json.dumps(td, ensure_ascii=False)}

Пользователь просит изменить: {text}

Верни обновлённый JSON задачи. Только JSON, без markdown."""

    try:
        client = _get_client_openai()
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": edit_prompt}],
            temperature=0, max_tokens=500,
        )
        raw = resp.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        updated = json.loads(raw)
        # Preserve internal keys
        for k in list(td.keys()):
            if k.startswith("_"):
                updated[k] = td[k]
        context.user_data["pending_task"] = updated
    except Exception:
        await _reply(update, "Не удалось обработать. Попробуй ещё.")
        return

    # Show updated card
    lines = [f"<b>{updated.get('title', '—')}</b>\n"]
    if updated.get("description"):
        lines.append(f"{updated['description']}\n")
    if updated.get("assignee_name"):
        lines.append(f"Исполнитель: {updated['assignee_name']}")
    lines.append(f"Приоритет: P{updated.get('priority') if updated.get('priority') is not None else DEFAULT_PRIORITY}")
    if updated.get("is_bug"):
        lines.append("Баг")
    if updated.get("due_date"):
        lines.append(f"Дедлайн: {updated['due_date']}")
    if updated.get("project_name"):
        lines.append(f"Проект: {updated['project_name']}")
    if updated.get("status"):
        lines.append(f"Этап: {updated['status']}")
    lines.append("\nПрикрепить файлы?")
    await _reply(update, "\n".join(lines),
        InlineKeyboardMarkup([
            [InlineKeyboardButton("Да", callback_data="files_yes"),
             InlineKeyboardButton("Нет, создать", callback_data="files_no")]]))


# ── Execute task creation ──

async def _execute_create(update: Update, context: ContextTypes.DEFAULT_TYPE):
    td = context.user_data.get("pending_task")
    if not td:
        await _reply(update, "Нет данных задачи.", _menu_kb())
        return

    async with AsyncSessionLocal() as session:
        user = await get_user_by_telegram_id(session, update.effective_user.id)
        users = await get_all_users(session)

        assignee_id = None
        if td.get("assignee_name"):
            match = _fuzzy_match_user(td["assignee_name"], users)
            if match:
                assignee_id = match.id

        project_id = None
        if td.get("project_name"):
            proj = await get_project_by_name(session, td["project_name"])
            if proj:
                project_id = proj.id

        import datetime as dt
        due_date = None
        if td.get("due_date"):
            try:
                raw = str(td["due_date"])[:10]
                due_date = dt.date.fromisoformat(raw)
            except (ValueError, TypeError):
                pass

        est_hours = None
        if td.get("estimated_hours"):
            try:
                est_hours = float(td["estimated_hours"])
            except (ValueError, TypeError):
                pass

        priority = DEFAULT_PRIORITY
        if td.get("priority") is not None:
            try:
                priority = int(td["priority"])
            except (ValueError, TypeError):
                pass

        status = TaskStatus.BACKLOG
        if td.get("status"):
            try:
                status = TaskStatus(td["status"])
            except ValueError:
                pass

        task = await create_task(
            session, title=td.get("title", "Без названия"),
            description=td.get("description"), project_id=project_id,
            priority=priority,
            is_bug=bool(td.get("is_bug", False)),
            assignee_id=assignee_id, creator_id=user.id if user else None,
            estimated_hours=est_hours, due_date=due_date,
            status=status,
        )

        files = context.user_data.get("pending_files", [])
        if files:
            from agent3_pm.web import UPLOAD_DIR
            import uuid
            comment = await add_comment(session, task.id, user.id if user else None,
                                        "Файлы при создании задачи")
            for finfo in files:
                ext = os.path.splitext(finfo["name"])[1]
                stored = f"{uuid.uuid4().hex}{ext}"
                with open(finfo["path"], "rb") as src:
                    (UPLOAD_DIR / stored).write_bytes(src.read())
                await add_attachment(session, comment.id, finfo["name"], stored, finfo.get("mime"))
                try:
                    os.remove(finfo["path"])
                except OSError:
                    pass

        logger.info(f"Task created: {task.title}, assignee_id={assignee_id}, project_id={project_id}")

        # Notify assignee
        if assignee_id and assignee_id != (user.id if user else None):
            from agent3_pm.repository import get_task_by_id as _get_task
            task = await _get_task(session, task.id)
            if task and task.assignee and task.assignee.telegram_id:
                from telegram import Bot
                try:
                    bot_inst = Bot(token=config.TELEGRAM_BOT_TOKEN)
                    base = config.WEB_BASE_URL.rstrip("/")
                    notify_text = (f"Тебе назначена задача от {user.name if user else '—'}\n\n"
                                   f"{task.title}\nP{task.priority}\n{_link_task(task.id, user_id=task.assignee.id)}")
                    await bot_inst.send_message(chat_id=task.assignee.telegram_id, text=notify_text, parse_mode="HTML")
                except Exception:
                    pass

    context.user_data.pop("pending_task", None)
    context.user_data.pop("pending_files", None)
    context.user_data.pop("waiting_files", None)

    uid = user.id if user else None
    await _reply(update, f"Задача создана: <b>{task.title}</b>\n{_link_new_task(task.id, uid)}", _menu_kb())


# ── Main handler ──

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Registration
    if context.user_data.get("reg_step") == "name":
        context.user_data["reg_name"] = update.message.text.strip()
        context.user_data["reg_step"] = "position"
        await update.message.reply_text("Выбери должность:", reply_markup=_positions_kb())
        return

    if context.user_data.get("waiting_files"):
        await _collect_file(update, context)
        return

    # KB approval editing
    if context.user_data.get("editing_batch"):
        await _handle_approval_edit(update, context)
        return

    # Editing pending task card (before creation)
    if context.user_data.get("pending_task"):
        text = (update.message.text or "").strip()
        if text and text not in ("Мои задачи", "Просрочки", "Задать задачу", "Спросить по задачам", "Инструкции"):
            await _send_typing(update)
            await _edit_pending_task(update, context, text)
            return

    text = (update.message.text or "").strip()

    async with AsyncSessionLocal() as session:
        user = await get_user_by_telegram_id(session, update.effective_user.id)
        if not user:
            await update.message.reply_text("Нажми /start для регистрации.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Зарегистрироваться", callback_data="register")]]))
            return

        # Quick menu buttons (no LLM needed)
        if text == "Мои задачи":
            all_my = await get_all_tasks(session, assignee_id=user.id)
            from agent3_pm.models import ACTIVE_STATUSES
            active = [t for t in all_my if t.status in ACTIVE_STATUSES and not t.archived_at]
            if not active:
                reply = "У тебя нет активных задач."
            else:
                base = config.WEB_BASE_URL.rstrip("/")
                lines = [f"<b>Твои задачи ({len(active)}):</b>\n"]
                for t in active:
                    p = t.project.name if t.project else "—"
                    lines.append(f"{'[Баг] ' if t.is_bug else ''}P{t.priority} {t.title} ({p}) {_link_task(t.id, 'ссылка', user.id)}")
                reply = "\n".join(lines)
            reply += f"\n\n{_link_board(user.id)}"
            await _reply(update, reply, _menu_kb())
            return

        if text == "Инструкции":
            instructions = (
                "<b>Как пользоваться трекером</b>\n\n"

                "<b>Кнопки меню:</b>\n"
                "- Мои задачи — все твои активные задачи со ссылками\n"
                "- Просрочки — просроченные, горящие и баги\n"
                "- Задать задачу — создать задачу текстом или голосовым\n"
                "- Спросить по задачам — вопросы и управление канбаном\n"
                "- Инструкции — это сообщение\n\n"

                "<b>Создание задачи:</b>\n"
                "Нажми «Задать задачу» и опиши что нужно.\n"
                "Можно текстом или голосовым сообщением.\n"
                "Агент уточнит доску и этап, покажет карточку.\n"
                "Можно прикрепить файлы.\n\n"

                "<b>Управление через «Спросить»:</b>\n"
                "- Какие задачи у Амира?\n"
                "- Какие задачи у CEO?\n"
                "- Что просрочено?\n"
                "- Переставь задачу X на следующий этап\n"
                "- Перенеси задачу X на доску Marketing\n"
                "- Отметь задачу X выполненной\n"
                "- Удали задачу X\n"
                "- Напомни через 30 минут проверить отчёт\n\n"

                "<b>В групповых чатах:</b>\n"
                "Добавь бота в рабочую группу.\n"
                "Тегни @projectmanageraiibot и опиши задачу.\n"
                "Бот уточнит детали и создаст задачу на канбане.\n"
                "Ответственный получит уведомление в личку.\n\n"

                "<b>Автоматика:</b>\n"
                "- Новые задачи из созвонов и записей (база знаний)\n"
                "- TOP-1 получает задачи на утверждение\n"
                "- Утренняя сводка руководителям в 9:00\n"
                "- Напоминания о дедлайнах 3 раза в день\n"
                "- Уведомление при назначении задачи\n"
                "- Баги из GitHub автоматически на канбане\n\n"

                "<b>Веб-трекер:</b>\n"
                f"{_link_board(user.id)}\n"
                "Канбан по проектам, редактирование, комментарии,\n"
                "файлы, удаление, кнопка «Выполнено»."
            )
            await _reply(update, instructions, _menu_kb())
            return

        if text == "Просрочки":
            overdue = await get_overdue_tasks(session, user_id=user.id)
            hot = await get_hot_tasks(session, 48, user_id=user.id)
            bugs = await get_user_bugs(session, user.id)
            reply = format_overdue_block(overdue, hot, bugs) + f"\n\n{_link_board(user.id)}"
            await _reply(update, reply, _menu_kb())
            return

        # Smart assistant mode — ЖЁСТКОЕ разделение по кнопкам
        if text == "Задать задачу":
            context.user_data["chat_mode"] = "create"
            context.user_data["chat_history"] = []
            await _reply(update, "Опиши задачу — текстом или голосом. Уточню доску и этап, покажу карточку.")
            return
        if text == "Спросить по задачам":
            context.user_data["chat_mode"] = "ask"
            context.user_data["chat_history"] = []
            await _reply(update, "Спрашивай по задачам или командуй: показать, перенести на этап, удалить, напомнить.")
            return

        if not context.user_data.get("chat_mode"):
            await _reply(update, "Выбери действие кнопкой меню.", _menu_kb())
            return

        # In chat mode — send to smart assistant
        await _process_smart(update, context, text, session, user)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with AsyncSessionLocal() as session:
        user = await get_user_by_telegram_id(session, update.effective_user.id)
    if not user:
        await _reply(update, "Нажми /start для регистрации.")
        return

    # Voice during approval edit
    if context.user_data.get("editing_batch"):
        voice = update.message.voice or update.message.audio
        if voice:
            file = await voice.get_file()
            tmp = tempfile.NamedTemporaryFile(suffix=".ogg", delete=False)
            tmp.close()
            await file.download_to_drive(tmp.name)
            await _reply(update, "Распознаю...")
            text = await transcribe_voice(tmp.name)
            try:
                os.remove(tmp.name)
            except OSError:
                pass
            if text:
                await _reply(update, f"Распознано: {text}")
                update.message.text = text
                await _handle_approval_edit(update, context)
            else:
                await _reply(update, "Не распознано. Попробуй текстом.")
            return

    # Voice while pending task card is shown — edit the pending task
    if context.user_data.get("pending_task"):
        voice = update.message.voice or update.message.audio
        if voice:
            file = await voice.get_file()
            tmp = tempfile.NamedTemporaryFile(suffix=".ogg", delete=False)
            tmp.close()
            await file.download_to_drive(tmp.name)
            await _reply(update, "Распознаю...")
            text = await transcribe_voice(tmp.name)
            try:
                os.remove(tmp.name)
            except OSError:
                pass
            if text:
                await _reply(update, f"Распознано: {text}")
                await _send_typing(update)
                await _edit_pending_task(update, context, text)
            else:
                await _reply(update, "Не распознано. Попробуй текстом.")
            return

    if not context.user_data.get("chat_mode"):
        context.user_data["chat_mode"] = True
        context.user_data["chat_history"] = []

    voice = update.message.voice or update.message.audio
    if not voice:
        return
    file = await voice.get_file()
    tmp = tempfile.NamedTemporaryFile(suffix=".ogg", delete=False)
    tmp.close()
    await file.download_to_drive(tmp.name)

    await _reply(update, "Распознаю...")
    text = await transcribe_voice(tmp.name)
    try:
        os.remove(tmp.name)
    except OSError:
        pass

    if not text:
        await _reply(update, "Не удалось распознать. Попробуй текстом.", _menu_kb())
        return

    await _reply(update, f"Распознано: {text}")

    async with AsyncSessionLocal() as session:
        await _process_smart(update, context, text, session, user)


async def _notify_task_updated_bot(session, task, updater, old_assignee_id: int | None):
    """Notify assignee when task is updated via bot by someone else."""
    try:
        from telegram import Bot
        if not config.TELEGRAM_BOT_TOKEN:
            return
        bot_inst = Bot(token=config.TELEGRAM_BOT_TOKEN)
        notified = set()
        updater_id = updater.id if updater else None
        updater_name = updater.name if updater else "Кто-то"

        if task.assignee and task.assignee.telegram_id and task.assignee_id != updater_id:
            text = (f"Твоя задача обновлена пользователем {updater_name}\n\n"
                    f"<b>{task.title}</b>\n{_link_task(task.id, user_id=task.assignee.id)}")
            await bot_inst.send_message(chat_id=task.assignee.telegram_id, text=text,
                                        parse_mode="HTML", disable_web_page_preview=True)
            notified.add(task.assignee_id)

        if old_assignee_id and old_assignee_id != task.assignee_id and old_assignee_id not in notified:
            from agent3_pm.repository import get_user_by_id
            old_user = await get_user_by_id(session, old_assignee_id)
            if old_user and old_user.telegram_id and old_user.id != updater_id:
                text = (f"Задача переназначена пользователем {updater_name}\n\n"
                        f"<b>{task.title}</b>\n{_link_task(task.id, user_id=old_user.id)}")
                await bot_inst.send_message(chat_id=old_user.telegram_id, text=text,
                                            parse_mode="HTML", disable_web_page_preview=True)
    except Exception:
        logger.exception("notify task update failed")


async def _send_typing(update):
    """Send 'typing...' indicator."""
    try:
        msg = update.message or (update.callback_query and update.callback_query.message)
        if msg:
            await msg.chat.send_action("typing")
    except Exception:
        pass


async def _process_smart(update, context, text, session, user):
    """Send message to smart assistant and handle the action."""
    mode = context.user_data.get("chat_mode", "ask")

    # Показываем «печатает…» пока GPT думает
    await _send_typing(update)

    ctx_data = await _get_context_data(session, user)

    # Детерминированные перехватчики (отчёт/список/удаление) — ТОЛЬКО в режиме «Спросить».
    # В режиме «Задать задачу» ничего не перехватываем — всё идёт на создание.
    if mode == "ask":
        # «мои задачи» / «какие у меня задачи» / «что у меня» — детерминированно
        low = text.lower()
        if ("мои задач" in low or "у меня" in low or "мне задач" in low
                or "у меня задач" in low or "мои активн" in low):
            await _reply(update, await _format_user_tasks(session, user, user))
            return

        if _is_team_report(text):
            await _send_team_report(update, session, user)
            return
        all_users = await get_all_users(session)
        intent, target = _task_intent(text, all_users)
        if target and intent == "list":
            await _reply(update, await _format_user_tasks(session, target, user))
            return
        if target and intent == "delete":
            tasks = [t for t in await get_all_tasks(session, assignee_id=target.id) if not t.archived_at]
            if not tasks:
                await _reply(update, f"У {target.name} нет задач для удаления.")
                return
            from agent3_pm.repository import delete_task as del_task
            n = 0
            for t in list(tasks):
                if await del_task(session, t.id):
                    n += 1
            await _reply(update, f"Удалено задач у {target.name}: {n}.")
            return

    history = context.user_data.get("chat_history", [])

    await _send_typing(update)
    result = await smart_assistant(text, ctx_data, history, mode=mode)

    # Save to history
    history.append({"role": "user", "content": text})
    history.append({"role": "assistant", "content": json.dumps(result, ensure_ascii=False)})
    if len(history) > 20:
        history = history[-20:]
    context.user_data["chat_history"] = history

    action = result.get("action", "answer")

    if action == "clarify":
        msg = result.get("message", "Уточни, пожалуйста.")
        # If asking about project — show project buttons
        msg_lower = msg.lower()
        if "проект" in msg_lower or "доск" in msg_lower:
            projects = ctx_data.get("projects", [])
            buttons = [[InlineKeyboardButton(p["name"], callback_data=f"pick_proj_{p['id']}")] for p in projects]
            await _reply(update, msg, InlineKeyboardMarkup(buttons))
        # If asking about assignee — show user buttons
        elif "исполнител" in msg_lower or "кому" in msg_lower or "назнач" in msg_lower:
            users = ctx_data.get("users", [])
            rows, row = [], []
            for u in users:
                row.append(InlineKeyboardButton(u["name"], callback_data=f"pick_user_{u['id']}"))
                if len(row) == 2:
                    rows.append(row)
                    row = []
            if row:
                rows.append(row)
            await _reply(update, msg, InlineKeyboardMarkup(rows))
        # If asking about status — show status buttons
        elif "статус" in msg_lower or "этап" in msg_lower or "колонк" in msg_lower:
            buttons = [
                [InlineKeyboardButton("Бэклог", callback_data="pick_status_backlog"),
                 InlineKeyboardButton("К выполнению", callback_data="pick_status_todo")],
                [InlineKeyboardButton("В работе", callback_data="pick_status_wip"),
                 InlineKeyboardButton("Планирование", callback_data="pick_status_planning")],
            ]
            await _reply(update, msg, InlineKeyboardMarkup(buttons))
        # If asking about priority — show priority buttons
        elif "приоритет" in msg_lower or "priority" in msg_lower or "важност" in msg_lower:
            buttons = [
                [InlineKeyboardButton("P0 — срочно", callback_data="pick_prio_0"),
                 InlineKeyboardButton("P1 — высокий", callback_data="pick_prio_1")],
                [InlineKeyboardButton("P2 — обычный", callback_data="pick_prio_2"),
                 InlineKeyboardButton("P3 — низкий", callback_data="pick_prio_3")],
            ]
            await _reply(update, msg, InlineKeyboardMarkup(buttons))
        else:
            await _reply(update, msg)

    elif action == "answer":
        msg = result.get("message", "Не понял.")
        await _reply(update, msg)

    elif action == "create_task":
        # Нормализуем priority: null/None → 2 (P2 по умолчанию)
        if result.get("priority") is None:
            result["priority"] = DEFAULT_PRIORITY
        context.user_data["pending_task"] = result
        context.user_data["pending_files"] = []

        lines = [f"<b>{result.get('title', '—')}</b>\n"]
        if result.get("description"):
            lines.append(f"{result['description']}\n")
        if result.get("assignee_name"):
            lines.append(f"Исполнитель: {result['assignee_name']}")
        lines.append(f"Приоритет: P{result['priority']}")
        if result.get("is_bug"):
            lines.append("Баг")
        if result.get("due_date"):
            lines.append(f"Дедлайн: {result['due_date']}")
        if result.get("project_name"):
            lines.append(f"Проект: {result['project_name']}")
        if result.get("status"):
            lines.append(f"Этап: {result['status']}")
        lines.append("\nПрикрепить файлы?")
        await _reply(update, "\n".join(lines),
            InlineKeyboardMarkup([
                [InlineKeyboardButton("Да", callback_data="files_yes"),
                 InlineKeyboardButton("Нет, создать", callback_data="files_no")]]))

    elif action == "update_task":
        task_id = result.get("task_id")
        changes = result.get("changes", {})

        active_ids = {t["id"] for t in ctx_data.get("all_tasks", [])}
        if not task_id or task_id not in active_ids:
            title = result.get("task_title", "")
            tasks = await search_tasks_by_title(session, title, limit=1)
            if not tasks:
                await _reply(update, "Такой задачи нет на канбане.")
                return
            task_id = tasks[0].id

        if "status" in changes:
            try:
                changes["status"] = TaskStatus(changes["status"])
            except ValueError:
                del changes["status"]
        if "assignee_name" in changes:
            users = await get_all_users(session)
            match = _fuzzy_match_user(changes["assignee_name"], users)
            if match:
                changes["assignee_id"] = match.id
            del changes["assignee_name"]
        if "project_name" in changes:
            proj = await get_project_by_name(session, changes["project_name"])
            if proj:
                changes["project_id"] = proj.id
            del changes["project_name"]
        if "due_date" in changes and isinstance(changes["due_date"], str):
            import datetime as dt
            try:
                changes["due_date"] = dt.date.fromisoformat(changes["due_date"][:10])
            except (ValueError, TypeError):
                del changes["due_date"]

        from agent3_pm.repository import get_task_by_id
        task = await get_task_by_id(session, task_id)
        if not task:
            await _reply(update, "Задача не найдена.")
            return

        old_assignee_id = task.assignee_id
        await update_task(session, task.id, **changes)
        await _reply(update, f"Обновлено: <b>{task.title}</b>\n{_link_task(task.id, user_id=user.id)}")

        # Уведомить ответственного (если не он сам обновил)
        task = await get_task_by_id(session, task.id)
        if task:
            await _notify_task_updated_bot(session, task, user, old_assignee_id)

    elif action == "delete_tasks":
        from agent3_pm.repository import get_task_by_id, delete_task as del_task
        active_ids = {t["id"] for t in ctx_data.get("all_tasks", [])}
        ids = [i for i in (result.get("task_ids") or []) if i in active_ids]
        if not ids:
            await _reply(update, "Таких задач нет на канбане.")
            return
        deleted = 0
        for tid in ids:
            task = await get_task_by_id(session, tid)
            if task:
                await del_task(session, tid)
                deleted += 1
        await _reply(update, f"Удалено задач: {deleted}.")

    elif action == "delete_task":
        task_id = result.get("task_id")
        active_ids = {t["id"] for t in ctx_data.get("all_tasks", [])}
        if task_id and task_id in active_ids:
            from agent3_pm.repository import get_task_by_id, delete_task as del_task
            task = await get_task_by_id(session, task_id)
            if task:
                title = task.title
                await del_task(session, task_id)
                await _reply(update, f"Задача удалена: {title}")
            else:
                await _reply(update, "Задача не найдена.")
        else:
            await _reply(update, "Не удалось определить задачу для удаления.")

    elif action == "set_reminder":
        delay = int(result.get("delay_minutes", 30))
        msg = result.get("message", "Напоминание")
        chat_id = update.effective_user.id
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        import asyncio

        async def _send_reminder():
            from telegram import Bot
            try:
                bot_inst = Bot(token=config.TELEGRAM_BOT_TOKEN)
                await bot_inst.send_message(chat_id=chat_id, text=f"Напоминание:\n{msg}")
            except Exception:
                pass

        loop = asyncio.get_event_loop()
        loop.call_later(delay * 60, lambda: asyncio.ensure_future(_send_reminder()))
        await _reply(update, f"Напомню через {delay} мин.")


async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("waiting_files"):
        await _reply(update, "Выбери действие кнопкой меню.", _menu_kb())
        return
    await _collect_file(update, context)


async def _collect_file(update, context):
    files = context.user_data.setdefault("pending_files", [])
    doc = update.message.document
    photo = update.message.photo
    if doc:
        file = await doc.get_file()
        name, mime = doc.file_name or "file", doc.mime_type
    elif photo:
        file = await photo[-1].get_file()
        name, mime = "photo.jpg", "image/jpeg"
    else:
        return
    ext = os.path.splitext(name)[1] or ".bin"
    tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
    tmp.close()
    await file.download_to_drive(tmp.name)
    files.append({"path": tmp.name, "name": name, "mime": mime})
    await _reply(update, f"Файл принят ({len(files)}). Ещё или нажми Готово.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Готово", callback_data="files_done")]]))


BOT_USERNAME = "projectmanageraiibot"

# Group sessions: {(chat_id, user_id): {"active": bool, "history": []}}
_group_sessions: dict[tuple[int, int], dict] = {}


async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle messages in group chats. React only to @bot mentions and follow-ups."""
    if not update.message or not update.message.text:
        return
    if update.effective_chat.type not in ("group", "supergroup"):
        return

    text = update.message.text
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    key = (chat_id, user_id)

    text_lower = text.lower()
    mentioned = f"@{BOT_USERNAME.lower()}" in text_lower

    if mentioned:
        import re, time
        clean_text = re.sub(rf"@{BOT_USERNAME}", "", text, flags=re.IGNORECASE).strip()
        _group_sessions[key] = {"active": True, "history": [], "ts": time.time()}
        if clean_text:
            await _process_group_smart(update, context, clean_text, key)
        else:
            await update.message.reply_text("Слушаю. Опиши задачу или спроси о задачах.")
        return

    session_data = _group_sessions.get(key)
    if not session_data or not session_data["active"]:
        return

    # Таймаут: 5 минут без активности — сессия деактивируется
    import time
    if time.time() - session_data.get("ts", 0) > 300:
        _group_sessions.pop(key, None)
        return
    session_data["ts"] = time.time()

    await _process_group_smart(update, context, text, key)


async def _process_group_smart(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                text: str, key: tuple[int, int]):
    """Process group message through smart assistant."""
    from agent3_pm.task_agent import smart_assistant

    try:
        async with AsyncSessionLocal() as session:
            user = await get_user_by_telegram_id(session, update.effective_user.id)
            if not user:
                await update.message.reply_text(
                    "Ты не зарегистрирован. Напиши мне в личку /start.")
                _group_sessions.pop(key, None)
                return

            ctx_data = await _get_context_data(session, user)
            history = _group_sessions[key].get("history", [])

            await _send_typing(update)
            result = await smart_assistant(text, ctx_data, history)
            logger.info(f"Group smart result: {result}")

            history.append({"role": "user", "content": text})
            history.append({"role": "assistant", "content": json.dumps(result, ensure_ascii=False)})
            if len(history) > 10:
                history = history[-10:]
            _group_sessions[key]["history"] = history

            action = result.get("action", "answer")

            if action == "clarify":
                await update.message.reply_text(result.get("message", "Уточни."))

            elif action == "answer":
                await update.message.reply_text(result.get("message", ""))
                _group_sessions[key]["active"] = False

            elif action == "create_task":
                td = result
                assignee_id = None
                if td.get("assignee_name"):
                    users = await get_all_users(session)
                    match = _fuzzy_match_user(td["assignee_name"], users)
                    if match:
                        assignee_id = match.id

                project_id = None
                if td.get("project_name"):
                    proj = await get_project_by_name(session, td["project_name"])
                    if proj:
                        project_id = proj.id

                import datetime as dt
                due_date = None
                if td.get("due_date"):
                    try:
                        due_date = dt.date.fromisoformat(str(td["due_date"])[:10])
                    except (ValueError, TypeError):
                        pass

                status = TaskStatus.BACKLOG
                if td.get("status"):
                    try:
                        status = TaskStatus(td["status"])
                    except ValueError:
                        pass

                task = await create_task(
                    session, title=td.get("title", "Без названия"),
                    description=td.get("description"), project_id=project_id,
                    priority=int(td.get("priority", 2)),
                    is_bug=bool(td.get("is_bug", False)),
                    assignee_id=assignee_id, creator_id=user.id,
                    due_date=due_date, status=status,
                )

                await update.message.reply_text(
                    f"Задача создана: {task.title}\n{_link_task(task.id, user_id=user.id)}", parse_mode="HTML")

                if assignee_id:
                    from agent3_pm.repository import get_task_by_id
                    task = await get_task_by_id(session, task.id)
                    if task and task.assignee and task.assignee.telegram_id:
                        from telegram import Bot
                        try:
                            bot_inst = Bot(token=config.TELEGRAM_BOT_TOKEN)
                            notify = (f"Тебе назначена задача от {user.name}\n\n"
                                      f"{task.title}\nP{task.priority}\n{_link_task(task.id, user_id=task.assignee.id)}")
                            await bot_inst.send_message(chat_id=task.assignee.telegram_id, text=notify, parse_mode="HTML")
                        except Exception:
                            pass

                _group_sessions[key]["active"] = False

            elif action == "update_task":
                await update.message.reply_text("Управление задачами в группе — используй личку бота.")
                _group_sessions[key]["active"] = False

            elif action == "set_reminder":
                delay = int(result.get("delay_minutes", 30))
                msg = result.get("message", "Напоминание")
                chat_id = update.effective_user.id
                import asyncio

                async def _send_group_reminder():
                    from telegram import Bot
                    try:
                        bot_inst = Bot(token=config.TELEGRAM_BOT_TOKEN)
                        await bot_inst.send_message(chat_id=chat_id, text=f"Напоминание:\n{msg}")
                    except Exception:
                        pass

                loop = asyncio.get_event_loop()
                loop.call_later(delay * 60, lambda: asyncio.ensure_future(_send_group_reminder()))
                await update.message.reply_text(f"Напомню через {delay} мин.")
                _group_sessions[key]["active"] = False

            elif action == "delete_task":
                await update.message.reply_text("Удаление задач — используй личку бота.")
                _group_sessions[key]["active"] = False

            else:
                await update.message.reply_text("Готово.")
                _group_sessions[key]["active"] = False

    except Exception:
        logger.exception("Error in group smart processing")
        try:
            await update.message.reply_text("Ошибка обработки. Попробуй ещё раз.")
        except Exception:
            pass


def create_bot_application() -> Application:
    app = (Application.builder()
           .token(config.TELEGRAM_BOT_TOKEN)
           .concurrent_updates(True)
           .build())
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS, handle_group_message))
    app.add_handler(MessageHandler(
        (filters.VOICE | filters.AUDIO) & filters.ChatType.PRIVATE, handle_voice))
    app.add_handler(MessageHandler(
        (filters.Document.ALL | filters.PHOTO) & filters.ChatType.PRIVATE, handle_file))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, handle_message))
    return app
