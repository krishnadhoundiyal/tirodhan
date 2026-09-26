from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine


def require_test_database_url() -> str:
    database_url = os.getenv("TIRODHAN_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TIRODHAN_TEST_DATABASE_URL is not configured")
    return database_url


@pytest.fixture
def migrated_database_url(monkeypatch: pytest.MonkeyPatch) -> str:
    database_url = require_test_database_url()
    monkeypatch.setenv("TIRODHAN_DATABASE_URL", database_url)
    command.upgrade(Config("alembic.ini"), "head")
    return database_url


@pytest_asyncio.fixture
async def database_engine(migrated_database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(migrated_database_url)
    table_names = "outbox_event, inbox_message, idempotency_record, app_user"
    async with engine.begin() as connection:
        await connection.execute(text(f"TRUNCATE TABLE {table_names}"))

    try:
        yield engine
    finally:
        async with engine.begin() as connection:
            await connection.execute(text(f"TRUNCATE TABLE {table_names}"))
        await engine.dispose()


@pytest_asyncio.fixture
async def database_session_factory(
    database_engine: AsyncEngine,
) -> async_sessionmaker:
    return async_sessionmaker(database_engine, expire_on_commit=False)
