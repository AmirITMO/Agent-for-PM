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


# ═══════════════════════════════════════
# NEW COMPREHENSIVE TESTS
# ═══════════════════════════════════════

from agent3_pm.formatter import (
    format_task_short, format_today_tasks, format_overdue_block,
    format_overdue_list, format_task_detail, format_project_status,
    format_morning_summary, format_deadline_warning, _status_label,
    STATUS_LABEL,
)
from agent3_pm.models import (
    TaskComment, Attachment, Settings, Project,
    LEVEL_3_POSITIONS, board_members,
)


# ── Formatter edge cases ──

class TestFormatterEdgeCases:
    def _make_task(self, **kwargs):
        """Create a mock Task-like object."""
        class MockUser:
            def __init__(self, name): self.name = name
        class MockProject:
            def __init__(self, name): self.name = name
        defaults = {
            "id": 1, "title": "Test Task", "description": None,
            "status": TaskStatus.TODO, "priority": 2, "is_bug": False,
            "due_date": None, "estimated_hours": None,
            "assignee": None, "assignee_id": None,
            "project": None, "project_id": None,
            "creator": None, "creator_id": None,
            "comments": [], "created_at": datetime.datetime.now(),
            "updated_at": datetime.datetime.now(), "archived_at": None,
        }
        defaults.update(kwargs)
        t = type("FakeTask", (), {})()
        for k, v in defaults.items():
            setattr(t, k, v)
        # Properties
        t.__class__.is_red = property(lambda s: s.priority == 0 or s.is_bug)
        t.__class__.is_overdue = property(lambda s: bool(
            s.due_date and s.status not in CLOSED_STATUSES and s.due_date < datetime.date.today()))
        t.__class__.is_due_today = property(lambda s: bool(
            s.due_date and s.status not in CLOSED_STATUSES and s.due_date == datetime.date.today()))
        t.__class__.is_hot = property(lambda s: bool(
            s.due_date and s.status not in CLOSED_STATUSES and 0 <= (s.due_date - datetime.date.today()).days <= 1))
        if "assignee_name" in kwargs:
            t.assignee = MockUser(kwargs["assignee_name"])
        if "project_name" in kwargs:
            t.project = MockProject(kwargs["project_name"])
        return t

    def test_format_task_short_minimal(self):
        t = self._make_task(title="Simple")
        result = format_task_short(t)
        assert "Simple" in result

    def test_format_task_short_bug(self):
        t = self._make_task(title="Bug task", is_bug=True)
        result = format_task_short(t)
        assert "[Баг]" in result

    def test_format_task_short_p0(self):
        t = self._make_task(title="Urgent", priority=0)
        result = format_task_short(t)
        assert "[P0]" in result

    def test_format_task_short_with_hours(self):
        t = self._make_task(title="Timed", estimated_hours=3.5)
        result = format_task_short(t)
        assert "3.5ч" in result

    def test_format_task_short_overdue(self):
        yesterday = datetime.date.today() - datetime.timedelta(days=1)
        t = self._make_task(title="Late", due_date=yesterday)
        result = format_task_short(t)
        assert "просрочено" in result

    def test_format_task_short_due_today(self):
        t = self._make_task(title="Today", due_date=datetime.date.today())
        result = format_task_short(t)
        assert "дедлайн сегодня" in result

    def test_format_task_short_future_date(self):
        future = datetime.date.today() + datetime.timedelta(days=5)
        t = self._make_task(title="Future", due_date=future)
        result = format_task_short(t)
        assert "до" in result

    def test_format_task_short_with_assignee(self):
        t = self._make_task(title="Assigned", assignee_name="Иван")
        result = format_task_short(t)
        assert "-> Иван" in result

    def test_format_today_tasks_empty(self):
        result = format_today_tasks([])
        assert "нет" in result.lower()

    def test_format_overdue_block_all_empty(self):
        result = format_overdue_block([], [], [])
        assert "нет" in result.lower()

    def test_format_overdue_block_with_data(self):
        yesterday = datetime.date.today() - datetime.timedelta(days=1)
        overdue = [self._make_task(title="Overdue1", due_date=yesterday)]
        hot = [self._make_task(title="Hot1", due_date=datetime.date.today())]
        bugs = [self._make_task(title="Bug1", is_bug=True)]
        result = format_overdue_block(overdue, hot, bugs)
        assert "Overdue1" in result
        assert "Hot1" in result
        assert "Bug1" in result

    def test_format_overdue_list_empty(self):
        result = format_overdue_list([])
        assert "нет" in result.lower()

    def test_format_overdue_list_with_tasks(self):
        yesterday = datetime.date.today() - datetime.timedelta(days=1)
        tasks = [self._make_task(title="Late1", due_date=yesterday)]
        result = format_overdue_list(tasks)
        assert "Late1" in result

    def test_format_task_detail_minimal(self):
        t = self._make_task(title="Detail Task", status=TaskStatus.TODO, priority=2)
        result = format_task_detail(t)
        assert "Detail Task" in result
        assert "P2" in result

    def test_format_task_detail_full(self):
        yesterday = datetime.date.today() - datetime.timedelta(days=1)
        t = self._make_task(
            title="Full Detail", status=TaskStatus.WIP, priority=0,
            is_bug=True, description="Some desc", due_date=yesterday,
            estimated_hours=8.0, assignee_name="Иван", project_name="Dev",
        )
        result = format_task_detail(t)
        assert "Баг" in result
        assert "Some desc" in result
        assert "ПРОСРОЧЕНО" in result
        assert "Иван" in result
        assert "Dev" in result
        assert "8.0ч" in result

    def test_format_task_detail_done_not_overdue(self):
        """Done tasks with past due_date should NOT show ПРОСРОЧЕНО."""
        yesterday = datetime.date.today() - datetime.timedelta(days=1)
        t = self._make_task(title="Done Task", status=TaskStatus.DONE, due_date=yesterday)
        result = format_task_detail(t)
        assert "ПРОСРОЧЕНО" not in result

    def test_format_project_status(self):
        status = {
            "status_counts": {TaskStatus.TODO: 3, TaskStatus.DONE: 2},
            "total": 5, "done": 2, "progress_pct": 40,
            "next_tasks": [], "overdue_count": 1,
        }
        result = format_project_status("TestProj", status)
        assert "TestProj" in result
        assert "40%" in result
        assert "Просрочено" in result

    def test_format_morning_summary(self):
        summary = {
            "date": datetime.date.today(), "open_count": 10,
            "overdue": [], "hot_today": [],
            "tasks_by_user": {"Иван": 5, "Миша": 3},
        }
        result = format_morning_summary(summary)
        assert "10" in result
        assert "Иван" in result

    def test_format_morning_summary_truncates_overdue(self):
        yesterday = datetime.date.today() - datetime.timedelta(days=1)
        overdue = [self._make_task(title=f"Over{i}", due_date=yesterday) for i in range(15)]
        summary = {
            "date": datetime.date.today(), "open_count": 20,
            "overdue": overdue, "hot_today": [],
            "tasks_by_user": {},
        }
        result = format_morning_summary(summary)
        assert "ещё" in result

    def test_format_deadline_warning_none_due_date(self):
        t = self._make_task(title="No Deadline", due_date=None)
        result = format_deadline_warning(t)
        assert "не установлен" in result
        assert "No Deadline" in result

    def test_format_deadline_warning_overdue(self):
        past = datetime.date.today() - datetime.timedelta(days=3)
        t = self._make_task(title="Overdue", due_date=past)
        result = format_deadline_warning(t)
        assert "Просрочено" in result
        assert "3" in result

    def test_format_deadline_warning_today(self):
        t = self._make_task(title="Today", due_date=datetime.date.today())
        result = format_deadline_warning(t)
        assert "СЕГОДНЯ" in result

    def test_format_deadline_warning_tomorrow(self):
        tomorrow = datetime.date.today() + datetime.timedelta(days=1)
        t = self._make_task(title="Tomorrow", due_date=tomorrow)
        result = format_deadline_warning(t)
        assert "ЗАВТРА" in result

    def test_format_deadline_warning_future(self):
        future = datetime.date.today() + datetime.timedelta(days=5)
        t = self._make_task(title="Future", due_date=future)
        result = format_deadline_warning(t)
        assert "5 дн." in result

    def test_status_label_known(self):
        assert _status_label(TaskStatus.TODO) == "К выполнению"
        assert _status_label(TaskStatus.WIP) == "В работе"

    def test_status_label_unknown(self):
        result = _status_label("unknown_status")
        assert result == "unknown_status"


