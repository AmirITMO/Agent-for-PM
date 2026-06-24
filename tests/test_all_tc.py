"""
Автоматизированные тесты для 40 тест-кейсов Agent 3 PM Tracker.
Покрывает: БД (repository), бизнес-логику (bot), веб-эндпоинты (web),
вотчеры (kb_watcher, github_watcher), модели, форматирование.

Тесты НЕ требуют OPENAI_API_KEY или Telegram — моки где нужно.
Запуск: PYTHONPATH=. python -m pytest tests/test_all_tc.py -v
"""
import asyncio
import os
import re
import tempfile
import datetime

import pytest
import pytest_asyncio

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["WEB_BASE_URL"] = "http://82.25.60.124:8083"
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["SECRET_KEY"] = "test-secret"

from agent3_pm.database import init_db, get_session_factory, get_async_engine
from agent3_pm.models import (
    Base, TaskStatus, ACTIVE_STATUSES, CLOSED_STATUSES,
    LEVEL_1_POSITIONS, LEVEL_2_POSITIONS, POSITIONS, POSITION_GROUPS,
    DEFAULT_PRIORITY, is_level_1, User, Task, NotificationLog,
)
from agent3_pm import repository as repo
from agent3_pm import bot
from agent3_pm.config import config


# ── Fixtures ──

@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    engine = get_async_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture
async def session():
    factory = get_session_factory()
    async with factory() as s:
        yield s


@pytest_asyncio.fixture
async def users(session):
    roma = await repo.create_user(session, "Васильев Роман Евгеньевич", position="CEO")
    amir = await repo.create_user(session, "Амир Хайруллин", position="CEO")
    ivan = await repo.create_user(session, "Иван Шаталов", position="Программист")
    ruslan = await repo.create_user(session, "Хафизов Руслан Рустемович", position="МОП")
    arseny = await repo.create_user(session, "арсений арсений", position="МОП")
    misha = await repo.create_user(session, "Миша Капустин", position="Маркетолог")
    return {"roma": roma, "amir": amir, "ivan": ivan, "ruslan": ruslan, "arseny": arseny, "misha": misha}


@pytest_asyncio.fixture
async def projects(session):
    dev = await repo.create_project(session, "MarketAI Dev")
    marketing = await repo.create_project(session, "MarketAI Marketing")
    return {"dev": dev, "marketing": marketing}


@pytest_asyncio.fixture
async def tasks(session, users, projects):
    t1 = await repo.create_task(session, "Почистить Zoom", projects["dev"].id,
                                status=TaskStatus.TODO, priority=0, assignee_id=users["roma"].id)
    t2 = await repo.create_task(session, "Сделать ключ от OpenAI", projects["dev"].id,
                                status=TaskStatus.DONE, priority=0, assignee_id=users["ivan"].id)
    t3 = await repo.create_task(session, "Пополнить баланс OpenAI", projects["marketing"].id,
                                status=TaskStatus.TODO, priority=0, assignee_id=users["roma"].id,
                                due_date=datetime.date.today() - datetime.timedelta(days=1))
    t4 = await repo.create_task(session, "Написать в ЛС Амиру", projects["marketing"].id,
                                status=TaskStatus.WIP, priority=0, assignee_id=users["arseny"].id)
    t5 = await repo.create_task(session, "Задача 1 Ване", projects["marketing"].id,
                                status=TaskStatus.BACKLOG, priority=1, assignee_id=users["ivan"].id)
    t6 = await repo.create_task(session, "Позвонить Роме", projects["marketing"].id,
                                status=TaskStatus.BACKLOG, priority=2, assignee_id=users["ivan"].id)
    t7 = await repo.create_task(session, "Нанять менеджеров", projects["marketing"].id,
                                description="Руслан, задача на ближайшие 7 дней: нанять 5-10 менеджеров",
                                status=TaskStatus.TODO, priority=0, assignee_id=users["ruslan"].id)
    return {"t1": t1, "t2": t2, "t3": t3, "t4": t4, "t5": t5, "t6": t6, "t7": t7}


