import datetime
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from agent3_pm.models import Base, UserRole, TaskStatus, DEFAULT_PRIORITY
from agent3_pm import repository as repo

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def async_engine():
    engine = create_async_engine(TEST_DB_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def session(async_engine):
    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session


@pytest_asyncio.fixture
async def sample_data(session):
    admin = await repo.create_user(session, "Admin User", telegram_id=111111,
                                   position="CEO", role=UserRole.EMPLOYEE)
    employee1 = await repo.create_user(session, "Employee One", telegram_id=222222,
                                       position="МОП")
    employee2 = await repo.create_user(session, "Employee Two", telegram_id=333333,
                                       position="Программист")
    no_tg_user = await repo.create_user(session, "No TG User")

    dev = await repo.create_project(session, "Dev")
    marketing = await repo.create_project(session, "Marketing")

    today = datetime.date.today()

    overdue = await repo.create_task(
        session, "Overdue bug fix", dev.id,
        status=TaskStatus.WIP, priority=0, is_bug=True,
        assignee_id=employee1.id,
        due_date=today - datetime.timedelta(days=2),
    )
    due_today = await repo.create_task(
        session, "Due today feature", dev.id,
        status=TaskStatus.TODO, priority=1,
        assignee_id=employee1.id,
        due_date=today,
    )
    hot = await repo.create_task(
        session, "Hot task tomorrow", dev.id,
        status=TaskStatus.TODO, priority=DEFAULT_PRIORITY,
        assignee_id=employee2.id,
        due_date=today + datetime.timedelta(days=1),
    )
    future = await repo.create_task(
        session, "Future task", dev.id,
        status=TaskStatus.BACKLOG,
        assignee_id=employee1.id,
        due_date=today + datetime.timedelta(days=30),
    )
    done = await repo.create_task(
        session, "Done task", dev.id,
        status=TaskStatus.DONE,
        assignee_id=employee1.id,
        due_date=today - datetime.timedelta(days=5),
    )
    no_date = await repo.create_task(
        session, "No due date task", marketing.id,
        status=TaskStatus.WIP,
        assignee_id=employee2.id,
    )
    unassigned = await repo.create_task(
        session, "Unassigned backlog", marketing.id,
        status=TaskStatus.BACKLOG,
    )
    hold = await repo.create_task(
        session, "On hold task", dev.id,
        status=TaskStatus.HOLD,
        assignee_id=employee1.id,
        due_date=today - datetime.timedelta(days=1),
    )

    return {
        "admin": admin, "employee1": employee1, "employee2": employee2,
        "no_tg_user": no_tg_user,
        "dev": dev, "marketing": marketing,
        "overdue": overdue, "due_today": due_today, "hot": hot,
        "future": future, "done": done, "no_date": no_date,
        "unassigned": unassigned, "hold": hold,
    }
