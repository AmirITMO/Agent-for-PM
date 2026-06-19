"""Tests for natural language query parser."""
import pytest
from agent3_pm.query_parser import parse_query, QueryIntent


class TestMyTasksToday:
    @pytest.mark.parametrize("text", [
        "что у меня на сегодня", "мои задачи", "мой план", "что делать",
    ])
    def test_recognized(self, text):
        assert parse_query(text).intent == QueryIntent.MY_TASKS_TODAY


class TestOverdue:
    @pytest.mark.parametrize("text", [
        "что просрочено", "просрочки", "overdue",
    ])
    def test_recognized(self, text):
        assert parse_query(text).intent == QueryIntent.OVERDUE_TEAM


class TestProjectStatus:
    def test_recognized(self):
        r = parse_query("статус проекта Dev")
        assert r.intent == QueryIntent.PROJECT_STATUS
        assert r.project_name == "Dev"


class TestTaskDetail:
    def test_by_hash(self):
        r = parse_query("#42")
        assert r.intent == QueryIntent.TASK_DETAIL and r.task_id == 42

    def test_by_word(self):
        r = parse_query("задача 5")
        assert r.intent == QueryIntent.TASK_DETAIL and r.task_id == 5


class TestSearch:
    def test_unknown_text(self):
        assert parse_query("баг с налогами").intent == QueryIntent.SEARCH_TASK

    def test_short_unknown(self):
        assert parse_query("ab").intent == QueryIntent.UNKNOWN
