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


async def _reply(update: Update, text: str, reply_markup=None):
    msg = update.message or (update.callback_query and update.callback_query.message)
    if msg:
        await msg.reply_text(text, parse_mode=ParseMode.HTML,
                             reply_markup=reply_markup, disable_web_page_preview=True)


async def _get_context_data(session, user) -> dict:
    """Build context for the smart assistant."""
    projects = await get_all_projects(session)
    users = await get_all_users(session)
    all_tasks = await get_all_tasks(session)
    base_url = config.WEB_BASE_URL.rstrip("/")

    def _task_dict(t):
        return {
            "id": t.id, "title": t.title,
            "status": t.status.value if hasattr(t.status, "value") else t.status,
            "priority": t.priority, "is_bug": t.is_bug,
            "due_date": t.due_date.isoformat() if t.due_date else None,
            "project": t.project.name if t.project else None,
            "assignee": t.assignee.name if t.assignee else None,
            "link": f"{base_url}/task/{t.id}",
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
        await _reply(update, f"Привет, {user.name}{pos}!\n\n{_enter_url(user.id)}", _menu_kb())
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
                    await _reply(update, f"{_enter_url(existing.id)}", _menu_kb())
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
        await _reply(update, f"{_enter_url(user.id)}", _menu_kb())

    elif data == "files_yes":
        await query.answer()
        context.user_data["waiting_files"] = True
        await query.message.reply_text("Отправь файлы. Когда закончишь — нажми кнопку.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Готово", callback_data="files_done")]]))

    elif data == "files_no":
        await query.answer()
        await _execute_create(update, context)

    elif data == "files_done":
        await query.answer()
        context.user_data["waiting_files"] = False
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

    # ── KB approval flow ──
    elif data.startswith("approve_take_"):
        await query.answer()
        batch_id = data.replace("approve_take_", "")
        from agent3_pm.kb_watcher import get_batch
        batch = get_batch(batch_id)
        if not batch:
            await query.message.reply_text("Этот пакет задач уже обработан.")
            return
        if batch["locked_by"] is not None:
            await query.message.reply_text("Задачи уже взял другой сотрудник.")
            return
        batch["locked_by"] = update.effective_user.id
        batch["current_idx"] = 0
        context.user_data["approval_batch"] = batch_id
        await _send_approval_card(query.message, batch_id, batch)

    elif data.startswith("approve_ok_"):
        await query.answer()
        batch_id = data.replace("approve_ok_", "")
        from agent3_pm.kb_watcher import get_batch
        batch = get_batch(batch_id)
        if not batch or batch["locked_by"] != update.effective_user.id:
            return
        batch["current_idx"] += 1
        if batch["current_idx"] >= len(batch["tasks"]):
            await _finalize_approval(query.message, context, batch_id, batch)
        else:
            await _send_approval_card(query.message, batch_id, batch)

    elif data.startswith("approve_edit_"):
        await query.answer()
        batch_id = data.replace("approve_edit_", "")
        from agent3_pm.kb_watcher import get_batch as _gb
        b = _gb(batch_id)
        if not b or b["locked_by"] != update.effective_user.id:
            await query.message.reply_text("Только взявший на утверждение может редактировать.")
            return
        context.user_data["editing_batch"] = batch_id
        await query.message.reply_text("Опиши изменения текстом или голосовым.")


# ── KB Approval helpers ──