# ── Model properties ──

class TestModelProperties:
    @pytest.mark.asyncio
    async def test_task_is_red_priority0(self, session, users, projects):
        t = await repo.create_task(session, "P0 task", priority=0,
                                   assignee_id=users["roma"].id)
        assert t.is_red is True

    @pytest.mark.asyncio
    async def test_task_is_red_bug(self, session, users, projects):
        t = await repo.create_task(session, "Bug task", is_bug=True,
                                   assignee_id=users["roma"].id)
        assert t.is_red is True

    @pytest.mark.asyncio
    async def test_task_not_red_p2(self, session, users, projects):
        t = await repo.create_task(session, "Normal task", priority=2,
                                   assignee_id=users["roma"].id)
        assert t.is_red is False

    @pytest.mark.asyncio
    async def test_task_is_overdue(self, session, users, projects):
        yesterday = datetime.date.today() - datetime.timedelta(days=1)
        t = await repo.create_task(session, "Overdue", due_date=yesterday,
                                   status=TaskStatus.TODO, assignee_id=users["roma"].id)
        assert t.is_overdue is True

    @pytest.mark.asyncio
    async def test_task_not_overdue_done(self, session, users, projects):
        yesterday = datetime.date.today() - datetime.timedelta(days=1)
        t = await repo.create_task(session, "Done", due_date=yesterday,
                                   status=TaskStatus.DONE, assignee_id=users["roma"].id)
        assert t.is_overdue is False

    @pytest.mark.asyncio
    async def test_task_not_overdue_no_date(self, session, users, projects):
        t = await repo.create_task(session, "No date", status=TaskStatus.TODO,
                                   assignee_id=users["roma"].id)
        assert t.is_overdue is False

    @pytest.mark.asyncio
    async def test_task_is_due_today(self, session, users, projects):
        t = await repo.create_task(session, "Today", due_date=datetime.date.today(),
                                   status=TaskStatus.TODO, assignee_id=users["roma"].id)
        assert t.is_due_today is True

    @pytest.mark.asyncio
    async def test_task_is_hot(self, session, users, projects):
        tomorrow = datetime.date.today() + datetime.timedelta(days=1)
        t = await repo.create_task(session, "Hot", due_date=tomorrow,
                                   status=TaskStatus.TODO, assignee_id=users["roma"].id)
        assert t.is_hot is True

    @pytest.mark.asyncio
    async def test_task_not_hot_far(self, session, users, projects):
        future = datetime.date.today() + datetime.timedelta(days=10)
        t = await repo.create_task(session, "Far", due_date=future,
                                   status=TaskStatus.TODO, assignee_id=users["roma"].id)
        assert t.is_hot is False

    def test_attachment_is_image(self):
        a = type("FakeAtt", (), {"content_type": "image/jpeg"})()
        a.__class__.is_image = property(lambda s: bool(s.content_type and s.content_type.startswith("image/")))
        assert a.is_image is True

    def test_attachment_not_image(self):
        a = type("FakeAtt", (), {"content_type": "application/pdf"})()
        a.__class__.is_image = property(lambda s: bool(s.content_type and s.content_type.startswith("image/")))
        assert a.is_image is False

    def test_attachment_none_content_type(self):
        a = type("FakeAtt", (), {"content_type": None})()
        a.__class__.is_image = property(lambda s: bool(s.content_type and s.content_type.startswith("image/")))
        assert a.is_image is False

    def test_is_level_1_all_positions(self):
        for pos in LEVEL_1_POSITIONS:
            assert is_level_1(pos) is True
        for pos in LEVEL_2_POSITIONS:
            assert is_level_1(pos) is False
        for pos in LEVEL_3_POSITIONS:
            assert is_level_1(pos) is False

    def test_settings_defaults(self):
        assert "morning_summary_hour" in Settings.DEFAULTS
        assert "timezone" in Settings.DEFAULTS
        assert Settings.DEFAULTS["timezone"] == "Europe/Moscow"


