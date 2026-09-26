from __future__ import annotations

import asyncio
import os

import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tirodhan.core.config import Settings
from tirodhan.main import create_app


def get_test_database_url() -> str:
    database_url = os.getenv("TIRODHAN_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TIRODHAN_TEST_DATABASE_URL is not configured")
    return database_url


@pytest.mark.integration
def test_alembic_upgrade_enables_postgis(monkeypatch: pytest.MonkeyPatch) -> None:
    database_url = get_test_database_url()
    monkeypatch.setenv("TIRODHAN_DATABASE_URL", database_url)

    command.upgrade(Config("alembic.ini"), "head")

    async def read_postgis_version() -> str | None:
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as connection:
                result = await connection.execute(text("SELECT postgis_version()"))
                return result.scalar_one_or_none()
        finally:
            await engine.dispose()

    assert asyncio.run(read_postgis_version()) is not None


@pytest.mark.integration
def test_phase_1b_migration_creates_expected_foundation_tables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = get_test_database_url()
    monkeypatch.setenv("TIRODHAN_DATABASE_URL", database_url)
    command.upgrade(Config("alembic.ini"), "head")

    async def read_table_names() -> set[str]:
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as connection:
                return await connection.run_sync(
                    lambda sync_connection: set(
                        sqlalchemy_inspect(sync_connection).get_table_names()
                    )
                )
        finally:
            await engine.dispose()

    table_names = asyncio.run(read_table_names())
    assert {
        "app_user",
        "idempotency_record",
        "inbox_message",
        "outbox_event",
    }.issubset(table_names)
    assert {
        "collection_request",
        "payment",
        "planning_batch",
        "refresh_session",
        "serviceability_context",
        "user_address",
        "user_phone",
        "user_role",
    }.isdisjoint(table_names)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_readiness_uses_lifespan_database_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = get_test_database_url()
    monkeypatch.setenv("TIRODHAN_DATABASE_URL", database_url)
    await asyncio.to_thread(command.upgrade, Config("alembic.ini"), "head")

    settings = Settings(
        _env_file=None,
        environment="test",
        database_url=database_url,
    )
    application = create_app(settings)

    async with application.router.lifespan_context(application):
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://test",
        ) as client:
            response = await client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