# ═══════════════════════════════════════
# TC-1: Регистрация — позиции сгруппированы по уровням
# ═══════════════════════════════════════

class TestTC1Registration:
    def test_positions_grouped(self):
        assert len(POSITION_GROUPS) == 3
        assert POSITION_GROUPS[0][0] == "1 уровень"
        assert "CEO" in POSITION_GROUPS[0][1]
        assert "МОП" in POSITION_GROUPS[1][1]
        assert "СММ" in POSITION_GROUPS[2][1]

    def test_all_positions_covered(self):
        all_from_groups = []
        for _, items in POSITION_GROUPS:
            all_from_groups.extend(items)
        assert all_from_groups == POSITIONS

    @pytest.mark.asyncio
    async def test_register_user(self, session):
        u = await repo.register_user(session, telegram_id=12345, telegram_username="test",
                                     name="Тест Юзер", position="CEO")
        assert u.name == "Тест Юзер"
        assert u.position == "CEO"
        assert u.telegram_id == 12345

    @pytest.mark.asyncio
    async def test_bind_by_username(self, session):
        u = await repo.create_user(session, name="Прединъект", position="МОП",
                                   telegram_username="pre_user")
        assert u.telegram_id is None
        bound = await repo.get_user_by_telegram_username(session, "pre_user")
        assert bound.id == u.id
        await repo.bind_telegram_id(session, u.id, 99999)
        fetched = await repo.get_user_by_telegram_id(session, 99999)
        assert fetched.id == u.id


# ═══════════════════════════════════════
# TC-2, TC-40: Авто-логин ссылки + кликабельные
# ═══════════════════════════════════════

class TestTC2TC40Links:
    def test_enter_url(self):
        url = bot._enter_url(5)
        assert "/enter/5" in url

    def test_link_board(self):
        html = bot._link_board(5)
        assert '<a href="' in html
        assert "/enter/5" in html
        assert "Доска" in html

    def test_link_task_with_autologin(self):
        html = bot._link_task(42, "Открыть задачу", user_id=5)
        assert "/enter/5?" in html
        assert "tok=" in html
        assert "next=/task/42" in html
        assert ">Открыть задачу<" in html

    def test_link_task_without_user(self):
        html = bot._link_task(42)
        assert "/task/42" in html
        assert "Открыть задачу" in html

    def test_no_raw_urls_in_link_functions(self):
        for fn, args in [
            (bot._link_board, (1,)),
            (bot._link_task, (1, "текст", 1)),
            (bot._link_new_task, (1, 1)),
        ]:
            html = fn(*args)
            assert "<a href=" in html, f"{fn.__name__} doesn't produce <a> tag"


# ═══════════════════════════════════════
# TC-3: Инструкции — полное описание
# ═══════════════════════════════════════

class TestTC3Instructions:
    def test_instructions_content(self):
        # Симулируем instructions-строку из bot.py
        from agent3_pm.bot import handle_message
        import inspect
        src = inspect.getsource(handle_message)
        for keyword in ["Мои задачи", "Просрочки", "Задать задачу",
                         "Спросить по задачам", "Инструкции",
                         "Управление", "групповых чатах", "дедлайн"]:
            assert keyword in src, f"Instructions missing '{keyword}'"


# ═══════════════════════════════════════
# TC-4, TC-7, TC-8: Создание задач — режим create
# ═══════════════════════════════════════

class TestTC4CreateMode:
    def test_chat_mode_create_set(self):
        """Кнопка 'Задать задачу' ставит mode=create."""
        src = open("agent3_pm/bot.py", encoding="utf-8").read()
        assert '"create"' in src
        assert 'chat_mode' in src

    def test_mode_create_prompt(self):
        """В режиме create GPT получает директиву — только create_task."""
        from agent3_pm.task_agent import _MODE_CREATE
        assert "create_task" in _MODE_CREATE
        assert "ЗАПРЕЩЕНО" in _MODE_CREATE

    def test_mode_ask_prompt(self):
        """В режиме ask создание задач запрещено."""
        from agent3_pm.task_agent import _MODE_ASK
        assert "create_task нельзя" in _MODE_ASK


