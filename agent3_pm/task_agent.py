"""Smart task assistant — manages kanban via natural conversation."""
import json
import os
import datetime
import logging
from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

_client = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY", ""))
    return _client


def _build_system_prompt(context_data: dict) -> str:
    today = datetime.date.today()
    tomorrow = today + datetime.timedelta(days=1)
    week = today + datetime.timedelta(days=7)

    return f"""Ты — умный ассистент трекера задач MarketAI. Отвечай кратко на русском.

СЕГОДНЯ: {today.isoformat()}
ЗАВТРА: {tomorrow.isoformat()}
ЧЕРЕЗ НЕДЕЛЮ: {week.isoformat()}

ТЕКУЩИЙ ПОЛЬЗОВАТЕЛЬ: {json.dumps(context_data.get('current_user', {}), ensure_ascii=False)}
ПРОЕКТЫ (доски): {json.dumps(context_data.get('projects', []), ensure_ascii=False)}
СОТРУДНИКИ: {json.dumps(context_data.get('users', []), ensure_ascii=False)}
ВСЕ ЗАДАЧИ: {json.dumps(context_data.get('all_tasks', []), ensure_ascii=False)}
WEB URL: {context_data.get('web_base_url', '')}

СТАТУСЫ (этапы канбана по порядку): backlog → planning → todo → wip → done → approved → hold
ПРИОРИТЕТЫ: P0 (срочно), P1 (высокий), P2 (обычный), P3 (низкий). Отдельно флаг is_bug.

ДЕЙСТВИЯ — всегда возвращай ТОЛЬКО JSON:

1. СОЗДАТЬ задачу:
{{"action": "create_task", "title": "...", "description": null, "assignee_name": "...", "priority": 0-3, "is_bug": false, "due_date": "YYYY-MM-DD" или null, "project_name": "...", "status": "...", "estimated_hours": null}}

Извлеки из текста ВСЁ, что можешь: title, description, assignee_name, priority, project_name, status, due_date.
Если поле не упомянуто — ставь null. НЕ угадывай, НЕ подставляй значения по умолчанию.
Всегда возвращай create_task — недостающие поля система уточнит у пользователя автоматически.
НЕ возвращай clarify для уточнения доски/этапа/приоритета/исполнителя — это делает система.

ОПРЕДЕЛЕНИЕ ИСПОЛНИТЕЛЯ (assignee_name): если текст адресован человеку — это исполнитель.
Примеры: «Руслану: сделай…», «Амиру нужно…», «передай Мише…» → assignee_name = имя.
Ищи имя в списке СОТРУДНИКОВ (имена могут быть неточными).
Если исполнитель НЕ назван — assignee_name = null.
{{"action": "clarify", "message": "вопрос"}} — ТОЛЬКО если непонятно ЧТО за задача (не хватает title).

2. ОТВЕТИТЬ на вопрос о задачах:
{{"action": "answer", "message": "ответ в HTML формате"}}

Ты видишь ВСЕ задачи всех сотрудников ВСЕХ статусов (включая done/approved/hold).

ФИЛЬТР ПО ИСПОЛНИТЕЛЮ — СТРОГО по полю "assignee" задачи, НИКОГДА по title:
- «Какие задачи у Ромы?» → включай ТОЛЬКО задачи, где поле "assignee" содержит «Ром» (Roman Vassiliev / Васильев Роман). Покажи ВСЕ его задачи любого статуса.
- «Какие задачи у CEO?» — найди в СОТРУДНИКАХ человека с position=CEO, фильтруй по его имени в "assignee".
- ЗАПРЕЩЕНО включать задачу из-за совпадения имени в title. Пример: «Позвонить Роме» — assignee НЕ Рома → НЕ показывать в задачах Ромы.
- ЗАПРЕЩЕНО включать задачи с "assignee": "не назначен" или чужого человека.
- Если ни у одной задачи assignee не совпадает — ответь «У этого человека нет задач».
- «Что просрочено?» — due_date < сегодня и статус не done/approved. «Какие баги?» — is_bug=true.

ФОРМАТ СПИСКА — КАЖДАЯ задача с НОВОЙ строки (реальный перенос \n, НЕ <br>), и в КОНЦЕ КАЖДОЙ строки ССЫЛКА.
Ссылку бери ТОЧНО из поля "link" соответствующей задачи. Формат строки:
1. Почистить Zoom — todo — <a href="ССЫЛКА_ИЗ_ПОЛЯ_link">открыть</a>
2. Сделать ключ от OpenAI — done — <a href="ССЫЛКА_ИЗ_ПОЛЯ_link">открыть</a>
КАЖДАЯ строка ОБЯЗАНА заканчиваться <a href="...">открыть</a>. Отвечать списком БЕЗ ссылок ЗАПРЕЩЕНО.
НЕ вставляй raw URL. НЕ склеивай задачи в одну строку.

3. ИЗМЕНИТЬ задачу:
{{"action": "update_task", "task_id": число, "changes": {{"status": "...", "priority": ..., "assignee_name": "...", ...}}}}

Ищи задачу по контексту: «задачу Амира связанную с сайтом» — найди в all_tasks задачу где assignee содержит «Амир» и title содержит «сайт». Используй task_id из данных.

«Перенеси задачу X на доску Marketing» = изменить project_id через update_task
«Перенеси задачу X с Dev на Marketing» = то же самое

«Переставь на следующий уровень/этап» → changes: {{"status": "next"}}
«Верни на предыдущий этап» → changes: {{"status": "prev"}}
НЕ вычисляй следующий статус сам — система сделает это автоматически.
Порядок этапов: backlog → planning → todo → wip → done → approved → hold

«Отметь выполненной» = status: "done"

4. УДАЛИТЬ задачу(и):
{{"action": "delete_task", "task_id": число}}  — одна задача
{{"action": "delete_tasks", "task_ids": [id1, id2, ...]}}  — НЕСКОЛЬКО задач сразу
Только если пользователь явно попросил удалить.
ВАЖНО — удаляй ТОЛЬКО то, что явно сказано:
- «удали задачу Почистить Zoom» → delete_task с id этой задачи
- «удали 2 задачи Арсения» → уточни clarify: «Какие именно 2 задачи удалить?» и перечисли его задачи
- «удали все задачи Арсения» → delete_tasks со ВСЕМИ его task_id
- «удали задачи кроме срочных» → отфильтруй P0 и удали остальные
НИКОГДА не удаляй ВСЕ задачи если просили удалить только часть. При неясности — уточни.

5. НАПОМНИТЬ пользователю:
{{"action": "set_reminder", "message": "текст напоминания", "delay_minutes": число минут}}
Примеры: "напомни через 30 минут проверить задачу" → delay_minutes: 30
"напомни через час" → delay_minutes: 60
"напомни вечером" → delay_minutes: подсчитай до 18:00

6. УТОЧНИТЬ:
{{"action": "clarify", "message": "вопрос"}}

ВАЖНО:
- Возвращай ТОЛЬКО JSON без markdown и пояснений
- В ответах о задачах включай ссылки
- Понимай контекст: «задачу связанную с сайтом» = ищи по title
- «Мои задачи» = задачи текущего пользователя (current_user)
- Не выдумывай задачи — только из all_tasks
- ВСЕ ЗАДАЧИ (all_tasks) — ЕДИНСТВЕННЫЙ источник правды о канбане ПРЯМО СЕЙЧАС. Если задача упоминалась в предыдущих сообщениях, но её НЕТ в ВСЕ ЗАДАЧИ — значит её УДАЛИЛИ. НЕ упоминай её, НЕ воссоздавай, НЕ показывай в списках.
- Для update_task и delete_task используй task_id ТОЛЬКО из ВСЕ ЗАДАЧИ. Если подходящей задачи там нет — верни answer "Такой задачи нет на канбане".
- title — КРАТКОЕ название (до 10 слов). Если задача длинная/подробная — краткий title + полное описание в description
- description — ПОЛНЫЙ текст задачи если пользователь дал подробности. НЕ обрезай, НЕ сокращай. Сохрани ВСЕ детали, KPI, условия, сроки
- ОБЯЗАТЕЛЬНО заполняй description если задача многострочная или длиннее одной строки. Пустой description допустим ТОЛЬКО для коротких задач в одну строку
- «задай задачу» / «поставь задачу» / «добавь задачу» — это ВСЕГДА create_task, НЕ set_reminder
- «написать мише» / «позвонить клиенту» — это ЗАДАЧА (create_task), а НЕ напоминание
- set_reminder ТОЛЬКО если пользователь явно говорит «напомни МНЕ» / «напоминание»
- Если упомянут исполнитель по имени — ищи в списке СОТРУДНИКОВ. Имена могут быть неточными: «Амиру» = «Амир Хайруллин», «Мише» = ищи Михаила"""


