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
    configuration = Config("alembic.ini")
    command.downgrade(configuration, "0002_domain_reliability")

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

    try:
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
            "user_phone",
            "user_role",
        }.isdisjoint(table_names)
    finally:
        command.upgrade(configuration, "head")


@pytest.mark.integration
def test_phase_1c_migration_downgrade_and_reupgrade(monkeypatch: pytest.MonkeyPatch) -> None:
    database_url = get_test_database_url()
    monkeypatch.setenv("TIRODHAN_DATABASE_URL", database_url)
    configuration = Config("alembic.ini")

    command.upgrade(configuration, "head")
    command.downgrade(configuration, "0002_domain_reliability")
    command.upgrade(configuration, "head")

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

    assert {"user_address", "serviceability_context"}.issubset(asyncio.run(read_table_names()))


@pytest.mark.integration
def test_phase_1d_migration_creates_payment_subset_without_refund(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = get_test_database_url()
    monkeypatch.setenv("TIRODHAN_DATABASE_URL", database_url)
    command.upgrade(Config("alembic.ini"), "head")

    async def inspect_schema() -> tuple[set[str], set[str]]:
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as connection:
                return await connection.run_sync(
                    lambda sync_connection: (
                        set(sqlalchemy_inspect(sync_connection).get_table_names()),
                        {
                            column["name"]
                            for column in sqlalchemy_inspect(sync_connection).get_columns(
                                "payment_provider_event"
                            )
                        },
                    )
                )
        finally:
            await engine.dispose()

    tables, event_columns = asyncio.run(inspect_schema())
    assert {
        "collection_request",
        "collection_request_item",
        "payment",
        "payment_attempt",
        "payment_provider_event",
        "planning_batch",
    }.issubset(tables)
    assert "refund" not in tables
    assert "refund_id" not in event_columns


@pytest.mark.integration
def test_phase_1e_migration_downgrades_and_reupgrades_planning_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = get_test_database_url()
    monkeypatch.setenv("TIRODHAN_DATABASE_URL", database_url)
    configuration = Config("alembic.ini")

    async def table_names() -> set[str]:
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

    command.upgrade(configuration, "head")
    command.downgrade(configuration, "0004_request_payment")
    assert "planning_batch_attempt" not in asyncio.run(table_names())
    command.upgrade(configuration, "head")
    assert "planning_batch_attempt" in asyncio.run(table_names())


@pytest.mark.integration
def test_phase_1f_migration_roundtrip_only_controls_result_tables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = get_test_database_url()
    monkeypatch.setenv("TIRODHAN_DATABASE_URL", database_url)
    configuration = Config("alembic.ini")
    result_tables = {"collection_group", "collection_group_member", "pickup_execution"}

    async def table_names() -> set[str]:
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

    command.upgrade(configuration, "head")
    assert result_tables.issubset(asyncio.run(table_names()))
    command.downgrade(configuration, "0005_planning_freeze")
    downgraded = asyncio.run(table_names())
    assert result_tables.isdisjoint(downgraded)
    assert "planning_batch_attempt" in downgraded
    command.upgrade(configuration, "head")
    assert result_tables.issubset(asyncio.run(table_names()))


@pytest.mark.integration
def test_phase_1g_migration_roundtrip_controls_policy_columns_and_indexes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = get_test_database_url()
    monkeypatch.setenv("TIRODHAN_DATABASE_URL", database_url)
    configuration = Config("alembic.ini")
    policy_columns = {
        "compaction_distance_m_snapshot",
        "max_group_requests_snapshot",
    }
    planner_indexes = {
        "ix_collection_request_planning_batch_id",
        "ix_collection_request_pickup_location_gist",
    }

    async def inspect_planner_schema() -> tuple[set[str], set[str], set[str]]:
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as connection:
                return await connection.run_sync(
                    lambda sync_connection: (
                        {
                            column["name"]
                            for column in sqlalchemy_inspect(sync_connection).get_columns(
                                "planning_batch"
                            )
                        },
                        {
                            index["name"]
                            for index in sqlalchemy_inspect(sync_connection).get_indexes(
                                "collection_request"
                            )
                        },
                        set(sqlalchemy_inspect(sync_connection).get_table_names()),
                    )
                )
        finally:
            await engine.dispose()

    command.upgrade(configuration, "head")
    columns, indexes, tables = asyncio.run(inspect_planner_schema())
    assert policy_columns.issubset(columns)
    assert planner_indexes.issubset(indexes)
    assert {"collection_group", "collection_group_member", "pickup_execution"}.issubset(tables)

    command.downgrade(configuration, "0006_planning_result")
    columns, indexes, tables = asyncio.run(inspect_planner_schema())
    assert policy_columns.isdisjoint(columns)
    assert planner_indexes.isdisjoint(indexes)
    assert {"collection_group", "collection_group_member", "pickup_execution"}.issubset(tables)

    command.upgrade(configuration, "head")
    columns, indexes, _tables = asyncio.run(inspect_planner_schema())
    assert policy_columns.issubset(columns)
    assert planner_indexes.issubset(indexes)


@pytest.mark.integration
def test_phase_1h_migration_roundtrip_only_controls_dispatch_tables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = get_test_database_url()
    monkeypatch.setenv("TIRODHAN_DATABASE_URL", database_url)
    configuration = Config("alembic.ini")
    dispatch_tables = {
        "rider_profile",
        "rider_availability",
        "assignment_offer",
        "rider_assignment",
        "rider_assignment_item",
    }

    async def table_names() -> set[str]:
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

    command.upgrade(configuration, "head")
    assert dispatch_tables.issubset(asyncio.run(table_names()))
    command.downgrade(configuration, "0007_compaction_planner")
    downgraded = asyncio.run(table_names())
    assert dispatch_tables.isdisjoint(downgraded)
    assert {"planning_batch", "collection_group", "pickup_execution"}.issubset(downgraded)
    command.upgrade(configuration, "head")
    assert dispatch_tables.issubset(asyncio.run(table_names()))


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