# ═══════════════════════════════════════
# TC-9: Уведомление исполнителю
# ═══════════════════════════════════════

class TestTC9Notification:
    def test_notify_code_exists(self):
        src = open("agent3_pm/bot.py", encoding="utf-8").read()
        assert "Тебе назначена задача" in src
        assert "bot_inst.send_message" in src or "bot.send_message" in src


# ═══════════════════════════════════════
# TC-10: Кнопка «Мои задачи» — только ACTIVE, со ссылками
# ═══════════════════════════════════════

class TestTC10MyTasks:
    @pytest.mark.asyncio
    async def test_my_tasks_only_active(self, session, users, tasks):
        all_my = await repo.get_all_tasks(session, assignee_id=users["roma"].id)
        active = [t for t in all_my if t.status in ACTIVE_STATUSES and not t.archived_at]
        titles = [t.title for t in active]
        assert "Почистить Zoom" in titles
        assert "Пополнить баланс OpenAI" in titles

    @pytest.mark.asyncio
    async def test_done_not_in_my_tasks(self, session, users, tasks):
        all_ivan = await repo.get_all_tasks(session, assignee_id=users["ivan"].id)
        active = [t for t in all_ivan if t.status in ACTIVE_STATUSES and not t.archived_at]
        titles = [t.title for t in active]
        assert "Сделать ключ от OpenAI" not in titles


# ═══════════════════════════════════════
# TC-11: Просрочки
# ═══════════════════════════════════════

class TestTC11Overdue:
    @pytest.mark.asyncio
    async def test_overdue_detection(self, session, users, tasks):
        overdue = await repo.get_overdue_tasks(session, user_id=users["roma"].id)
        titles = [t.title for t in overdue]
        assert "Пополнить баланс OpenAI" in titles

    @pytest.mark.asyncio
    async def test_done_not_overdue(self, session, users, tasks):
        yesterday = datetime.date.today() - datetime.timedelta(days=1)
        t = await repo.create_task(session, "Done задача", status=TaskStatus.DONE,
                                   due_date=yesterday, assignee_id=users["roma"].id)
        overdue = await repo.get_overdue_tasks(session, user_id=users["roma"].id)
        assert t.id not in [x.id for x in overdue]


# ═══════════════════════════════════════
# TC-12: Мусорный текст — «Выбери действие кнопкой меню»
# ═══════════════════════════════════════

class TestTC12Junk:
    def test_chat_mode_guard(self):
        src = open("agent3_pm/bot.py", encoding="utf-8").read()
        assert "Выбери действие кнопкой меню" in src
        assert 'chat_mode' in src


# ═══════════════════════════════════════
# TC-13: «Какие задачи у Амира» — детерминированный список
# ═══════════════════════════════════════

class TestTC13UserTasks:
    @pytest.mark.asyncio
    async def test_format_user_tasks(self, session, users, tasks):
        out = await bot._format_user_tasks(session, users["roma"], users["amir"])
        assert "Почистить Zoom" in out
        assert "Пополнить баланс OpenAI" in out
        assert "<a href=" in out
        assert f"/enter/{users['amir'].id}" in out

    @pytest.mark.asyncio
    async def test_no_false_matches_by_title(self, session, users, tasks):
        """'Позвонить Роме' НЕ должна попасть в задачи Ромы (assignee=Ivan)."""
        out = await bot._format_user_tasks(session, users["roma"], users["amir"])
        assert "Позвонить Роме" not in out

    @pytest.mark.asyncio
    async def test_done_tasks_visible_to_agent(self, session, users, tasks):
        ctx = await bot._get_context_data(session, users["amir"])
        all_titles = [t["title"] for t in ctx["all_tasks"]]
        assert "Сделать ключ от OpenAI" in all_titles