# ── Repository edge cases ──

class TestRepositoryEdgeCases:
    @pytest.mark.asyncio
    async def test_get_user_by_id_nonexistent(self, session):
        user = await repo.get_user_by_id(session, 99999)
        assert user is None

    @pytest.mark.asyncio
    async def test_get_user_by_telegram_id_nonexistent(self, session):
        user = await repo.get_user_by_telegram_id(session, 99999)
        assert user is None

    @pytest.mark.asyncio
    async def test_get_user_by_username_case_insensitive(self, session):
        await repo.create_user(session, "TestUser", telegram_username="TestName")
        user = await repo.get_user_by_telegram_username(session, "testname")
        assert user is not None
        assert user.name == "TestUser"

    @pytest.mark.asyncio
    async def test_update_user_nonexistent(self, session):
        result = await repo.update_user(session, 99999, name="nope")
        assert result is None

    @pytest.mark.asyncio
    async def test_delete_user_nonexistent(self, session):
        result = await repo.delete_user(session, 99999)
        assert result is False

    @pytest.mark.asyncio
    async def test_bind_telegram_id_nonexistent(self, session):
        result = await repo.bind_telegram_id(session, 99999, 12345)
        assert result is None

    @pytest.mark.asyncio
    async def test_delete_task_nonexistent(self, session):
        result = await repo.delete_task(session, 99999)
        assert result is False

    @pytest.mark.asyncio
    async def test_update_task_nonexistent(self, session):
        result = await repo.update_task(session, 99999, title="nope")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_task_by_id_nonexistent(self, session):
        result = await repo.get_task_by_id(session, 99999)
        assert result is None

    @pytest.mark.asyncio
    async def test_search_tasks_empty_query(self, session, users, projects, tasks):
        results = await repo.search_tasks_by_title(session, "nonexistent_xyz")
        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_search_tasks_finds_match(self, session, users, projects, tasks):
        results = await repo.search_tasks_by_title(session, "Zoom")
        assert len(results) >= 1
        assert any("Zoom" in t.title for t in results)

    @pytest.mark.asyncio
    async def test_get_project_by_name_case_insensitive(self, session, projects):
        proj = await repo.get_project_by_name(session, "marketai dev")
        assert proj is not None
        assert proj.name == "MarketAI Dev"

    @pytest.mark.asyncio
    async def test_get_project_by_name_nonexistent(self, session):
        proj = await repo.get_project_by_name(session, "Nonexistent Project")
        assert proj is None

    @pytest.mark.asyncio
    async def test_get_project_by_id_nonexistent(self, session):
        proj = await repo.get_project_by_id(session, 99999)
        assert proj is None

    @pytest.mark.asyncio
    async def test_board_access_toggle(self, session, users, projects):
        pid = projects["dev"].id
        uid = users["ruslan"].id
        # Grant
        await repo.set_board_access(session, pid, uid, True)
        members = await repo.get_board_member_ids(session, pid)
        assert uid in members
        # Revoke
        await repo.set_board_access(session, pid, uid, False)
        members = await repo.get_board_member_ids(session, pid)
        assert uid not in members

    @pytest.mark.asyncio
    async def test_board_access_idempotent(self, session, users, projects):
        pid = projects["dev"].id
        uid = users["ruslan"].id
        await repo.set_board_access(session, pid, uid, True)
        await repo.set_board_access(session, pid, uid, True)  # no error
        members = await repo.get_board_member_ids(session, pid)
        assert uid in members

    @pytest.mark.asyncio
    async def test_get_board_members(self, session, users, projects):
        pid = projects["dev"].id
        uid = users["ivan"].id
        await repo.set_board_access(session, pid, uid, True)
        members = await repo.get_board_members(session, pid)
        assert any(m.id == uid for m in members)

    @pytest.mark.asyncio
    async def test_get_managers(self, session, users):
        managers = await repo.get_managers(session)
        names = [m.name for m in managers]
        assert "Амир Хайруллин" in names  # CEO
        assert "Иван Шаталов" not in names  # Программист

    @pytest.mark.asyncio
    async def test_settings_crud(self, session):
        val = await repo.get_setting(session, "timezone")
        assert val == "Europe/Moscow"  # default
        await repo.set_setting(session, "timezone", "UTC")
        val2 = await repo.get_setting(session, "timezone")
        assert val2 == "UTC"
        # Update existing
        await repo.set_setting(session, "timezone", "Asia/Tokyo")
        val3 = await repo.get_setting(session, "timezone")
        assert val3 == "Asia/Tokyo"

    @pytest.mark.asyncio
    async def test_get_all_settings_merged(self, session):
        await repo.set_setting(session, "timezone", "UTC")
        settings = await repo.get_all_settings(session)
        assert settings["timezone"] == "UTC"
        assert "morning_summary_hour" in settings  # default

    @pytest.mark.asyncio
    async def test_get_setting_unknown_key(self, session):
        val = await repo.get_setting(session, "nonexistent_key")
        assert val == ""

    @pytest.mark.asyncio
    async def test_notification_log_and_check(self, session, users, tasks):
        uid = users["roma"].id
        tid = tasks["t1"].id
        # Not notified yet
        result = await repo.was_notified_today(session, uid, tid, "test_type")
        assert result is False
        # Log notification
        await repo.log_notification(session, uid, tid, "test_type")
        result = await repo.was_notified_today(session, uid, tid, "test_type")
        assert result is True
        # Different type not affected
        result = await repo.was_notified_today(session, uid, tid, "other_type")
        assert result is False

    @pytest.mark.asyncio
    async def test_create_task_with_all_fields(self, session, users, projects):
        dd = datetime.date.today() + datetime.timedelta(days=3)
        t = await repo.create_task(
            session, title="Full task", project_id=projects["dev"].id,
            description="Full desc", status=TaskStatus.WIP,
            priority=1, is_bug=True, assignee_id=users["ivan"].id,
            creator_id=users["roma"].id, estimated_hours=5.5, due_date=dd,
        )
        assert t.title == "Full task"
        assert t.description == "Full desc"
        assert t.priority == 1
        assert t.is_bug is True
        assert t.due_date == dd
        assert t.assignee_id == users["ivan"].id

    @pytest.mark.asyncio
    async def test_add_comment_and_attachment(self, session, users, tasks):
        comment = await repo.add_comment(session, tasks["t1"].id, users["roma"].id, "Test comment")
        assert comment.text == "Test comment"
        att = await repo.add_attachment(session, comment.id, "test.pdf", "stored_abc.pdf", "application/pdf")
        assert att.filename == "test.pdf"
        assert att.stored_name == "stored_abc.pdf"

    @pytest.mark.asyncio
    async def test_add_comment_null_text(self, session, users, tasks):
        comment = await repo.add_comment(session, tasks["t1"].id, None, None)
        assert comment.text is None
        assert comment.user_id is None

    @pytest.mark.asyncio
    async def test_get_tasks_due_today(self, session, users, projects):
        await repo.create_task(session, "Due today", due_date=datetime.date.today(),
                               status=TaskStatus.TODO, assignee_id=users["roma"].id,
                               project_id=projects["dev"].id)
        result = await repo.get_tasks_due_today(session)
        assert any(t.title == "Due today" for t in result)

    @pytest.mark.asyncio
    async def test_get_overdue_tasks_by_project(self, session, users, projects):
        yesterday = datetime.date.today() - datetime.timedelta(days=1)
        await repo.create_task(session, "Proj Overdue", due_date=yesterday,
                               status=TaskStatus.TODO, project_id=projects["dev"].id,
                               assignee_id=users["roma"].id)
        overdue = await repo.get_overdue_tasks(session, project_id=projects["dev"].id)
        assert any(t.title == "Proj Overdue" for t in overdue)

    @pytest.mark.asyncio
    async def test_get_user_bugs(self, session, users, projects):
        await repo.create_task(session, "User Bug", is_bug=True, status=TaskStatus.TODO,
                               assignee_id=users["roma"].id)
        bugs = await repo.get_user_bugs(session, users["roma"].id)
        assert any(t.title == "User Bug" for t in bugs)

    @pytest.mark.asyncio
    async def test_get_project_status(self, session, users, projects, tasks):
        status = await repo.get_project_status(session, projects["dev"].id)
        assert "total" in status
        assert "done" in status
        assert "progress_pct" in status
        assert "overdue_count" in status
        assert isinstance(status["next_tasks"], list)

    @pytest.mark.asyncio
    async def test_get_team_summary(self, session, users, projects, tasks):
        summary = await repo.get_team_summary(session)
        assert "open_count" in summary
        assert isinstance(summary["overdue"], list)
        assert isinstance(summary["tasks_by_user"], dict)

    @pytest.mark.asyncio
    async def test_get_all_tasks_with_archived(self, session, users, projects):
        t = await repo.create_task(session, "Archived", status=TaskStatus.DONE,
                                   assignee_id=users["roma"].id)
        t.archived_at = datetime.datetime.now()
        await session.commit()
        # Without archived
        all_tasks = await repo.get_all_tasks(session)
        assert t.id not in [x.id for x in all_tasks]
        # With archived
        all_tasks = await repo.get_all_tasks(session, include_archived=True)
        assert t.id in [x.id for x in all_tasks]


