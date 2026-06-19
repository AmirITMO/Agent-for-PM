"""Tests for model properties and enums."""
import datetime
import pytest
from agent3_pm.models import Task, TaskStatus, ACTIVE_STATUSES, CLOSED_STATUSES, DEFAULT_PRIORITY


class TestTaskProperties:
    def _make_task(self, status=TaskStatus.TODO, due_date=None, priority=DEFAULT_PRIORITY, is_bug=False):
        t = Task(title="test", status=status, due_date=due_date, priority=priority, is_bug=is_bug)
        return t

    def test_is_overdue_past_date_active(self):
        t = self._make_task(due_date=datetime.date.today() - datetime.timedelta(days=1))
        assert t.is_overdue is True

    def test_is_overdue_today_not_overdue(self):
        t = self._make_task(due_date=datetime.date.today())
        assert t.is_overdue is False

    def test_is_overdue_no_date(self):
        assert self._make_task(due_date=None).is_overdue is False

    def test_is_overdue_done_not_overdue(self):
        t = self._make_task(status=TaskStatus.DONE,
                            due_date=datetime.date.today() - datetime.timedelta(days=10))
        assert t.is_overdue is False

    def test_is_overdue_approved_not_overdue(self):
        t = self._make_task(status=TaskStatus.APPROVED,
                            due_date=datetime.date.today() - datetime.timedelta(days=5))
        assert t.is_overdue is False

    def test_is_due_today(self):
        assert self._make_task(due_date=datetime.date.today()).is_due_today is True

    def test_is_due_today_done_not_shown(self):
        t = self._make_task(status=TaskStatus.DONE, due_date=datetime.date.today())
        assert t.is_due_today is False

    def test_is_hot_tomorrow(self):
        t = self._make_task(due_date=datetime.date.today() + datetime.timedelta(days=1))
        assert t.is_hot is True

    def test_is_hot_past_not_hot(self):
        t = self._make_task(due_date=datetime.date.today() - datetime.timedelta(days=1))
        assert t.is_hot is False

    def test_is_red_p0(self):
        assert self._make_task(priority=0).is_red is True

    def test_is_red_bug(self):
        assert self._make_task(is_bug=True).is_red is True

    def test_is_red_normal(self):
        assert self._make_task(priority=2).is_red is False


class TestEnumSets:
    def test_active_and_closed_disjoint(self):
        assert ACTIVE_STATUSES & CLOSED_STATUSES == set()

    def test_all_statuses_covered(self):
        all_statuses = set(TaskStatus)
        covered = ACTIVE_STATUSES | CLOSED_STATUSES | {TaskStatus.HOLD}
        assert all_statuses == covered

    def test_hold_not_active_not_closed(self):
        assert TaskStatus.HOLD not in ACTIVE_STATUSES
        assert TaskStatus.HOLD not in CLOSED_STATUSES