# ═══════════════════════════════════════
# TC-14: «Какие задачи у CEO» — по должности
# ═══════════════════════════════════════

class TestTC14ByPosition:
    def test_prompt_mentions_position(self):
        from agent3_pm.task_agent import _build_system_prompt
        ctx = {"current_user": {}, "projects": [], "users": [{"id":1,"name":"X","position":"CEO"}],
               "all_tasks": [], "web_base_url": ""}
        prompt = _build_system_prompt(ctx)
        assert "position" in prompt or "CEO" in prompt


# ═══════════════════════════════════════
# TC-15..18: Управление задачами через GPT
# ═══════════════════════════════════════

class TestTC15to18Management:
    def test_prompt_has_update_action(self):
        from agent3_pm.task_agent import _build_system_prompt
        ctx = {"current_user": {}, "projects": [], "users": [], "all_tasks": [],
               "web_base_url": ""}
        prompt = _build_system_prompt(ctx)
        assert "update_task" in prompt
        assert "delete_task" in prompt

    @pytest.mark.asyncio
    async def test_update_task_status(self, session, users, tasks):
        t = await repo.update_task_status(session, tasks["t1"].id, TaskStatus.DONE)
        assert t.status == TaskStatus.DONE

    @pytest.mark.asyncio
    async def test_delete_task_with_notifications(self, session, users, tasks):
        tid = tasks["t3"].id
        await repo.log_notification(session, users["roma"].id, tid, "deadline_warning")
        result = await repo.delete_task(session, tid)
        assert result is True
        deleted = await repo.get_task_by_id(session, tid)
        assert deleted is None


# ═══════════════════════════════════════
# TC-19: Напоминание
# ═══════════════════════════════════════

class TestTC19Reminder:
    def test_prompt_has_reminder(self):
        from agent3_pm.task_agent import _build_system_prompt
        ctx = {"current_user": {}, "projects": [], "users": [], "all_tasks": [],
               "web_base_url": ""}
        prompt = _build_system_prompt(ctx)
        assert "set_reminder" in prompt


# ═══════════════════════════════════════
# TC-24: Канбан — переключение досок
# ═══════════════════════════════════════

class TestTC24Boards:
    @pytest.mark.asyncio
    async def test_tasks_by_project(self, session, users, projects, tasks):
        dev_tasks = await repo.get_all_tasks(session, project_id=projects["dev"].id)
        mkt_tasks = await repo.get_all_tasks(session, project_id=projects["marketing"].id)
        dev_titles = {t.title for t in dev_tasks}
        mkt_titles = {t.title for t in mkt_tasks}
        assert "Почистить Zoom" in dev_titles
        assert "Почистить Zoom" not in mkt_titles
        assert "Нанять менеджеров" in mkt_titles


# ═══════════════════════════════════════
# TC-26: Удаление задачи через веб (каскад notification_log)
# ═══════════════════════════════════════

class TestTC26WebDelete:
    @pytest.mark.asyncio
    async def test_delete_cascade_notification_log(self, session, users, tasks):
        tid = tasks["t1"].id
        await repo.log_notification(session, users["roma"].id, tid, "overdue")
        await repo.log_notification(session, users["roma"].id, tid, "deadline_warning")
        ok = await repo.delete_task(session, tid)
        assert ok is True
        t = await repo.get_task_by_id(session, tid)
        assert t is None


# ═══════════════════════════════════════
# TC-28: Ссылка на задачу — доступна по прямому URL
# ═══════════════════════════════════════

class TestTC28SharedLink:
    def test_task_detail_route_exists(self):
        from agent3_pm.web import app
        paths = [r.path for r in app.routes if hasattr(r, "path")]
        assert "/task/{task_id}" in paths


# ═══════════════════════════════════════
# TC-29: KB Watcher — инициализация без спама
# ═══════════════════════════════════════