async def _send_approval_card(message, batch_id: str, batch: dict):
    """Send current task card for approval."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    idx = batch["current_idx"]
    task = batch["tasks"][idx]
    total = len(batch["tasks"])

    lines = [f"<b>Задача {idx + 1}/{total}</b>\n"]
    lines.append(f"<b>{task.get('title', '—')}</b>\n")
    if task.get("description"):
        lines.append(f"{task['description']}\n")
    lines.append(f"Исполнитель: {task.get('assignee_name') or 'не назначен'}")
    lines.append(f"Приоритет: P{task.get('priority', 2)}")
    if task.get("is_bug"):
        lines.append("Тип: Баг")
    if task.get("due_date"):
        lines.append(f"Дедлайн: {task['due_date']}")
    lines.append(f"Проект: {task.get('project_name') or 'не указан'}")
    lines.append(f"Этап: {task.get('status') or 'не указан'}")

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Редактировать", callback_data=f"approve_edit_{batch_id}"),
         InlineKeyboardButton("Утвердить", callback_data=f"approve_ok_{batch_id}")],
    ])
    await message.reply_text("\n".join(lines), parse_mode="HTML", reply_markup=kb)


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
                        base = config.WEB_BASE_URL.rstrip("/")
                        text = (f"Тебе назначена задача\n\n{task.title}\n"
                                f"P{task.priority}\n{base}/task/{task.id}")
                        await bot_inst.send_message(chat_id=task.assignee.telegram_id, text=text)
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
                                   f"{task.title}\nP{task.priority}\n{base}/task/{task.id}")
                    await bot_inst.send_message(chat_id=task.assignee.telegram_id, text=notify_text)
                except Exception:
                    pass

    context.user_data.pop("pending_task", None)
    context.user_data.pop("pending_files", None)
    context.user_data.pop("waiting_files", None)

    web = f"{config.WEB_BASE_URL.rstrip('/')}/task/{task.id}"
    await _reply(update, f"Задача создана: <b>{task.title}</b>\n{web}", _menu_kb())


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
            active = [t for t in all_my if t.status in ACTIVE_STATUSES]
            if not active:
                reply = "У тебя нет активных задач."
            else:
                base = config.WEB_BASE_URL.rstrip("/")
                lines = [f"<b>Твои задачи ({len(active)}):</b>\n"]
                for t in active:
                    p = t.project.name if t.project else "—"
                    lines.append(f"{'[Баг] ' if t.is_bug else ''}P{t.priority} {t.title} ({p}) {base}/task/{t.id}")
                reply = "\n".join(lines)
            reply += f"\n\n{_enter_url(user.id)}"
            await _reply(update, reply, _menu_kb())
            return

        if text == "Инструкции":
            instructions = (
                "<b>Как пользоваться трекером:</b>\n\n"
                "<b>Кнопки меню:</b>\n"
                "- Мои задачи — список твоих активных задач\n"
                "- Просрочки — просроченные и горящие\n"
                "- Задать задачу — создать задачу текстом или голосовым\n"
                "- Спросить по задачам — вопросы и управление канбаном\n\n"
                "<b>Создание задачи:</b>\n"
                "Нажми «Задать задачу» и опиши что нужно. Можно текстом или голосовым.\n"
                "Агент спросит на какую доску и этап поставить.\n\n"
                "<b>Управление через «Спросить»:</b>\n"
                "- Какие задачи у Амира?\n"
                "- Переставь задачу X на следующий этап\n"
                "- Перенеси задачу X на доску Marketing\n"
                "- Отметь задачу X выполненной\n"
                "- Удали задачу X\n"
                "- Напомни через 30 минут проверить задачу\n\n"
                "<b>Веб-трекер:</b>\n"
                f"{_enter_url(user.id)}\n"
                "Канбан, редактирование задач, комментарии, файлы.\n\n"
                "<b>Уведомления:</b>\n"
                "- При назначении задачи\n"
                "- Напоминания о дедлайнах 3 раза в день\n"
                "- Утренняя сводка руководителям"
            )
            await _reply(update, instructions, _menu_kb())
            return

        if text == "Просрочки":
            overdue = await get_overdue_tasks(session, user_id=user.id)
            hot = await get_hot_tasks(session, 48, user_id=user.id)
            bugs = await get_user_bugs(session, user.id)
            reply = format_overdue_block(overdue, hot, bugs) + f"\n\n{_enter_url(user.id)}"
            await _reply(update, reply, _menu_kb())
            return

        # Smart assistant mode
        if text in ("Задать задачу", "Спросить по задачам"):
            context.user_data["chat_mode"] = True
            context.user_data["chat_history"] = []
            await _reply(update, "Слушаю. Опиши что нужно сделать или спроси о задачах.")
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


async def _process_smart(update, context, text, session, user):
    """Send message to smart assistant and handle the action."""
    ctx_data = await _get_context_data(session, user)
    history = context.user_data.get("chat_history", [])

    result = await smart_assistant(text, ctx_data, history)

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
        else:
            await _reply(update, msg)

    elif action == "answer":
        msg = result.get("message", "Не понял.")
        await _reply(update, msg)

    elif action == "create_task":
        context.user_data["pending_task"] = result
        context.user_data["pending_files"] = []

        lines = [f"<b>{result.get('title', '—')}</b>\n"]
        if result.get("assignee_name"):
            lines.append(f"Исполнитель: {result['assignee_name']}")
        lines.append(f"Приоритет: P{result.get('priority', 2)}")
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

        if not task_id:
            title = result.get("task_title", "")
            tasks = await search_tasks_by_title(session, title, limit=1)
            if not tasks:
                await _reply(update, f"Задача «{title}» не найдена.")
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

        await update_task(session, task.id, **changes)
        web = f"{config.WEB_BASE_URL.rstrip('/')}/task/{task.id}"
        await _reply(update, f"Обновлено: <b>{task.title}</b>\n{web}")

    elif action == "delete_task":
        task_id = result.get("task_id")
        if task_id:
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


def create_bot_application() -> Application:
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, handle_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    return app
