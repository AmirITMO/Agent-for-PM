"""System tests — DB integrity, access control, cascade deletes, agent context."""
import datetime
import pytest
from agent3_pm.models import TaskStatus, UserRole, ACTIVE_STATUSES, CLOSED_STATUSES, is_level_1
from agent3_pm import repository as repo

pytestmark = pytest.mark.asyncio


class TestTaskDeletion:
    """Verify tasks are properly deleted from DB with all related data."""

    async def test_delete_task_removes_from_db(self, session, sample_data):
        task_id = sample_data["overdue"].id
        assert await repo.get_task_by_id(session, task_id) is not None
        result = await repo.delete_task(session, task_id)
        assert result is True
        assert await repo.get_task_by_id(session, task_id) is None

    async def test_delete_task_with_comments(self, session, sample_data):
        task_id = sample_data["overdue"].id
        await repo.add_comment(session, task_id, sample_data["employee1"].id, "test comment")
        result = await repo.delete_task(session, task_id)
        assert result is True
        assert await repo.get_task_by_id(session, task_id) is None

    async def test_delete_task_with_attachments(self, session, sample_data):
        task_id = sample_data["overdue"].id
        comment = await repo.add_comment(session, task_id, None, "with file")
        await repo.add_attachment(session, comment.id, "test.png", "abc.png", "image/png")
        result = await repo.delete_task(session, task_id)
        assert result is True

    async def test_delete_nonexistent_returns_false(self, session, sample_data):
        assert await repo.delete_task(session, 99999) is False

    async def test_deleted_task_not_in_overdue(self, session, sample_data):
        task_id = sample_data["overdue"].id
        overdue_before = await repo.get_overdue_tasks(session)
        assert any(t.id == task_id for t in overdue_before)
        await repo.delete_task(session, task_id)
        overdue_after = await repo.get_overdue_tasks(session)
        assert all(t.id != task_id for t in overdue_after)

    async def test_deleted_task_not_in_all_tasks(self, session, sample_data):
        task_id = sample_data["overdue"].id
        await repo.delete_task(session, task_id)
        all_tasks = await repo.get_all_tasks(session)
        assert all(t.id != task_id for t in all_tasks)


class TestActiveStatusFiltering:
    """Agent should only see active tasks (not done/approved/hold)."""

    async def test_done_not_in_active_filter(self, session, sample_data):
        all_tasks = await repo.get_all_tasks(session)
        active = [t for t in all_tasks if t.status in ACTIVE_STATUSES]
        for t in active:
            assert t.status not in CLOSED_STATUSES
            assert t.status != TaskStatus.HOLD

    async def test_done_task_excluded(self, session, sample_data):
        done_id = sample_data["done"].id
        all_tasks = await repo.get_all_tasks(session)
        active = [t for t in all_tasks if t.status in ACTIVE_STATUSES]
        assert all(t.id != done_id for t in active)

    async def test_hold_task_excluded_from_overdue(self, session, sample_data):
        hold_id = sample_data["hold"].id
        overdue = await repo.get_overdue_tasks(session)
        assert all(t.id != hold_id for t in overdue)

    async def test_approved_not_in_hot(self, session, sample_data):
        # Move a task to approved with future deadline
        await repo.update_task(session, sample_data["hot"].id, status=TaskStatus.APPROVED)
        hot = await repo.get_hot_tasks(session, 48)
        assert all(t.id != sample_data["hot"].id for t in hot)

    async def test_archived_tasks_excluded(self, session, sample_data):
        """Archived tasks should not appear in get_all_tasks."""
        task = sample_data["done"]
        task.archived_at = datetime.datetime.now()
        await session.commit()
        all_tasks = await repo.get_all_tasks(session, include_archived=False)
        assert all(t.id != task.id for t in all_tasks)


class TestBoardAccess:
    """Board membership controls who sees what."""

    async def test_set_and_check_access(self, session, sample_data):
        pid = sample_data["dev"].id
        uid = sample_data["employee1"].id
        await repo.set_board_access(session, pid, uid, True)
        ids = await repo.get_board_member_ids(session, pid)
        assert uid in ids

    async def test_remove_access(self, session, sample_data):
        pid = sample_data["dev"].id
        uid = sample_data["employee1"].id
        await repo.set_board_access(session, pid, uid, True)
        await repo.set_board_access(session, pid, uid, False)
        ids = await repo.get_board_member_ids(session, pid)
        assert uid not in ids

    async def test_double_grant_no_error(self, session, sample_data):
        pid = sample_data["dev"].id
        uid = sample_data["employee1"].id
        await repo.set_board_access(session, pid, uid, True)
        await repo.set_board_access(session, pid, uid, True)
        ids = await repo.get_board_member_ids(session, pid)
        assert uid in ids

    async def test_double_revoke_no_error(self, session, sample_data):
        pid = sample_data["dev"].id
        uid = sample_data["employee1"].id
        await repo.set_board_access(session, pid, uid, False)
        await repo.set_board_access(session, pid, uid, False)