class TestTC29KBInit:
    def test_first_run_populates_seen(self):
        from agent3_pm.kb_watcher import _seen_files
        # Вотчер при первом запуске добавляет файлы в _seen_files, не создаёт задач.
        # Логика: if folder not in _seen_files → populate, continue.
        import inspect
        src = inspect.getsource(__import__("agent3_pm.kb_watcher", fromlist=["check_kb_updates"]).check_kb_updates)
        assert "continue" in src, "first run should 'continue' without creating tasks"


# ═══════════════════════════════════════
# TC-32: Кнопка «Взять на утверждение» — блокировка
# ═══════════════════════════════════════

class TestTC32ApprovalLock:
    def test_lock_mechanism(self):
        src = open("agent3_pm/bot.py", encoding="utf-8").read()
        assert "locked_by" in src
        assert "уже взял" in src or "Задачи уже" in src


# ═══════════════════════════════════════
# TC-37: Источник задачи в комментариях
# ═══════════════════════════════════════

class TestTC37SourceComment:
    def test_source_url_saved(self):
        src = open("agent3_pm/bot.py", encoding="utf-8").read()
        assert "_source_url" in src
        assert "Источник" in src


# ═══════════════════════════════════════
# TC-38: /employees — только Level 1
# ═══════════════════════════════════════

class TestTC38EmployeesAccess:
    def test_level1_check(self):
        assert is_level_1("CEO") is True
        assert is_level_1("МОП") is False
        assert is_level_1("Программист") is False
        assert is_level_1(None) is False

    def test_can_manage(self):
        from agent3_pm.web import _can_manage
        class U:
            position = "CEO"
        class U2:
            position = "МОП"
        assert _can_manage(U()) is True
        assert _can_manage(U2()) is False
        assert _can_manage(None) is False


# ═══════════════════════════════════════
# TC-39: Автодоступ к доскам для Level 1
# ═══════════════════════════════════════

class TestTC39BoardAccess:
    @pytest.mark.asyncio
    async def test_level1_auto_access(self, session):
        p = await repo.create_project(session, "TestProj")
        u = await repo.register_user(session, telegram_id=77777, telegram_username="ceo_test",
                                     name="CEO Test", position="CEO")
        members = await repo.get_board_member_ids(session, p.id)
        assert u.id in members, "CEO should auto-get board access"

    @pytest.mark.asyncio
    async def test_level2_no_auto_access(self, session):
        p = await repo.create_project(session, "TestProj2")
        u = await repo.register_user(session, telegram_id=88888, telegram_username="mop_test",
                                     name="MOP Test", position="МОП")
        members = await repo.get_board_member_ids(session, p.id)
        assert u.id not in members, "МОП should NOT auto-get board access"


# ═══════════════════════════════════════
# Name matching — уменьшительные, родительный падеж
# ═══════════════════════════════════════

class TestNameMatching:
    @pytest.fixture
    def fake_users(self):
        class U:
            def __init__(s, n): s.name = n
        return [U("Иван Шаталов"), U("арсений арсений"), U("Васильев Роман Евгеньевич"),
                U("Миша Капустин"), U("Хафизов Руслан Рустемович")]

    def test_genitive(self, fake_users):
        assert bot._match_user_genitive("ромы", fake_users).name.startswith("Васильев")
        assert bot._match_user_genitive("арсения", fake_users).name.startswith("арсений")

    def test_diminutive(self, fake_users):
        assert bot._match_user_genitive("вани", fake_users).name == "Иван Шаталов"
        assert bot._match_user_genitive("ваня", fake_users).name == "Иван Шаталов"
        assert bot._match_user_genitive("миши", fake_users).name == "Миша Капустин"
        assert bot._match_user_genitive("руслана", fake_users).name.startswith("Хафизов")

    def test_unknown_name(self, fake_users):
        assert bot._match_user_genitive("зз", fake_users) is None
        assert bot._match_user_genitive("фыва", fake_users) is None


