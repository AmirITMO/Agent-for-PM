"""Integration tests — cross-component consistency."""
import datetime
import pytest
from agent3_pm.models import TaskStatus, ACTIVE_STATUSES, CLOSED_STATUSES
from agent3_pm import repository as repo
from agent3_pm.formatter import format_today_tasks, format_overdue_list

pytestmark = pytest.mark.asyncio


class TestDataConsistency:
    async def test_overdue_all_past_dates(self, session, sample_data):
        for t in await repo.get_overdue_tasks(session):
            assert t.due_date < datetime.date.today()
            assert t.status in ACTIVE_STATUSES

    async def test_hot_all_future_or_today(self, session, sample_data):
        for t in await repo.get_hot_tasks(session, 48):
            assert t.due_date >= datetime.date.today()

    async def test_overdue_and_hot_disjoint(self, session, sample_data):
        overdue_ids = {t.id for t in await repo.get_overdue_tasks(session)}
        hot_ids = {t.id for t in await repo.get_hot_tasks(session, 48)}
        assert overdue_ids & hot_ids == set()

    async def test_done_never_in_active(self, session, sample_data):
        for t in (await repo.get_overdue_tasks(session) +
                  await repo.get_hot_tasks(session, 48) +
                  await repo.get_tasks_due_today(session)):
            assert t.status not in CLOSED_STATUSES

    async def test_project_status_consistent(self, session, sample_data):
        status = await repo.get_project_status(session, sample_data["dev"].id)
        assert sum(status["status_counts"].values()) == status["total"]

    async def test_summary_overdue_matches(self, session, sample_data):
        summary = await repo.get_team_summary(session)
        direct = await repo.get_overdue_tasks(session)
        assert len(summary["overdue"]) == len(direct)


class TestStatusTransitions:
    async def test_done_removes_from_overdue(self, session, sample_data):
        assert any(t.id == sample_data["overdue"].id for t in await repo.get_overdue_tasks(session))
        await repo.update_task_status(session, sample_data["overdue"].id, TaskStatus.DONE)
        assert all(t.id != sample_data["overdue"].id for t in await repo.get_overdue_tasks(session))

    async def test_hold_removes_from_active(self, session, sample_data):
        await repo.update_task_status(session, sample_data["due_today"].id, TaskStatus.HOLD)
        assert all(t.id != sample_data["due_today"].id for t in await repo.get_tasks_due_today(session))
