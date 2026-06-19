"""Tests for repository queries."""
import datetime
import pytest
from agent3_pm.models import TaskStatus, UserRole, DEFAULT_PRIORITY
from agent3_pm import repository as repo

pytestmark = pytest.mark.asyncio


class TestUserQueries:
    async def test_get_user_by_telegram_id(self, session, sample_data):
        user = await repo.get_user_by_telegram_id(session, 222222)
        assert user is not None and user.name == "Employee One"

    async def test_get_user_not_found(self, session, sample_data):
        assert await repo.get_user_by_telegram_id(session, 999999) is None

    async def test_get_all_users(self, session, sample_data):
        assert len(await repo.get_all_users(session)) == 4

    async def test_get_managers(self, session, sample_data):
        managers = await repo.get_managers(session)
        assert len(managers) == 1
        assert managers[0].position == "CEO"


class TestProjectQueries:
    async def test_get_project_by_name_case_insensitive(self, session, sample_data):
        p = await repo.get_project_by_name(session, "dev")
        assert p is not None and p.name == "Dev"

    async def test_get_project_not_found(self, session, sample_data):
        assert await repo.get_project_by_name(session, "Nonexistent") is None


class TestTaskQueries:
    async def test_get_task_by_id(self, session, sample_data):
        task = await repo.get_task_by_id(session, sample_data["overdue"].id)
        assert task is not None and task.title == "Overdue bug fix"

    async def test_search_tasks(self, session, sample_data):
        results = await repo.search_tasks_by_title(session, "bug")
        assert len(results) >= 1

    async def test_get_tasks_for_user_today(self, session, sample_data):
        tasks = await repo.get_tasks_for_user_today(session, sample_data["employee1"].id)
        ids = {t.id for t in tasks}
        assert sample_data["overdue"].id in ids
        assert sample_data["due_today"].id in ids
        assert sample_data["done"].id not in ids

    async def test_get_overdue_tasks(self, session, sample_data):
        overdue = await repo.get_overdue_tasks(session)
        ids = {t.id for t in overdue}
        assert sample_data["overdue"].id in ids
        assert sample_data["done"].id not in ids
        assert sample_data["hold"].id not in ids

    async def test_get_hot_tasks(self, session, sample_data):
        hot = await repo.get_hot_tasks(session, 48)
        ids = {t.id for t in hot}
        assert sample_data["due_today"].id in ids
        assert sample_data["hot"].id in ids
        assert sample_data["overdue"].id not in ids

    async def test_get_project_status(self, session, sample_data):
        status = await repo.get_project_status(session, sample_data["dev"].id)
        assert status["total"] > 0
        assert 0 <= status["progress_pct"] <= 100

    async def test_get_project_status_empty(self, session):
        project = await repo.create_project(session, "EmptyProject")
        status = await repo.get_project_status(session, project.id)
        assert status["total"] == 0 and status["progress_pct"] == 0

    async def test_get_user_bugs(self, session, sample_data):
        bugs = await repo.get_user_bugs(session, sample_data["employee1"].id)
        assert any(t.is_bug for t in bugs)


class TestTaskMutations:
    async def test_create_task(self, session, sample_data):
        task = await repo.create_task(session, "New task", sample_data["dev"].id,
                                      status=TaskStatus.TODO, priority=1)
        assert task.id is not None and task.priority == 1

    async def test_update_task_status(self, session, sample_data):
        task = await repo.update_task_status(session, sample_data["overdue"].id, TaskStatus.DONE)
        assert task.status == TaskStatus.DONE

    async def test_update_task(self, session, sample_data):
        task = await repo.update_task(session, sample_data["overdue"].id,
                                      title="Updated", is_bug=False)
        assert task.title == "Updated" and task.is_bug is False


class TestBoardMembership:
    async def test_set_board_access(self, session, sample_data):
        await repo.set_board_access(session, sample_data["dev"].id,
                                    sample_data["employee1"].id, True)
        ids = await repo.get_board_member_ids(session, sample_data["dev"].id)
        assert sample_data["employee1"].id in ids

    async def test_remove_board_access(self, session, sample_data):
        await repo.set_board_access(session, sample_data["dev"].id,
                                    sample_data["employee1"].id, True)
        await repo.set_board_access(session, sample_data["dev"].id,
                                    sample_data["employee1"].id, False)
        ids = await repo.get_board_member_ids(session, sample_data["dev"].id)
        assert sample_data["employee1"].id not in ids


class TestComments:
    async def test_add_comment(self, session, sample_data):
        comment = await repo.add_comment(session, sample_data["overdue"].id,
                                         sample_data["employee1"].id, "Test comment")
        assert comment.id is not None and comment.text == "Test comment"

    async def test_add_attachment(self, session, sample_data):
        comment = await repo.add_comment(session, sample_data["overdue"].id, None, "With file")
        att = await repo.add_attachment(session, comment.id, "test.png",
                                        "abc123.png", "image/png")
        assert att.is_image is True


class TestNotifications:
    async def test_notification_dedup(self, session, sample_data):
        assert await repo.was_notified_today(
            session, sample_data["employee1"].id, sample_data["overdue"].id, "overdue") is False
        await repo.log_notification(session, sample_data["employee1"].id,
                                    sample_data["overdue"].id, "overdue")
        assert await repo.was_notified_today(
            session, sample_data["employee1"].id, sample_data["overdue"].id, "overdue") is True

    async def test_different_type_not_blocked(self, session, sample_data):
        await repo.log_notification(session, sample_data["employee1"].id,
                                    sample_data["overdue"].id, "deadline_warning")
        assert await repo.was_notified_today(
            session, sample_data["employee1"].id, sample_data["overdue"].id, "overdue") is False