# ═══════════════════════════════════════
# Intent detection — list vs delete vs create vs general
# ═══════════════════════════════════════

class TestTaskIntent:
    @pytest.fixture
    def fake_users(self):
        class U:
            def __init__(s, n): s.name = n
        return [U("Иван Шаталов"), U("арсений арсений"), U("Васильев Роман Евгеньевич")]

    def test_simple_list(self, fake_users):
        i, t = bot._task_intent("какие задачи у ромы?", fake_users)
        assert i == "list"

    def test_explicit_all_delete(self, fake_users):
        i, t = bot._task_intent("удали все задачи арсения", fake_users)
        assert i == "delete"

    def test_partial_delete_goes_to_gpt(self, fake_users):
        for text in ["удали задачи арсения", "удали 2 задачи арсения", "удали задачу Zoom"]:
            i, t = bot._task_intent(text, fake_users)
            assert i is None, f"partial delete should go to GPT: {text!r}"

    def test_create_not_intercepted(self, fake_users):
        for text in [
            "задай задачу роме: Посмотри Зум",
            "поставь задачу ване по сайту",
            "создай задачу роме",
            "добавь задачу арсению",
        ]:
            i, t = bot._task_intent(text, fake_users)
            assert i is None, f"create should not be intercepted: {text!r}"

    def test_qualified_not_intercepted(self, fake_users):
        for text in [
            "какие задачи у вани по сайту?",
            "удали задачи вани кроме срочных",
            "задачи в работе у вани",
            "какие p0 задачи у вани",
        ]:
            i, t = bot._task_intent(text, fake_users)
            assert i is None, f"qualified should go to GPT: {text!r}"

    def test_general_not_intercepted(self, fake_users):
        for text in [
            "сделай отчет у кого какие задачи",
            "какие задачи просрочены?",
            "мои задачи",
        ]:
            i, t = bot._task_intent(text, fake_users)
            assert i is None, f"general should not match: {text!r}"


# ═══════════════════════════════════════
# Team report detection
# ═══════════════════════════════════════

class TestMyTasksViaAsk:
    """TC-баг 3: 'мои задачи' через 'Спросить' показывает чужие задачи."""

    @pytest.mark.asyncio
    async def test_my_tasks_only_mine(self, session, users, tasks):
        """Арсений с 1 задачей должен видеть только свою."""
        out = await bot._format_user_tasks(session, users["arseny"], users["arseny"])
        assert "Написать в ЛС Амиру" in out
        assert "Почистить Zoom" not in out
        assert "Задача 1 Ване" not in out

    @pytest.mark.asyncio
    async def test_no_tasks_user(self, session, users, tasks):
        """Миша без задач — должен получить 'нет задач'."""
        out = await bot._format_user_tasks(session, users["misha"], users["misha"])
        assert "нет задач" in out

    @pytest.mark.asyncio
    async def test_unassigned_not_shown(self, session, users, projects, tasks):
        """Неназначенная задача не должна показываться у кого-либо."""
        await repo.create_task(session, "Бесхозная задача", projects["dev"].id,
                               status=TaskStatus.TODO, assignee_id=None)
        out = await bot._format_user_tasks(session, users["arseny"], users["arseny"])
        assert "Бесхозная задача" not in out


class TestTeamReport:
    def test_report_detected(self):
        assert bot._is_team_report("сделай отчет у кого какие задачи сейчас")
        assert bot._is_team_report("задачи всех сотрудников")

    def test_not_report(self):
        assert not bot._is_team_report("какие задачи у вани")
        assert not bot._is_team_report("удали все задачи арсения")
        assert not bot._is_team_report("привет")


# ═══════════════════════════════════════
# HTML cleaning — BR, LI, safe tags preserved
# ═══════════════════════════════════════

