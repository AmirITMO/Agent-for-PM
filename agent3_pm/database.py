from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from agent3_pm.config import config
from agent3_pm.models import Base

_async_engine = None
_AsyncSessionLocal = None


def get_async_engine():
    global _async_engine
    if _async_engine is None:
        _async_engine = create_async_engine(config.DATABASE_URL, echo=False)
    return _async_engine


def get_session_factory():
    global _AsyncSessionLocal
    if _AsyncSessionLocal is None:
        _AsyncSessionLocal = async_sessionmaker(
            get_async_engine(), class_=AsyncSession, expire_on_commit=False
        )
    return _AsyncSessionLocal


class _SessionLocalProxy:
    def __call__(self):
        return get_session_factory()()

    def __getattr__(self, name):
        return getattr(get_session_factory(), name)


AsyncSessionLocal = _SessionLocalProxy()


async def init_db():
    engine = get_async_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_async_session() -> AsyncSession:
    factory = get_session_factory()
    async with factory() as session:
        yield session