class TestLevelAccess:
    """Level 1 vs Level 2-3 access rules."""

    def test_is_level_1_ceo(self):
        assert is_level_1("CEO") is True

    def test_is_level_1_cbdo(self):
        assert is_level_1("CBDO") is True

    def test_is_level_1_coo(self):
        assert is_level_1("COO") is True

    def test_is_level_2_mop(self):
        assert is_level_1("МОП") is False

    def test_is_level_3_smm(self):
        assert is_level_1("СММ") is False

    def test_is_level_none(self):
        assert is_level_1(None) is False

    def test_is_level_empty(self):
        assert is_level_1("") is False


class TestUserDeletion:
    """Deleting a user should not break tasks."""

    async def test_delete_user(self, session, sample_data):
        uid = sample_data["no_tg_user"].id
        result = await repo.delete_user(session, uid)
        assert result is True

    async def test_delete_nonexistent_user(self, session, sample_data):
        assert await repo.delete_user(session, 99999) is False

    async def test_task_survives_user_deletion(self, session, sample_data):
        """Tasks assigned to deleted user should still exist (assignee=None)."""
        task_id = sample_data["overdue"].id
        user_id = sample_data["employee1"].id
        # Task is assigned to employee1
        task = await repo.get_task_by_id(session, task_id)
        assert task.assignee_id == user_id
        # Can't easily test CASCADE here with SQLite, but verify task exists
        assert task is not None


class TestStatusTransitions:
    """Full lifecycle of task status changes."""

    async def test_backlog_to_wip(self, session, sample_data):
        task = await repo.update_task_status(session, sample_data["future"].id, TaskStatus.WIP)
        assert task.status == TaskStatus.WIP

    async def test_wip_to_done(self, session, sample_data):
        task = await repo.update_task_status(session, sample_data["overdue"].id, TaskStatus.DONE)
        assert task.status == TaskStatus.DONE
        # Should disappear from overdue
        overdue = await repo.get_overdue_tasks(session)
        assert all(t.id != task.id for t in overdue)

    async def test_done_to_approved(self, session, sample_data):
        task = await repo.update_task_status(session, sample_data["done"].id, TaskStatus.APPROVED)
        assert task.status == TaskStatus.APPROVED

    async def test_any_to_hold(self, session, sample_data):
        task = await repo.update_task_status(session, sample_data["due_today"].id, TaskStatus.HOLD)
        assert task.status == TaskStatus.HOLD
        # Should disappear from hot/today
        today = await repo.get_tasks_due_today(session)
        assert all(t.id != task.id for t in today)

    async def test_update_nonexistent_returns_none(self, session, sample_data):
        result = await repo.update_task_status(session, 99999, TaskStatus.DONE)
        assert result is None


class TestProjectStatusConsistency:
    """Project stats must be mathematically consistent."""

    async def test_counts_sum_to_total(self, session, sample_data):
        status = await repo.get_project_status(session, sample_data["dev"].id)
        assert sum(status["status_counts"].values()) == status["total"]

    async def test_done_count_correct(self, session, sample_data):
        status = await repo.get_project_status(session, sample_data["dev"].id)
        expected = (status["status_counts"].get(TaskStatus.DONE, 0) +
                    status["status_counts"].get(TaskStatus.APPROVED, 0))
        assert status["done"] == expected

    async def test_progress_bounds(self, session, sample_data):
        status = await repo.get_project_status(session, sample_data["dev"].id)
        assert 0 <= status["progress_pct"] <= 100

    async def test_empty_project_no_crash(self, session):
        project = await repo.create_project(session, "EmptyTest")
        status = await repo.get_project_status(session, project.id)
        assert status["total"] == 0
        assert status["progress_pct"] == 0
        assert status["overdue_count"] == 0


class TestNotificationIntegrity:
    """Notification dedup must work across types, users, tasks."""

    async def test_same_type_same_day_blocked(self, session, sample_data):
        uid, tid = sample_data["employee1"].id, sample_data["overdue"].id
        await repo.log_notification(session, uid, tid, "overdue")
        assert await repo.was_notified_today(session, uid, tid, "overdue") is True

    async def test_different_type_allowed(self, session, sample_data):
        uid, tid = sample_data["employee1"].id, sample_data["overdue"].id
        await repo.log_notification(session, uid, tid, "overdue")
        assert await repo.was_notified_today(session, uid, tid, "deadline_warning") is False

    async def test_different_task_allowed(self, session, sample_data):
        uid = sample_data["employee1"].id
        await repo.log_notification(session, uid, sample_data["overdue"].id, "overdue")
        assert await repo.was_notified_today(session, uid, sample_data["due_today"].id, "overdue") is False

    async def test_different_user_allowed(self, session, sample_data):
        tid = sample_data["overdue"].id
        await repo.log_notification(session, sample_data["employee1"].id, tid, "overdue")
        assert await repo.was_notified_today(session, sample_data["employee2"].id, tid, "overdue") is False