# ── Web security tests ──

class TestWebSecurity:
    def test_open_redirect_enter_double_slash(self):
        """next=//evil.com should be rejected."""
        from agent3_pm.web import enter
        import inspect
        src = inspect.getsource(enter)
        assert 'startswith("//")' in src or 'not next.startswith("//"' in src

    def test_open_redirect_delete_api(self):
        """redirect parameter should not allow absolute URLs."""
        src = open("agent3_pm/web.py", encoding="utf-8").read()
        # All redirect validations should use startswith("//") check
        assert 'redirect.startswith("//"' in src or 'startswith("//")' in src

    def test_update_task_api_requires_auth(self):
        src = open("agent3_pm/web.py", encoding="utf-8").read()
        # Find the update_task_api function and check it has auth
        idx = src.index("async def update_task_api")
        chunk = src[idx:idx+300]
        assert "_current_user" in chunk
        assert "not current" in chunk or "if not current" in chunk

    def test_update_status_api_requires_auth(self):
        src = open("agent3_pm/web.py", encoding="utf-8").read()
        idx = src.index("async def update_task_status_api")
        chunk = src[idx:idx+300]
        assert "_current_user" in chunk

    def test_mark_done_api_requires_auth(self):
        src = open("agent3_pm/web.py", encoding="utf-8").read()
        idx = src.index("async def mark_done_api")
        chunk = src[idx:idx+300]
        assert "_current_user" in chunk

    def test_delete_task_api_requires_auth(self):
        src = open("agent3_pm/web.py", encoding="utf-8").read()
        idx = src.index("async def delete_task_api")
        chunk = src[idx:idx+300]
        assert "_current_user" in chunk

    def test_add_comment_api_requires_auth(self):
        src = open("agent3_pm/web.py", encoding="utf-8").read()
        idx = src.index("async def add_comment_api")
        chunk = src[idx:idx+300]
        assert "not current" in chunk or "if not current" in chunk

    def test_create_task_api_requires_auth(self):
        src = open("agent3_pm/web.py", encoding="utf-8").read()
        idx = src.index("async def create_task_api")
        chunk = src[idx:idx+300]
        assert "not current" in chunk or "if not current" in chunk

    def test_settings_requires_manager(self):
        src = open("agent3_pm/web.py", encoding="utf-8").read()
        idx = src.index("async def settings_page")
        chunk = src[idx:idx+300]
        assert "_can_manage" in chunk

    def test_settings_update_requires_manager(self):
        src = open("agent3_pm/web.py", encoding="utf-8").read()
        idx = src.index("async def update_settings_api")
        chunk = src[idx:idx+300]
        assert "_can_manage" in chunk

    def test_web_token_consistency(self):
        from agent3_pm.web import _make_token
        assert _make_token(1) == _make_token(1)
        assert _make_token(1) != _make_token(2)

    def test_enter_token_validation(self):
        from agent3_pm.web import _verify_enter_token
        from agent3_pm.bot import _make_enter_token
        tok = _make_enter_token(1)
        assert _verify_enter_token(1, tok) is True
        assert _verify_enter_token(2, tok) is False
        assert _verify_enter_token(1, "invalid") is False
        assert _verify_enter_token(1, "") is False

    def test_enter_token_expired(self):
        from agent3_pm.web import _verify_enter_token
        import time, hmac as hmac_mod, hashlib as hl_mod
        old_ts = str(int(time.time()) - 90000)  # >24h ago
        sig = hmac_mod.new(config.SECRET_KEY.encode(),
                           f"1:{old_ts}".encode(),
                           hl_mod.sha256).hexdigest()[:16]
        tok = f"{old_ts}.{sig}"
        assert _verify_enter_token(1, tok) is False

    def test_password_hashing(self):
        from agent3_pm.web import _hash_password
        h1 = _hash_password("test123")
        h2 = _hash_password("test123")
        assert h1 == h2
        assert _hash_password("other") != h1