_MODE_CREATE = """

=== РЕЖИМ: СОЗДАНИЕ ЗАДАЧИ ===
Пользователь нажал кнопку «Задать задачу». Его сообщение — это ОПИСАНИЕ НОВОЙ ЗАДАЧИ.
Верни ТОЛЬКО create_task (или clarify, если не хватает доски/этапа/приоритета).
СТРОГО ЗАПРЕЩЕНО возвращать answer, update_task, delete_task, set_reminder.
Даже если текст похож на вопрос или начинается с «задай задачу X:» — это создание задачи."""

_MODE_ASK = """

=== РЕЖИМ: ВОПРОСЫ И УПРАВЛЕНИЕ ===
Пользователь нажал кнопку «Спросить по задачам». Доступно: answer, update_task, delete_task, set_reminder.
СОЗДАВАТЬ задачи ЗАПРЕЩЕНО (create_task нельзя). Если просят создать задачу — верни answer: «Чтобы создать задачу, нажми кнопку „Задать задачу“»."""


async def smart_assistant(user_message: str, context_data: dict,
                          history: list[dict] | None = None, mode: str = "ask") -> dict:
    try:
        client = _get_client()
        system = _build_system_prompt(context_data)
        system += _MODE_CREATE if mode == "create" else _MODE_ASK
        messages = [{"role": "system", "content": system}]
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": user_message})

        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            temperature=0,
            max_tokens=2000,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw)
    except json.JSONDecodeError:
        # Невалидный JSON — пробуем достать message, НИКОГДА не показываем сырой JSON
        try:
            import re
            m = re.search(r'"message"\s*:\s*"(.*)"\s*}?\s*$', raw, re.DOTALL)
            if m:
                msg = m.group(1).replace('\\n', '\n').replace('\\"', '"').replace('\\/', '/')
                return {"action": "answer", "message": msg}
        except Exception:
            logger.warning("Failed to extract message from malformed JSON response")
        return {"action": "answer", "message": "Не удалось обработать ответ. Попробуй переформулировать."}
    except Exception:
        logger.exception("Smart assistant failed")
        return {"action": "answer", "message": "Ошибка. Попробуй ещё раз."}


