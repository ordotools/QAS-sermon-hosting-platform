import asyncio
import os
import tempfile
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite://")
os.environ.setdefault("SUPERUSER_EMAIL", "admin@test.com")
os.environ.setdefault("SUPERUSER_PASSWORD", "testpass123")
os.environ.setdefault("DEBUG", "true")
os.environ.setdefault("STORAGE_BACKEND", "local")

from app.config import get_settings
from app.main import app
from app.storage import get_storage

get_settings.cache_clear()
get_storage.cache_clear()


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session(engine) -> AsyncGenerator[AsyncSession, None]:
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s


@pytest_asyncio.fixture
async def client(engine, tmp_path, monkeypatch) -> AsyncGenerator[AsyncClient, None]:
    from app import database
    from app.services.auth import bootstrap_superuser, create_user

    media_path = str(tmp_path / "media")
    monkeypatch.setenv("LOCAL_STORAGE_PATH", media_path)
    get_settings.cache_clear()
    get_storage.cache_clear()

    database.engine = engine
    database.async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with database.async_session() as s:
        await bootstrap_superuser(s, "admin@test.com", "testpass123")
        await create_user(s, "user@test.com", "userpass123")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def auth_client(client) -> AsyncClient:
    from app.deps import SESSION_COOKIE, create_session_token
    from app.services.auth import get_user_by_email
    from app import database

    async with database.async_session() as s:
        user = await get_user_by_email(s, "user@test.com")
    token = create_session_token(user.id)
    client.cookies.set(SESSION_COOKIE, token)
    return client


@pytest_asyncio.fixture
async def admin_client(client) -> AsyncClient:
    from app.deps import SESSION_COOKIE, create_session_token
    from app.services.auth import get_user_by_email
    from app import database

    async with database.async_session() as s:
        user = await get_user_by_email(s, "admin@test.com")
    token = create_session_token(user.id)
    client.cookies.set(SESSION_COOKIE, token)
    return client