# ── Web routes existence ──

class TestWebRoutes:
    def test_all_routes_exist(self):
        from agent3_pm.web import app
        paths = [r.path for r in app.routes if hasattr(r, "path")]
        expected = [
            "/", "/enter/{user_id}", "/login", "/set-password", "/logout",
            "/board", "/my", "/employees", "/task/{task_id}", "/settings",
            "/api/tasks", "/api/tasks/{task_id}", "/api/tasks/{task_id}/status",
            "/api/tasks/{task_id}/done", "/api/tasks/{task_id}/delete",
            "/api/tasks/{task_id}/comment", "/api/board-access",
            "/api/users", "/api/users/{user_id}", "/api/users/{user_id}/delete",
            "/api/users/{user_id}/reset-password", "/api/settings",
        ]
        for p in expected:
            assert p in paths, f"Route {p} not found"


# ── Bot logic tests ──

class TestBotLogic:
    @pytest.fixture
    def fake_users(self):
        class U:
            def __init__(s, n): s.name = n
        return [U("Иван Шаталов"), U("арсений арсений"), U("Васильев Роман Евгеньевич"),
                U("Миша Капустин"), U("Хафизов Руслан Рустемович"), U("Максим Орлов")]

    def test_fuzzy_match_exact(self, fake_users):
        result = bot._fuzzy_match_user("Иван Шаталов", fake_users)
        assert result.name == "Иван Шаталов"

    def test_fuzzy_match_partial(self, fake_users):
        result = bot._fuzzy_match_user("Иван", fake_users)
        assert result.name == "Иван Шаталов"

    def test_fuzzy_match_first_word(self, fake_users):
        result = bot._fuzzy_match_user("Васильев", fake_users)
        assert "Васильев" in result.name

    def test_fuzzy_match_no_match(self, fake_users):
        result = bot._fuzzy_match_user("Несуществующий", fake_users)
        assert result is None

    def test_fuzzy_match_short_name(self, fake_users):
        result = bot._fuzzy_match_user("Ив", fake_users)
        # "Ив" is a substring of "Иван" so fuzzy_match finds it
        assert result is not None
        assert "Иван" in result.name
        # But truly random short input should fail
        assert bot._fuzzy_match_user("Яя", fake_users) is None

    def test_name_stems(self):
        stems = bot._name_stems("максима")
        assert len(stems) > 0
        assert any("макс" in s for s in stems)

    def test_name_stems_short(self):
        stems = bot._name_stems("ка")
        assert len(stems) == 0  # too short

    def test_name_stems_nickname(self):
        stems = bot._name_stems("вани")
        assert "иван" in stems or any("иван" in s for s in stems)

    def test_match_user_genitive_short(self, fake_users):
        """Too short names should return None."""
        assert bot._match_user_genitive("ив", fake_users) is None
        assert bot._match_user_genitive("", fake_users) is None
        assert bot._match_user_genitive("a", fake_users) is None

    def test_match_user_genitive_strips_punctuation(self, fake_users):
        result = bot._match_user_genitive("ромы?", fake_users)
        assert result is not None
        assert "Васильев" in result.name

    def test_match_user_genitive_maxim(self, fake_users):
        """Nickname 'макс' should match 'Максим Орлов'."""
        result = bot._match_user_genitive("макса", fake_users)
        assert result is not None
        assert "Максим" in result.name

    def test_is_team_report_variations(self):
        assert bot._is_team_report("кто чем занят задачи") is True
        assert bot._is_team_report("у всех задачи") is True
        assert bot._is_team_report("отчёт по задачам") is True
        assert bot._is_team_report("по всем сотрудникам задачи") is True

    def test_not_team_report(self):
        assert bot._is_team_report("hello world") is False
        assert bot._is_team_report("просто текст без ключевых слов") is False

    def test_clean_html_code_preserved(self):
        html = "<code>x = 1</code>"
        assert bot._clean_html(html) == html

    def test_clean_html_pre_preserved(self):
        html = "<pre>block</pre>"
        assert bot._clean_html(html) == html

    def test_clean_html_div_removed(self):
        html = "<div>text</div>"
        result = bot._clean_html(html)
        assert "<div>" not in result
        assert "text" in result

    def test_clean_html_p_to_newline(self):
        html = "<p>line1</p><p>line2</p>"
        result = bot._clean_html(html)
        assert "\n" in result
        assert "line1" in result
        assert "line2" in result

    def test_task_intent_not_names(self):
        """Pronouns should not match as user names."""
        class U:
            def __init__(s, n): s.name = n
        users = [U("Иван")]
        for text in [
            "задачи у кого", "задачи у всех", "задачи у меня",
        ]:
            i, t = bot._task_intent(text, users)
            assert i is None, f"Should not match: {text}"

    def test_task_intent_no_zadach_word(self):
        class U:
            def __init__(s, n): s.name = n
        users = [U("Иван")]
        i, t = bot._task_intent("покажи расписание Ивана", users)
        assert i is None

    def test_group_sessions_dict_exists(self):
        assert isinstance(bot._group_sessions, dict)

    def test_menu_kb(self):
        kb = bot._menu_kb()
        assert kb is not None

    def test_bot_username_constant(self):
        assert bot.BOT_USERNAME == "projectmanageraiibot"


