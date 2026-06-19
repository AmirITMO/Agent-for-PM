import re
from dataclasses import dataclass
from enum import Enum


class QueryIntent(Enum):
    MY_TASKS_TODAY = "my_tasks_today"
    OVERDUE_TEAM = "overdue_team"
    PROJECT_STATUS = "project_status"
    TASK_DETAIL = "task_detail"
    SEARCH_TASK = "search_task"
    HELP = "help"
    UNKNOWN = "unknown"


@dataclass
class ParsedQuery:
    intent: QueryIntent
    project_name: str | None = None
    task_id: int | None = None
    search_query: str | None = None


_PATTERNS: list[tuple[QueryIntent, re.Pattern, list[str]]] = [
    (
        QueryIntent.MY_TASKS_TODAY,
        re.compile(
            r"(что\s+(у\s+меня\s+)?(на\s+сегодня|сегодня)|"
            r"мои\s+задачи|"
            r"мой\s+план|"
            r"чем\s+заняться|"
            r"что\s+делать)",
            re.IGNORECASE,
        ),
        [],
    ),
    (
        QueryIntent.OVERDUE_TEAM,
        re.compile(
            r"(что\s+просрочено|просрочк[иа]|просроченн|"
            r"overdue|"
            r"дедлайн.*(пропущен|прошел|прошёл)|"
            r"опаздыва)",
            re.IGNORECASE,
        ),
        [],
    ),
    (
        QueryIntent.PROJECT_STATUS,
        re.compile(
            r"(статус\s+(проекта\s+)?(?P<project>.+)|"
            r"как\s+дела\s+(с\s+|по\s+|в\s+)(?P<project2>.+)|"
            r"прогресс\s+(по\s+|проекта\s+)?(?P<project3>.+))",
            re.IGNORECASE,
        ),
        ["project", "project2", "project3"],
    ),
    (
        QueryIntent.TASK_DETAIL,
        re.compile(
            r"(задача\s*#?\s*(?P<task_id>\d+)|"
            r"#(?P<task_id2>\d+)|"
            r"детали\s+задачи\s*#?\s*(?P<task_id3>\d+)|"
            r"покажи\s+задачу\s*#?\s*(?P<task_id4>\d+))",
            re.IGNORECASE,
        ),
        [],
    ),
]


def parse_query(text: str) -> ParsedQuery:
    text = text.strip()

    if text.startswith("/"):
        return ParsedQuery(intent=QueryIntent.HELP)

    for intent, pattern, project_groups in _PATTERNS:
        match = pattern.search(text)
        if not match:
            continue

        if intent == QueryIntent.TASK_DETAIL:
            task_id = None
            for g in ["task_id", "task_id2", "task_id3", "task_id4"]:
                try:
                    val = match.group(g)
                    if val:
                        task_id = int(val)
                        break
                except IndexError:
                    continue
            return ParsedQuery(intent=intent, task_id=task_id)

        if intent == QueryIntent.PROJECT_STATUS:
            project_name = None
            for g in project_groups:
                try:
                    val = match.group(g)
                    if val:
                        project_name = val.strip().rstrip("?.,!")
                        break
                except IndexError:
                    continue
            return ParsedQuery(intent=intent, project_name=project_name)

        return ParsedQuery(intent=intent)

    task_id_match = re.search(r"#(\d+)", text)
    if task_id_match:
        return ParsedQuery(intent=QueryIntent.TASK_DETAIL, task_id=int(task_id_match.group(1)))

    if len(text) > 2:
        return ParsedQuery(intent=QueryIntent.SEARCH_TASK, search_query=text)

    return ParsedQuery(intent=QueryIntent.UNKNOWN)