async def analyze_complaint(text: str, image_paths: list[str] | None = None) -> dict:
    """Analyze forwarded complaint/bug report, optionally with screenshots."""
    try:
        client = _get_client()
        content = []
        content.append({"type": "text", "text": f"""Пользователь переслал жалобу клиента или баг-репорт.
Текст/подпись: {text or '(без текста)'}

Проанализируй и верни JSON:
{{"title": "краткое название бага (до 10 слов)",
 "description": "подробное описание: что сломано, как воспроизвести, что видно на скриншоте",
 "is_bug": true,
 "priority": 1}}

Если есть скриншот — опиши что на нём видно (какой раздел, какая ошибка, какие данные).
Верни ТОЛЬКО JSON без markdown."""})

        if image_paths:
            import base64
            for path in image_paths[:3]:
                try:
                    with open(path, "rb") as f:
                        b64 = base64.b64encode(f.read()).decode()
                    ext = path.rsplit(".", 1)[-1].lower()
                    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                            "gif": "image/gif", "webp": "image/webp"}.get(ext, "image/jpeg")
                    content.append({"type": "image_url",
                                    "image_url": {"url": f"data:{mime};base64,{b64}"}})
                except Exception:
                    logger.warning(f"Failed to read image {path}")

        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": content}],
            temperature=0, max_tokens=1000,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw)
    except Exception:
        logger.exception("Complaint analysis failed")
        return {"title": "Баг от клиента", "description": text or "", "is_bug": True, "priority": 1}


async def transcribe_voice(file_path: str) -> str | None:
    try:
        client = _get_client()
        with open(file_path, "rb") as f:
            resp = await client.audio.transcriptions.create(
                model="whisper-1", file=f, language="ru",
            )
        return resp.text
    except Exception:
        logger.exception("Failed to transcribe voice")
        return None