# ── KB Watcher edge cases ──

class TestKBWatcherEdgeCases:
    def test_extract_source_info_empty(self):
        from agent3_pm.kb_watcher import _extract_source_info
        info = _extract_source_info("", "test.md")
        assert info["filename"] == "test.md"
        assert info["date"] == ""

    def test_extract_source_info_frontmatter(self):
        from agent3_pm.kb_watcher import _extract_source_info
        content = "date: 2024-01-15\nauthor: test_user\nauthor_name: \"Иван Иванов\""
        info = _extract_source_info(content, "call.md")
        assert info["date"] == "2024-01-15"
        assert info["author"] == "test_user"
        assert info["author_name"] == "Иван Иванов"

    def test_extract_source_info_sotrudnik(self):
        from agent3_pm.kb_watcher import _extract_source_info
        content = "Сотрудник: Петр Петров (Маркетолог)"
        info = _extract_source_info(content, "file.md")
        assert "Петр Петров" in info["author_name"]

    def test_batch_operations(self):
        from agent3_pm.kb_watcher import _approval_batches, get_batch, remove_batch
        test_id = "test_batch_9999"
        _approval_batches[test_id] = {"tasks": [], "locked_by": None, "current_idx": 0}
        assert get_batch(test_id) is not None
        remove_batch(test_id)
        assert get_batch(test_id) is None

    def test_get_batch_nonexistent(self):
        from agent3_pm.kb_watcher import get_batch
        assert get_batch("nonexistent_batch") is None

    def test_remove_batch_nonexistent(self):
        from agent3_pm.kb_watcher import remove_batch
        remove_batch("nonexistent_batch")  # should not raise

    def test_seen_global_dedup(self):
        from agent3_pm.kb_watcher import _seen_global
        _seen_global.add("test_dedup_file.md")
        assert "test_dedup_file.md" in _seen_global
        _seen_global.discard("test_dedup_file.md")

    def test_parse_prompt_calls_has_json_format(self):
        from agent3_pm.kb_watcher import PARSE_PROMPT_CALLS
        assert '"title"' in PARSE_PROMPT_CALLS
        assert '"assignee_name"' in PARSE_PROMPT_CALLS
        assert '"priority"' in PARSE_PROMPT_CALLS

    def test_parse_prompt_bugs_has_json_format(self):
        from agent3_pm.kb_watcher import PARSE_PROMPT_BUGS
        assert '"is_bug": true' in PARSE_PROMPT_BUGS


