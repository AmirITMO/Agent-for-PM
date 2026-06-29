import enum
import datetime
from sqlalchemy import (
    Column, Integer, BigInteger, String, Text, Date, DateTime, Boolean,
    Numeric, ForeignKey, Enum as SAEnum, func, Index, Table,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class UserRole(str, enum.Enum):
    ADMIN = "admin"
    EMPLOYEE = "employee"


class TaskStatus(str, enum.Enum):
    BACKLOG = "backlog"
    PLANNING = "planning"
    TODO = "todo"
    WIP = "wip"
    DONE = "done"
    APPROVED = "approved"
    HOLD = "hold"


ACTIVE_STATUSES = {TaskStatus.BACKLOG, TaskStatus.PLANNING, TaskStatus.TODO, TaskStatus.WIP}
CLOSED_STATUSES = {TaskStatus.DONE, TaskStatus.APPROVED}

# Priority levels: 0 (highest, red) .. 3 (lowest). Bug is a separate flag (red).
PRIORITY_LEVELS = [0, 1, 2, 3]
DEFAULT_PRIORITY = 2


# Уровень 1 — руководители, имеют доступ к вкладке «Сотрудники»
LEVEL_1_POSITIONS = ["CEO", "CBDO", "CMO", "CTO", "CRO", "COO"]
LEVEL_2_POSITIONS = ["РОП", "МОП", "Программист", "Продуктолог", "Маркетолог"]
LEVEL_3_POSITIONS = [
    "Директолог", "ВК", "Яндекс", "ТГ", "Авитолог", "СММ",
    "Амбассадор", "Партнер", "Диагност", "Отдел заботы", "Тех. поддержка",
]

POSITIONS = LEVEL_1_POSITIONS + LEVEL_2_POSITIONS + LEVEL_3_POSITIONS
POSITION_GROUPS = [
    ("1 уровень", LEVEL_1_POSITIONS),
    ("2 уровень", LEVEL_2_POSITIONS),
    ("3 уровень", LEVEL_3_POSITIONS),
]


def is_level_1(position: str | None) -> bool:
    return position in LEVEL_1_POSITIONS


# Association: which users are members of which boards (projects)
board_members = Table(
    "board_members",
    Base.metadata,
    Column("project_id", ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True),
    Column("user_id", ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, unique=True, nullable=True)
    telegram_username = Column(String(255), nullable=True)
    name = Column(String(255), nullable=False)
    role = Column(SAEnum(UserRole, values_callable=lambda e: [x.value for x in e]),
                  default=UserRole.EMPLOYEE, nullable=False)
    position = Column(String(100), nullable=True)
    password_hash = Column(String(255), nullable=True)
    is_active = Column(Boolean, default=True, nullable=False, server_default="true")
    created_at = Column(DateTime, server_default=func.now())

    tasks = relationship("Task", back_populates="assignee", foreign_keys="Task.assignee_id")
    boards = relationship("Project", secondary=board_members, back_populates="members")


class Project(Base):
    __tablename__ = "projects"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False, unique=True)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())

    tasks = relationship("Task", back_populates="project")
    members = relationship("User", secondary=board_members, back_populates="boards")


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        Index("ix_tasks_assignee_status", "assignee_id", "status"),
        Index("ix_tasks_due_date", "due_date"),
        Index("ix_tasks_project_status", "project_id", "status"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    display_number = Column(Integer, nullable=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=True)
    title = Column(String(500), nullable=False)
    description = Column(Text, nullable=True)
    status = Column(SAEnum(TaskStatus, values_callable=lambda e: [x.value for x in e]),
                    default=TaskStatus.BACKLOG, nullable=False)
    priority = Column(Integer, default=DEFAULT_PRIORITY, nullable=False)
    is_bug = Column(Boolean, default=False, nullable=False)
    assignee_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    creator_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    estimated_hours = Column(Numeric(5, 2), nullable=True)
    due_date = Column(Date, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    archived_at = Column(DateTime, nullable=True)

    project = relationship("Project", back_populates="tasks")
    assignee = relationship("User", back_populates="tasks", foreign_keys=[assignee_id])
    creator = relationship("User", foreign_keys=[creator_id])
    comments = relationship("TaskComment", back_populates="task",
                            order_by="TaskComment.created_at", cascade="all, delete-orphan")

    @property
    def is_red(self) -> bool:
        """Красная метка: высший приоритет (0) или баг."""
        return self.priority == 0 or self.is_bug

    @property
    def is_overdue(self) -> bool:
        if not self.due_date or self.status in CLOSED_STATUSES:
            return False
        return self.due_date < datetime.date.today()

    @property
    def is_due_today(self) -> bool:
        if not self.due_date or self.status in CLOSED_STATUSES:
            return False
        return self.due_date == datetime.date.today()

    @property
    def is_hot(self) -> bool:
        if not self.due_date or self.status in CLOSED_STATUSES:
            return False
        delta = (self.due_date - datetime.date.today()).days
        return 0 <= delta <= 1


class TaskComment(Base):
    __tablename__ = "task_comments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(Integer, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    text = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())

    task = relationship("Task", back_populates="comments")
    user = relationship("User")
    attachments = relationship("Attachment", back_populates="comment",
                               cascade="all, delete-orphan")


class Attachment(Base):
    __tablename__ = "attachments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    comment_id = Column(Integer, ForeignKey("task_comments.id", ondelete="CASCADE"), nullable=False)
    filename = Column(String(500), nullable=False)
    stored_name = Column(String(500), nullable=False)
    content_type = Column(String(255), nullable=True)
    created_at = Column(DateTime, server_default=func.now())

    comment = relationship("TaskComment", back_populates="attachments")

    @property
    def is_image(self) -> bool:
        return bool(self.content_type and self.content_type.startswith("image/"))


class NotificationLog(Base):
    __tablename__ = "notification_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    task_id = Column(Integer, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=True)
    notification_type = Column(String(50), nullable=False)
    sent_at = Column(DateTime, server_default=func.now())

    user = relationship("User")
    task = relationship("Task")


class Settings(Base):
    __tablename__ = "settings"

    key = Column(String(100), primary_key=True)
    value = Column(Text, nullable=False)

    DEFAULTS = {
        "morning_summary_hour": "9",
        "morning_summary_minute": "0",
        "evening_summary_hour": "19",
        "evening_summary_minute": "0",
        "deadline_check_interval_minutes": "30",
        "deadline_warning_hours": "24",
        "timezone": "Europe/Moscow",
    }

    LABELS = {
        "morning_summary_hour": "Утренняя сводка",
        "morning_summary_minute": "Минута утренней сводки (0-59)",
        "evening_summary_hour": "Вечерняя сводка",
        "evening_summary_minute": "Минута вечерней сводки (0-59)",
        "deadline_check_interval_minutes": "Проверка дедлайнов (мин)",
        "deadline_warning_hours": "Предупреждать за (часов)",
        "timezone": "Часовой пояс",
    }