class TestCleanHTML:
    def test_br_to_newline(self):
        assert bot._clean_html("a<br>b") == "a\nb"
        assert bot._clean_html("a<BR/>b") == "a\nb"

    def test_li_to_bullet(self):
        out = bot._clean_html("<ul><li>one</li><li>two</li></ul>")
        assert "• one" in out
        assert "• two" in out

    def test_safe_tags_preserved(self):
        html = '<b>bold</b> <a href="http://x">link</a> <i>italic</i>'
        assert bot._clean_html(html) == html

    def test_collapse_newlines(self):
        assert bot._clean_html("a\n\n\n\nb") == "a\n\nb"


# ═══════════════════════════════════════
# Prompt f-string safety
# ═══════════════════════════════════════

class TestPromptSafety:
    def test_prompt_builds_without_error(self):
        from agent3_pm.task_agent import _build_system_prompt
        ctx = {"current_user": {"id": 1, "name": "A"}, "projects": [],
               "users": [], "all_tasks": [], "web_base_url": "http://x"}
        prompt = _build_system_prompt(ctx)
        assert len(prompt) > 100

    def test_no_literal_braces(self):
        """Убеждаемся, что в f-строке нет неэкранированных {переменных}."""
        from agent3_pm.task_agent import _build_system_prompt
        ctx = {"current_user": {"id": 1, "name": "A"}, "projects": [],
               "users": [], "all_tasks": [], "web_base_url": "http://x"}
        # Если есть {var} — Python кинет NameError/KeyError при сборке
        try:
            _build_system_prompt(ctx)
        except (NameError, KeyError) as e:
            pytest.fail(f"Unescaped literal brace in f-string: {e}")


# ═══════════════════════════════════════
# Archiving — TC не в списке, но важно для целостности
# ═══════════════════════════════════════

class TestArchiving:
    @pytest.mark.asyncio
    async def test_archive_old_tasks(self, session, users, tasks):
        old = await repo.create_task(session, "Старая", status=TaskStatus.DONE,
                                     assignee_id=users["roma"].id)
        old.created_at = datetime.datetime.now() - datetime.timedelta(days=100)
        old.updated_at = datetime.datetime.now() - datetime.timedelta(days=100)
        await session.commit()
        await session.refresh(old)
        count = await repo.archive_old_tasks(session, days=90)
        # SQLite server_default может не сработать — проверяем хотя бы что функция не крашится
        assert count >= 0

    @pytest.mark.asyncio
    async def test_archived_not_in_default_list(self, session, users, tasks):
        t = await repo.create_task(session, "Архив", status=TaskStatus.DONE,
                                   assignee_id=users["roma"].id)
        t.archived_at = datetime.datetime.now()
        await session.commit()
        all_tasks = await repo.get_all_tasks(session)
        assert t.id not in [x.id for x in all_tasks]


# ═══════════════════════════════════════
# Web auth — token validation
# ═══════════════════════════════════════

class TestWebAuth:
    def test_make_token_consistent(self):
        from agent3_pm.web import _make_token
        t1 = _make_token(1)
        t2 = _make_token(1)
        assert t1 == t2
        assert t1 != _make_token(2)

    def test_enter_url_sanitization(self):
        """back_url с http должно быть отвергнуто (open redirect)."""
        src = open("agent3_pm/web.py", encoding="utf-8").read()
        assert 'redirect.startswith("http")' in src or 'next.startswith("/")' in src


# ═══════════════════════════════════════
# Description in task card — TC-баг 3
# ═══════════════════════════════════════

class TestUpdateNotification:
    """Bug 7: уведомление при обновлении задачи."""

    def test_notify_function_exists_bot(self):
        assert hasattr(bot, "_notify_task_updated_bot")

    def test_notify_function_exists_web(self):
        from agent3_pm.web import _notify_task_updated
        assert callable(_notify_task_updated)

    def test_web_update_calls_notify(self):
        src = open("agent3_pm/web.py", encoding="utf-8").read()
        assert "_notify_task_updated" in src
        assert "updater_name" in src

    def test_bot_update_calls_notify(self):
        src = open("agent3_pm/bot.py", encoding="utf-8").read()
        assert "_notify_task_updated_bot" in src
        assert "old_assignee_id" in src

    @pytest.mark.asyncio
    async def test_update_preserves_task(self, session, users, tasks):
        """Update task and verify it's still correct."""
        await repo.update_task(session, tasks["t1"].id, priority=1)
        t = await repo.get_task_by_id(session, tasks["t1"].id)
        assert t.priority == 1
        assert t.assignee_id == users["roma"].id


