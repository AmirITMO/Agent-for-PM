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

ОБЯЗАТЕЛЬНО спроси если не указаны: project_name и status. Спрашивай по одному.
{{"action": "clarify", "message": "вопрос"}}

2. ОТВЕТИТЬ на вопрос о задачах:
{{"action": "answer", "message": "ответ"}}

Ты видишь ВСЕ задачи всех сотрудников. Можешь отвечать:
- «Какие задачи у Амира?» — фильтруй по assignee
- «Какие задачи у CEO?» — найди пользователя с position=CEO, покажи его задачи
- «Что просрочено?» — задачи с due_date < сегодня и статус не done/approved
- «Какие баги?» — is_bug=true
- В ответе ВСЕГДА включай ссылки на задачи из поля link

3. ИЗМЕНИТЬ задачу:
{{"action": "update_task", "task_id": число, "changes": {{"status": "...", "priority": ..., "assignee_name": "...", ...}}}}

Ищи задачу по контексту: «задачу Амира связанную с сайтом» — найди в all_tasks задачу где assignee содержит «Амир» и title содержит «сайт». Используй task_id из данных.

«Переставь на следующий уровень» — посмотри текущий status и поставь следующий по порядку:
backlog→planning, planning→todo, todo→wip, wip→done, done→approved

«Отметь выполненной» = status: "done"

4. УТОЧНИТЬ:
{{"action": "clarify", "message": "вопрос"}}

ВАЖНО:
- Возвращай ТОЛЬКО JSON без markdown и пояснений
- В ответах о задачах включай ссылки
- Понимай контекст: «задачу связанную с сайтом» = ищи по title
- «Мои задачи» = задачи текущего пользователя (current_user)
- Не выдумывай задачи — только из all_tasks"""


async def smart_assistant(user_message: str, context_data: dict,
                          history: list[dict] | None = None) -> dict:
    try:
        client = _get_client()
        messages = [{"role": "system", "content": _build_system_prompt(context_data)}]
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": user_message})

        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            temperature=0,
            max_tokens=1500,
        )
        raw = resp.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"action": "answer", "message": raw if 'raw' in dir() else "Ошибка обработки."}
    except Exception:
        logger.exception("Smart assistant failed")
        return {"action": "answer", "message": "Ошибка. Попробуй ещё раз."}


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
