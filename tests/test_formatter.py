"""Tests for message formatting."""
import datetime
import pytest
from unittest.mock import MagicMock
from agent3_pm.models import Task, TaskStatus
from agent3_pm.formatter import (
    format_task_short, format_today_tasks, format_overdue_list,
    format_overdue_block, format_deadline_warning,
)


def _make_task(id=1, title="Test", status=TaskStatus.TODO, priority=2, is_bug=False,
               due_date=None, estimated_hours=None, assignee_name=None):
    t = MagicMock(spec=Task)
    t.id = id
    t.title = title
    t.status = status
    t.priority = priority
    t.is_bug = is_bug
    t.due_date = due_date
    t.estimated_hours = estimated_hours
    t.assignee = MagicMock(name=assignee_name) if assignee_name else None
    if assignee_name:
        t.assignee.name = assignee_name

    today = datetime.date.today()
    if due_date and status not in (TaskStatus.DONE, TaskStatus.APPROVED):
        t.is_overdue = due_date < today
        t.is_due_today = due_date == today
        t.is_hot = 0 <= (due_date - today).days <= 1
        t.is_red = priority == 0 or is_bug
    else:
        t.is_overdue = False
        t.is_due_today = False
        t.is_hot = False
        t.is_red = priority == 0 or is_bug
    return t


class TestFormatTaskShort:
    def test_basic(self):
        assert "Test" in format_task_short(_make_task())

    def test_overdue(self):
        t = _make_task(due_date=datetime.date.today() - datetime.timedelta(days=1))
        assert "просрочено" in format_task_short(t)

    def test_bug_shown(self):
        assert "[Баг]" in format_task_short(_make_task(is_bug=True))

    def test_p0_shown(self):
        assert "[P0]" in format_task_short(_make_task(priority=0))

    def test_normal_no_tag(self):
        r = format_task_short(_make_task(priority=2))
        assert "[" not in r


class TestFormatTodayTasks:
    def test_empty(self):
        assert "нет" in format_today_tasks([]).lower()

    def test_overdue_not_duplicated(self):
        t = _make_task(status=TaskStatus.WIP,
                       due_date=datetime.date.today() - datetime.timedelta(days=1))
        result = format_today_tasks([t])
        assert result.count("Test") == 1


class TestFormatOverdueBlock:
    def test_all_empty(self):
        assert "нет" in format_overdue_block([], [], []).lower()

    def test_with_data(self):
        t = _make_task(due_date=datetime.date.today() - datetime.timedelta(days=1))
        result = format_overdue_block([t], [], [])
        assert "Просрочено" in result


class TestFormatDeadlineWarning:
    def test_overdue(self):
        t = _make_task(due_date=datetime.date.today() - datetime.timedelta(days=3))
        assert "3 дн" in format_deadline_warning(t)

    def test_today(self):
        t = _make_task(due_date=datetime.date.today())
        assert "СЕГОДНЯ" in format_deadline_warning(t)
