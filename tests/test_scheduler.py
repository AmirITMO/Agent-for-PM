"""Tests for scheduler logic."""
import pytest
from agent3_pm.models import TaskStatus
from agent3_pm import repository as repo

pytestmark = pytest.mark.asyncio


class TestDeadlineDedup:
    async def test_first_goes_through(self, session, sample_data):
        assert await repo.was_notified_today(
            session, sample_data["employee1"].id, sample_data["overdue"].id, "overdue") is False

    async def test_second_blocked(self, session, sample_data):
        await repo.log_notification(session, sample_data["employee1"].id,
                                    sample_data["overdue"].id, "overdue")
        assert await repo.was_notified_today(
            session, sample_data["employee1"].id, sample_data["overdue"].id, "overdue") is True


class TestSchedulerEdgeCases:
    async def test_no_managers_no_crash(self, session):
        managers = await repo.get_managers(session)
        assert len(managers) == 0

    async def test_unassigned_no_crash(self, session, sample_data):
        task = await repo.get_task_by_id(session, sample_data["unassigned"].id)
        assert task.assignee is None

    async def test_hold_not_in_overdue(self, session, sample_data):
        for t in await repo.get_overdue_tasks(session):
            assert t.status != TaskStatus.HOLD