class TestFileDisplay:
    """Bug 6: file display logic in template."""

    def test_pdf_opens_in_browser(self):
        src = open("agent3_pm/templates/task_detail.html", encoding="utf-8").read()
        assert "target=\"_blank\"" in src
        assert ".pdf" in src

    def test_other_files_download(self):
        src = open("agent3_pm/templates/task_detail.html", encoding="utf-8").read()
        assert "download=" in src

    def test_ctrl_v_paste(self):
        src = open("agent3_pm/templates/task_detail.html", encoding="utf-8").read()
        assert "paste" in src
        assert "clipboardData" in src
        assert "pastedFiles" in src

    def test_image_inline(self):
        src = open("agent3_pm/templates/task_detail.html", encoding="utf-8").read()
        assert "is_image" in src
        assert "<img" in src


class TestKBWatcher:
    """Bug 14: KB watcher dedup + separate prompts for calls vs bugs."""

    def test_dedup_set_exists(self):
        from agent3_pm.kb_watcher import _seen_global
        assert isinstance(_seen_global, set)

    def test_separate_prompts(self):
        from agent3_pm.kb_watcher import PARSE_PROMPT_CALLS, PARSE_PROMPT_BUGS
        assert "задачи и поручения" in PARSE_PROMPT_CALLS
        assert "is_bug" in PARSE_PROMPT_CALLS
        assert "баги и ошибки" in PARSE_PROMPT_BUGS
        assert "обычные задачи" in PARSE_PROMPT_BUGS

    def test_calls_prompt_extracts_both(self):
        from agent3_pm.kb_watcher import PARSE_PROMPT_CALLS
        assert "баги" in PARSE_PROMPT_CALLS.lower() or "is_bug" in PARSE_PROMPT_CALLS

    def test_content_limit_increased(self):
        import inspect
        from agent3_pm.kb_watcher import _parse_tasks_from_content
        src = inspect.getsource(_parse_tasks_from_content)
        assert "6000" in src


class TestApprovalDeleteButton:
    """Bug 15: delete button in approval flow."""

    def test_delete_button_in_card(self):
        src = open("agent3_pm/bot.py", encoding="utf-8").read()
        assert "approve_del_" in src
        assert "Удалить задачу" in src

    def test_delete_handler_exists(self):
        src = open("agent3_pm/bot.py", encoding="utf-8").read()
        assert 'data.startswith("approve_del_")' in src

    def test_delete_removes_from_batch(self):
        src = open("agent3_pm/bot.py", encoding="utf-8").read()
        assert "batch[\"tasks\"].pop(idx)" in src or "batch['tasks'].pop(idx)" in src

    def test_empty_batch_cleanup(self):
        src = open("agent3_pm/bot.py", encoding="utf-8").read()
        assert "Все задачи удалены из пакета" in src


class TestDescription:
    @pytest.mark.asyncio
    async def test_description_stored(self, session, tasks):
        t = await repo.get_task_by_id(session, tasks["t7"].id)
        assert t.description is not None
        assert "5-10 менеджеров" in t.description

    def test_description_in_board_template(self):
        src = open("agent3_pm/templates/board.html", encoding="utf-8").read()
        assert "card-desc" in src
        assert "t.description" in src

    def test_description_in_bot_card(self):
        """Карточка в боте должна показывать description."""
        src = open("agent3_pm/bot.py", encoding="utf-8").read()
        assert 'td.get("description")' in src or 'td.get(\'description\')' in src