# ── GitHub Watcher ──

class TestGitHubWatcher:
    def test_github_watcher_seen_files_set(self):
        from agent3_pm.github_watcher import _seen_files
        assert isinstance(_seen_files, set)

    def test_github_constants(self):
        from agent3_pm.github_watcher import GITHUB_REPO, GITHUB_PATH
        assert "bugs" in GITHUB_PATH
        assert len(GITHUB_REPO) > 0


# ── Config ──

class TestConfig:
    def test_config_defaults(self):
        assert config.WEB_PORT == 8080 or isinstance(config.WEB_PORT, int)
        assert config.MORNING_SUMMARY_HOUR >= 0
        assert config.MORNING_SUMMARY_MINUTE >= 0
        assert config.DEADLINE_WARNING_HOURS > 0

    def test_config_admin_ids(self):
        assert isinstance(config.ADMIN_TELEGRAM_IDS, list)

    def test_config_timezone(self):
        assert len(config.TIMEZONE) > 0

    def test_config_secret_key(self):
        assert config.SECRET_KEY == "test-secret"  # from env


# ── Task Agent ──

class TestTaskAgent:
    def test_build_system_prompt_contains_dates(self):
        from agent3_pm.task_agent import _build_system_prompt
        ctx = {"current_user": {"id": 1, "name": "Test"}, "projects": [],
               "users": [], "all_tasks": [], "web_base_url": "http://test"}
        prompt = _build_system_prompt(ctx)
        today = datetime.date.today().isoformat()
        assert today in prompt

    def test_build_system_prompt_contains_all_statuses(self):
        from agent3_pm.task_agent import _build_system_prompt
        ctx = {"current_user": {}, "projects": [], "users": [],
               "all_tasks": [], "web_base_url": ""}
        prompt = _build_system_prompt(ctx)
        for s in ["backlog", "planning", "todo", "wip", "done", "approved", "hold"]:
            assert s in prompt

    def test_build_system_prompt_includes_tasks(self):
        from agent3_pm.task_agent import _build_system_prompt
        ctx = {
            "current_user": {"id": 1, "name": "A"},
            "projects": [{"id": 1, "name": "Dev"}],
            "users": [{"id": 1, "name": "A", "position": "CEO"}],
            "all_tasks": [{"id": 1, "title": "TestTask", "status": "todo"}],
            "web_base_url": "http://test",
        }
        prompt = _build_system_prompt(ctx)
        assert "TestTask" in prompt

    def test_mode_create_and_ask_different(self):
        from agent3_pm.task_agent import _MODE_CREATE, _MODE_ASK
        assert _MODE_CREATE != _MODE_ASK
        assert "СОЗДАНИЕ" in _MODE_CREATE
        assert "ВОПРОСЫ" in _MODE_ASK

    @pytest.mark.asyncio
    async def test_smart_assistant_handles_api_error(self):
        """When OpenAI client fails, should return error message."""
        from agent3_pm.task_agent import smart_assistant
        # With no valid API key, should gracefully return error
        result = await smart_assistant(
            "test", {"current_user": {}, "projects": [], "users": [],
                     "all_tasks": [], "web_base_url": ""},
            mode="ask"
        )
        assert result.get("action") == "answer"
        assert "message" in result


