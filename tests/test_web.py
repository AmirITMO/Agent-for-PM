"""Tests for web interface endpoints."""
import datetime
import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from httpx import AsyncClient, ASGITransport

from agent3_pm.models import Base, TaskStatus
from agent3_pm import repository as repo
from agent3_pm.web import app, get_session

pytestmark = pytest.mark.asyncio
TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture
async def test_app():
    engine = create_async_engine(TEST_DB_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def override():
        async with factory() as s:
            yield s

    app.dependency_overrides[get_session] = override

    async with factory() as session:
        user = await repo.create_user(session, "Test User", telegram_id=100, position="CEO")
        await repo.create_project(session, "Dev")
        await repo.create_task(session, "Task 1", 1, status=TaskStatus.TODO)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, user

    app.dependency_overrides.clear()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


class TestWelcome:
    async def test_no_auth_shows_welcome(self, test_app):
        client, _ = test_app
        resp = await client.get("/", follow_redirects=False)
        assert resp.status_code == 200


class TestBoard:
    async def test_board_200(self, test_app):
        client, user = test_app
        resp = await client.get(f"/enter/{user.id}", follow_redirects=False)
        cookies = resp.cookies
        resp = await client.get("/board", cookies=cookies)
        assert resp.status_code == 200

    async def test_task_detail_200(self, test_app):
        client, user = test_app
        resp = await client.get(f"/enter/{user.id}", follow_redirects=False)
        cookies = resp.cookies
        resp = await client.get("/task/1", cookies=cookies)
        assert resp.status_code == 200

    async def test_task_not_found(self, test_app):
        client, user = test_app
        resp = await client.get(f"/enter/{user.id}", follow_redirects=False)
        cookies = resp.cookies
        resp = await client.get("/task/99999", cookies=cookies)
        assert resp.status_code == 404


class TestTaskAPI:
    async def test_create_task(self, test_app):
        client, user = test_app
        resp = await client.get(f"/enter/{user.id}", follow_redirects=False)
        cookies = resp.cookies
        resp = await client.post("/api/tasks", data={
            "title": "New", "project_id": "1", "status": "backlog",
            "priority": "2", "redirect": "/board",
        }, cookies=cookies, follow_redirects=False)
        assert resp.status_code == 303
