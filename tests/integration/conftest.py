from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from tirodhan.modules.customers.ports import AddressProtector


class DeterministicTestAddressProtector(AddressProtector):
    """Test-only reversible envelope; never used by application configuration."""

    _prefix = b"test-envelope-v1:"

    async def protect(self, plaintext: str) -> bytes:
        return self._prefix + plaintext.encode("utf-8")

    async def unprotect(self, protected: bytes) -> str:
        if not protected.startswith(self._prefix):
            raise ValueError("unexpected test envelope")
        return protected.removeprefix(self._prefix).decode("utf-8")


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
    table_names = (
        "payment_provider_event, payment_attempt, payment, collection_request_item, "
        "collection_request, planning_batch, serviceability_context, user_address, "
        "outbox_event, inbox_message, idempotency_record, app_user"
    )
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


@pytest.fixture
def address_protector() -> DeterministicTestAddressProtector:
    return DeterministicTestAddressProtector()