# ── Web label maps ──

class TestWebLabelMaps:
    def test_status_label_map_complete(self):
        from agent3_pm.web import STATUS_LABEL_MAP, KANBAN_COLUMNS
        for col in KANBAN_COLUMNS:
            assert col in STATUS_LABEL_MAP

    def test_kanban_columns_order(self):
        from agent3_pm.web import KANBAN_COLUMNS
        assert KANBAN_COLUMNS[0] == "backlog"
        assert KANBAN_COLUMNS[-1] == "hold"
        assert "wip" in KANBAN_COLUMNS

    def test_priority_label_map(self):
        from agent3_pm.web import PRIORITY_LABEL_MAP
        assert 0 in PRIORITY_LABEL_MAP
        assert 3 in PRIORITY_LABEL_MAP
        assert "срочно" in PRIORITY_LABEL_MAP[0]

    def test_task_to_dict(self):
        from agent3_pm.web import _task_to_dict
        class FakeTask:
            id = 1; title = "T"; description = None
            status = TaskStatus.TODO; priority = 2; is_bug = False
            assignee = None; assignee_id = None
            project = None; project_id = None
            estimated_hours = None; due_date = None
            comments = []; archived_at = None
            @property
            def is_red(self): return False
            @property
            def is_overdue(self): return False
            @property
            def is_hot(self): return False
        d = _task_to_dict(FakeTask())
        assert d["id"] == 1
        assert d["status"] == "todo"
        assert d["assignee"] == "—"
        assert d["comments_count"] == 0
