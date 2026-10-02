from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_rider_dispatch import create_fixture

from tirodhan.core.config import Settings
from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.main import create_app
from tirodhan.modules.dispatch.service import assign_group_manually
from tirodhan.modules.operations.service import reassign_outstanding_work


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
def test_phase_1d_completion_migration_creates_refund_lifecycle(
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
    assert "refund" in tables
    assert "refund_id" in event_columns


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

    async def schema_state() -> tuple[set[str], set[str]]:
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as connection:
                return await connection.run_sync(
                    lambda sync_connection: (
                        set(sqlalchemy_inspect(sync_connection).get_table_names()),
                        {
                            index["name"]
                            for index in sqlalchemy_inspect(sync_connection).get_indexes(
                                "pickup_execution"
                            )
                        },
                    )
                )
        finally:
            await engine.dispose()

    command.upgrade(configuration, "head")
    tables, indexes = asyncio.run(schema_state())
    assert dispatch_tables.issubset(tables)
    assert "ix_pickup_execution_collection_group_id" in indexes
    command.downgrade(configuration, "0007_compaction_planner")
    tables, indexes = asyncio.run(schema_state())
    assert dispatch_tables.isdisjoint(tables)
    assert {"planning_batch", "collection_group", "pickup_execution"}.issubset(tables)
    assert "ix_pickup_execution_collection_group_id" not in indexes
    command.upgrade(configuration, "head")
    tables, indexes = asyncio.run(schema_state())
    assert dispatch_tables.issubset(tables)
    assert "ix_pickup_execution_collection_group_id" in indexes


@pytest.mark.integration
def test_phase_1i_migration_roundtrip_restores_lifecycle_constraints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = get_test_database_url()
    monkeypatch.setenv("TIRODHAN_DATABASE_URL", database_url)
    configuration = Config("alembic.ini")

    async def schema_state() -> tuple[
        set[str], dict[str, str], dict[str, str], set[str], dict[str, str]
    ]:
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as connection:
                tables = await connection.run_sync(
                    lambda sync_connection: set(
                        sqlalchemy_inspect(sync_connection).get_table_names()
                    )
                )
                assignment_constraints = dict(
                    (
                        row.name,
                        row.definition,
                    )
                    for row in (
                        await connection.execute(
                            text(
                                "SELECT conname AS name, pg_get_constraintdef(oid) AS definition "
                                "FROM pg_constraint "
                                "WHERE conrelid = 'rider_assignment'::regclass"
                            )
                        )
                    )
                )
                pickup_constraints = dict(
                    (
                        row.name,
                        row.definition,
                    )
                    for row in (
                        await connection.execute(
                            text(
                                "SELECT conname AS name, pg_get_constraintdef(oid) AS definition "
                                "FROM pg_constraint "
                                "WHERE conrelid = 'pickup_execution'::regclass"
                            )
                        )
                    )
                )
                offer_columns, offer_foreign_keys = await connection.run_sync(
                    lambda sync_connection: (
                        {
                            column["name"]
                            for column in sqlalchemy_inspect(sync_connection).get_columns(
                                "assignment_offer"
                            )
                        },
                        {
                            foreign_key["name"]: foreign_key["referred_table"]
                            for foreign_key in sqlalchemy_inspect(sync_connection).get_foreign_keys(
                                "assignment_offer"
                            )
                        },
                    )
                )
                return (
                    tables,
                    assignment_constraints,
                    pickup_constraints,
                    offer_columns,
                    offer_foreign_keys,
                )
        finally:
            await engine.dispose()

    async def seed_phase_1h_resolved_offers() -> tuple[object, object, object]:
        rider_id = new_uuid7()
        losing_rider_id = new_uuid7()
        batch_id = new_uuid7()
        group_id = new_uuid7()
        assignment_id = new_uuid7()
        accepted_offer_id = new_uuid7()
        losing_offer_id = new_uuid7()
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO app_user (user_id, status, created_at, updated_at) "
                        "VALUES (:rider_id, 'ACTIVE', now(), now()), "
                        "(:losing_rider_id, 'ACTIVE', now(), now())"
                    ),
                    {"rider_id": rider_id, "losing_rider_id": losing_rider_id},
                )
                await connection.execute(
                    text(
                        "INSERT INTO planning_batch "
                        "(planning_batch_id, cell_id, slot_start, slot_end, status, "
                        "max_attempts_snapshot, created_at) "
                        "VALUES (:batch_id, 'migration-cell', now(), "
                        "now() + interval '30 minutes', "
                        "'COMPLETED', 1, now())"
                    ),
                    {"batch_id": batch_id},
                )
                await connection.execute(
                    text(
                        "INSERT INTO collection_group "
                        "(collection_group_id, planning_batch_id, planning_mode, created_at) "
                        "VALUES (:group_id, :batch_id, 'NORMAL_SINGLETON', now())"
                    ),
                    {"group_id": group_id, "batch_id": batch_id},
                )
                await connection.execute(
                    text(
                        "INSERT INTO rider_profile "
                        "(rider_id, status, created_at, updated_at) "
                        "VALUES (:rider_id, 'ACTIVE', now(), now()), "
                        "(:losing_rider_id, 'ACTIVE', now(), now())"
                    ),
                    {"rider_id": rider_id, "losing_rider_id": losing_rider_id},
                )
                await connection.execute(
                    text(
                        "INSERT INTO rider_assignment "
                        "(assignment_id, collection_group_id, rider_id, source, status, "
                        "created_at, assigned_at) "
                        "VALUES (:assignment_id, :group_id, :rider_id, "
                        "'RIDER_OFFER_ACCEPTED', 'ACTIVE', now(), now())"
                    ),
                    {
                        "assignment_id": assignment_id,
                        "group_id": group_id,
                        "rider_id": rider_id,
                    },
                )
                await connection.execute(
                    text(
                        "INSERT INTO assignment_offer "
                        "(offer_id, collection_group_id, rider_id, offer_round, status, "
                        "offered_at, expires_at, responded_at) VALUES "
                        "(:accepted_offer_id, :group_id, :rider_id, 1, 'ACCEPTED', "
                        "now(), now() + interval '5 minutes', now()), "
                        "(:losing_offer_id, :group_id, :losing_rider_id, 1, 'CLOSED_LOST', "
                        "now(), now() + interval '5 minutes', NULL)"
                    ),
                    {
                        "accepted_offer_id": accepted_offer_id,
                        "losing_offer_id": losing_offer_id,
                        "group_id": group_id,
                        "rider_id": rider_id,
                        "losing_rider_id": losing_rider_id,
                    },
                )
        finally:
            await engine.dispose()
        return accepted_offer_id, losing_offer_id, assignment_id

    async def resolved_assignments() -> dict[object, object]:
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as connection:
                return {
                    row.offer_id: row.resolved_assignment_id
                    for row in await connection.execute(
                        text(
                            "SELECT offer_id, resolved_assignment_id FROM assignment_offer "
                            "WHERE status IN ('ACCEPTED', 'CLOSED_LOST')"
                        )
                    )
                }
        finally:
            await engine.dispose()

    command.upgrade(configuration, "head")
    tables, assignment_constraints, pickup_constraints, offer_columns, offer_fks = asyncio.run(
        schema_state()
    )
    assert "pickup_attempt" in tables
    assert "COMPLETED" in assignment_constraints["ck_rider_assignment_status"]
    assert "COLLECTED" in pickup_constraints["ck_pickup_execution_status"]
    assert "resolved_assignment_id" in offer_columns
    assert offer_fks["fk_assignment_offer_resolved_assignment"] == "rider_assignment"

    command.downgrade(configuration, "0008_rider_dispatch_assignment")
    tables, assignment_constraints, pickup_constraints, offer_columns, offer_fks = asyncio.run(
        schema_state()
    )
    assert "pickup_attempt" not in tables
    assert "COMPLETED" not in assignment_constraints["ck_rider_assignment_status"]
    assert "ACTIVE" in assignment_constraints["ck_rider_assignment_status"]
    assert "ck_pickup_execution_status" not in pickup_constraints
    assert {"rider_assignment", "rider_assignment_item", "pickup_execution"}.issubset(tables)
    assert "resolved_assignment_id" not in offer_columns
    assert "fk_assignment_offer_resolved_assignment" not in offer_fks

    accepted_offer_id, losing_offer_id, assignment_id = asyncio.run(seed_phase_1h_resolved_offers())

    command.upgrade(configuration, "head")
    tables, assignment_constraints, pickup_constraints, offer_columns, offer_fks = asyncio.run(
        schema_state()
    )
    assert "pickup_attempt" in tables
    assert "COMPLETED" in assignment_constraints["ck_rider_assignment_status"]
    assert "COLLECTED" in pickup_constraints["ck_pickup_execution_status"]
    assert "resolved_assignment_id" in offer_columns
    assert offer_fks["fk_assignment_offer_resolved_assignment"] == "rider_assignment"
    links = asyncio.run(resolved_assignments())
    assert links[accepted_offer_id] == assignment_id
    assert links[losing_offer_id] == assignment_id

    command.downgrade(configuration, "0008_rider_dispatch_assignment")
    _tables, _assignment_constraints, _pickup_constraints, offer_columns, offer_fks = asyncio.run(
        schema_state()
    )
    assert "resolved_assignment_id" not in offer_columns
    assert "fk_assignment_offer_resolved_assignment" not in offer_fks

    command.upgrade(configuration, "head")
    _tables, _assignment_constraints, _pickup_constraints, offer_columns, offer_fks = asyncio.run(
        schema_state()
    )
    assert "resolved_assignment_id" in offer_columns
    assert offer_fks["fk_assignment_offer_resolved_assignment"] == "rider_assignment"
    links = asyncio.run(resolved_assignments())
    assert links[accepted_offer_id] == assignment_id
    assert links[losing_offer_id] == assignment_id


@pytest.mark.integration
def test_phase_1i_downgrade_preserves_completed_assignment_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = get_test_database_url()
    monkeypatch.setenv("TIRODHAN_DATABASE_URL", database_url)
    configuration = Config("alembic.ini")
    rider_id = new_uuid7()
    batch_id = new_uuid7()
    group_id = new_uuid7()
    assignment_id = new_uuid7()

    async def seed_completed_assignment() -> object:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO app_user (user_id, status, created_at, updated_at) "
                        "VALUES (:rider_id, 'ACTIVE', now(), now())"
                    ),
                    {"rider_id": rider_id},
                )
                await connection.execute(
                    text(
                        "INSERT INTO planning_batch "
                        "(planning_batch_id, cell_id, slot_start, slot_end, status, "
                        "max_attempts_snapshot, created_at) "
                        "VALUES (:batch_id, :cell_id, now(), "
                        "now() + interval '30 minutes', 'COMPLETED', 1, now())"
                    ),
                    {"batch_id": batch_id, "cell_id": f"migration-{batch_id}"},
                )
                await connection.execute(
                    text(
                        "INSERT INTO collection_group "
                        "(collection_group_id, planning_batch_id, planning_mode, created_at) "
                        "VALUES (:group_id, :batch_id, 'NORMAL_SINGLETON', now())"
                    ),
                    {"group_id": group_id, "batch_id": batch_id},
                )
                await connection.execute(
                    text(
                        "INSERT INTO rider_profile "
                        "(rider_id, status, created_at, updated_at) "
                        "VALUES (:rider_id, 'ACTIVE', now(), now())"
                    ),
                    {"rider_id": rider_id},
                )
                return (
                    await connection.execute(
                        text(
                            "INSERT INTO rider_assignment "
                            "(assignment_id, collection_group_id, rider_id, source, status, "
                            "created_at, assigned_at, completed_at) "
                            "VALUES (:assignment_id, :group_id, :rider_id, "
                            "'RIDER_OFFER_ACCEPTED', 'COMPLETED', now(), now(), now()) "
                            "RETURNING completed_at"
                        ),
                        {
                            "assignment_id": assignment_id,
                            "group_id": group_id,
                            "rider_id": rider_id,
                        },
                    )
                ).scalar_one()
        finally:
            await engine.dispose()

    async def downgraded_state() -> tuple[str, object, set[str], str, set[str], dict[str, str]]:
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as connection:
                assignment = (
                    await connection.execute(
                        text(
                            "SELECT status, completed_at FROM rider_assignment "
                            "WHERE assignment_id = :assignment_id"
                        ),
                        {"assignment_id": assignment_id},
                    )
                ).one()
                constraint = (
                    await connection.execute(
                        text(
                            "SELECT pg_get_constraintdef(oid) "
                            "FROM pg_constraint "
                            "WHERE conrelid = 'rider_assignment'::regclass "
                            "AND conname = 'ck_rider_assignment_status'"
                        )
                    )
                ).scalar_one()
                tables, offer_columns, offer_foreign_keys = await connection.run_sync(
                    lambda sync_connection: (
                        set(sqlalchemy_inspect(sync_connection).get_table_names()),
                        {
                            column["name"]
                            for column in sqlalchemy_inspect(sync_connection).get_columns(
                                "assignment_offer"
                            )
                        },
                        {
                            foreign_key["name"]: foreign_key["referred_table"]
                            for foreign_key in sqlalchemy_inspect(sync_connection).get_foreign_keys(
                                "assignment_offer"
                            )
                        },
                    )
                )
                return (
                    assignment.status,
                    assignment.completed_at,
                    tables,
                    constraint,
                    offer_columns,
                    offer_foreign_keys,
                )
        finally:
            await engine.dispose()

    command.upgrade(configuration, "head")
    completed_at = asyncio.run(seed_completed_assignment())
    command.downgrade(configuration, "0008_rider_dispatch_assignment")
    status, retained_completed_at, tables, constraint, offer_columns, offer_fks = asyncio.run(
        downgraded_state()
    )
    assert status == "ACTIVE"
    assert retained_completed_at == completed_at
    assert "ACTIVE" in constraint
    assert "COMPLETED" not in constraint
    assert "pickup_attempt" not in tables
    assert "resolved_assignment_id" not in offer_columns
    assert "fk_assignment_offer_resolved_assignment" not in offer_fks

    command.upgrade(configuration, "head")
    assert "pickup_attempt" in asyncio.run(downgraded_state())[2]


@pytest.mark.integration
def test_phase_1j_migration_preserves_reassignment_history_on_lossy_downgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = get_test_database_url()
    monkeypatch.setenv("TIRODHAN_DATABASE_URL", database_url)
    configuration = Config("alembic.ini")

    async def seed_reassignment() -> tuple[UUID, UUID, UUID, datetime]:
        engine = create_async_engine(database_url)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            fixture = await create_fixture(factory, rider_count=2, pickups_per_group=2)
            predecessor = await assign_group_manually(
                factory,
                collection_group_id=fixture.group_ids[0],
                rider_id=fixture.rider_ids[0],
                manager_user_id=fixture.manager_id,
            )
            superseded_at = utc_now().replace(microsecond=0)
            successor = await reassign_outstanding_work(
                factory,
                predecessor_assignment_id=predecessor.assignment_id,
                replacement_rider_id=fixture.rider_ids[1],
                manager_user_id=fixture.manager_id,
                client_reassignment_id=new_uuid7(),
                idempotency_expires_at=superseded_at + timedelta(days=1),
                now=superseded_at,
            )
            return (
                predecessor.assignment_id,
                successor.assignment_id,
                fixture.pickup_ids[0][0],
                superseded_at,
            )
        finally:
            await engine.dispose()

    async def schema_and_history(
        predecessor_id: UUID, successor_id: UUID, pickup_id: UUID
    ) -> tuple[set[str], set[str], str, datetime, str, datetime, UUID]:
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as connection:
                tables, assignment_columns = await connection.run_sync(
                    lambda sync_connection: (
                        set(sqlalchemy_inspect(sync_connection).get_table_names()),
                        {
                            column["name"]
                            for column in sqlalchemy_inspect(sync_connection).get_columns(
                                "rider_assignment"
                            )
                        },
                    )
                )
                predecessor = (
                    await connection.execute(
                        text(
                            "SELECT status, completed_at FROM rider_assignment "
                            "WHERE assignment_id = :assignment_id"
                        ),
                        {"assignment_id": predecessor_id},
                    )
                ).one()
                successor_status = await connection.scalar(
                    text(
                        "SELECT status FROM rider_assignment WHERE assignment_id = :assignment_id"
                    ),
                    {"assignment_id": successor_id},
                )
                released_at = await connection.scalar(
                    text(
                        "SELECT released_at FROM rider_assignment_item "
                        "WHERE assignment_id = :assignment_id "
                        "AND pickup_execution_id = :pickup_id"
                    ),
                    {"assignment_id": predecessor_id, "pickup_id": pickup_id},
                )
                successor_item = await connection.scalar(
                    text(
                        "SELECT pickup_execution_id FROM rider_assignment_item "
                        "WHERE assignment_id = :assignment_id "
                        "AND pickup_execution_id = :pickup_id"
                    ),
                    {"assignment_id": successor_id, "pickup_id": pickup_id},
                )
                return (
                    tables,
                    assignment_columns,
                    predecessor.status,
                    predecessor.completed_at,
                    successor_status,
                    released_at,
                    successor_item,
                )
        finally:
            await engine.dispose()

    command.upgrade(configuration, "head")
    predecessor_id, successor_id, pickup_id, superseded_at = asyncio.run(seed_reassignment())
    command.downgrade(configuration, "0009_pickup_execution_lifecycle")
    (
        tables,
        assignment_columns,
        predecessor_status,
        predecessor_completed_at,
        successor_status,
        released_at,
        successor_item,
    ) = asyncio.run(schema_and_history(predecessor_id, successor_id, pickup_id))
    assert predecessor_status == "COMPLETED"
    assert predecessor_completed_at == superseded_at
    assert successor_status == "ACTIVE"
    assert released_at == superseded_at
    assert successor_item == pickup_id
    assert "pickup_incident" not in tables
    assert "superseded_at" not in assignment_columns

    command.upgrade(configuration, "head")
    tables, assignment_columns, predecessor_status, _, successor_status, _, _ = asyncio.run(
        schema_and_history(predecessor_id, successor_id, pickup_id)
    )
    assert predecessor_status == "COMPLETED"
    assert successor_status == "ACTIVE"
    assert "pickup_incident" in tables
    assert "superseded_at" in assignment_columns


@pytest.mark.integration
def test_phase_1k_migration_roundtrip_only_controls_handover_tables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = get_test_database_url()
    monkeypatch.setenv("TIRODHAN_DATABASE_URL", database_url)
    configuration = Config("alembic.ini")
    handover_tables = {"receiving_point", "handover_event", "handover_event_item"}

    async def schema_state() -> tuple[
        set[str],
        set[str],
        set[str],
        set[str],
        set[str],
        set[str],
        set[str],
        str,
        str,
    ]:
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as connection:
                (
                    tables,
                    event_columns,
                    item_columns,
                    event_fks,
                    item_fks,
                ) = await connection.run_sync(
                    lambda sync_connection: (
                        set(sqlalchemy_inspect(sync_connection).get_table_names()),
                        {
                            column["name"]
                            for column in sqlalchemy_inspect(sync_connection).get_columns(
                                "handover_event"
                            )
                        },
                        {
                            column["name"]
                            for column in sqlalchemy_inspect(sync_connection).get_columns(
                                "handover_event_item"
                            )
                        },
                        {
                            foreign_key["referred_table"]
                            for foreign_key in sqlalchemy_inspect(sync_connection).get_foreign_keys(
                                "handover_event"
                            )
                        },
                        {
                            foreign_key["referred_table"]
                            for foreign_key in sqlalchemy_inspect(sync_connection).get_foreign_keys(
                                "handover_event_item"
                            )
                        },
                    )
                )
                check_names = {
                    row.name
                    for row in await connection.execute(
                        text(
                            "SELECT conname AS name FROM pg_constraint "
                            "WHERE conrelid IN ("
                            "'receiving_point'::regclass, 'handover_event'::regclass, "
                            "'handover_event_item'::regclass) AND contype = 'c'"
                        )
                    )
                }
                unique_names = {
                    row.name
                    for row in await connection.execute(
                        text(
                            "SELECT conname AS name FROM pg_constraint "
                            "WHERE conrelid = 'handover_event'::regclass AND contype = 'u'"
                        )
                    )
                }
                index_definition = str(
                    await connection.scalar(
                        text(
                            "SELECT indexdef FROM pg_indexes "
                            "WHERE indexname = 'ix_receiving_point_location_gist'"
                        )
                    )
                )
                item_index_definition = str(
                    await connection.scalar(
                        text(
                            "SELECT indexdef FROM pg_indexes "
                            "WHERE indexname = "
                            "'uq_handover_event_item_validated_pickup'"
                        )
                    )
                )
                return (
                    tables,
                    event_columns,
                    item_columns,
                    event_fks,
                    item_fks,
                    check_names,
                    unique_names,
                    index_definition,
                    item_index_definition,
                )
        finally:
            await engine.dispose()

    command.upgrade(configuration, "head")
    (
        tables,
        event_columns,
        item_columns,
        event_fks,
        item_fks,
        checks,
        unique_constraints,
        receiving_point_index,
        validated_pickup_index,
    ) = asyncio.run(schema_state())
    assert handover_tables.issubset(tables)
    assert event_columns == {
        "handover_event_id",
        "client_handover_id",
        "rider_id",
        "receiving_point_id",
        "occurred_at",
        "observed_location",
        "receiving_point_location_snapshot",
        "allowed_radius_m_snapshot",
        "distance_m",
        "status",
        "validation_code",
        "created_at",
        "evaluated_at",
    }
    assert item_columns == {
        "handover_event_id",
        "pickup_execution_id",
        "status",
        "created_at",
        "evaluated_at",
    }
    assert event_fks == {"rider_profile", "receiving_point"}
    assert item_fks == {"handover_event", "pickup_execution"}
    assert {
        "ck_receiving_point_status",
        "ck_receiving_point_positive_allowed_radius",
        "ck_receiving_point_positive_version",
        "ck_handover_event_status",
        "ck_handover_event_validation_code",
        "ck_handover_event_validation_consistency",
        "ck_handover_event_positive_radius_snapshot",
        "ck_handover_event_nonnegative_distance",
        "ck_handover_event_item_status",
    }.issubset(checks)
    assert "uq_handover_event_client_handover" in unique_constraints
    assert "USING gist" in receiving_point_index
    assert "UNIQUE INDEX" in validated_pickup_index
    assert "WHERE ((status)::text = 'VALIDATED'::text)" in validated_pickup_index

    command.downgrade(configuration, "0010_pickup_incident_reassign")
    downgraded_tables = asyncio.run(_table_names_for_database(database_url))
    assert handover_tables.isdisjoint(downgraded_tables)
    assert {"pickup_incident", "pickup_attempt", "rider_assignment"}.issubset(downgraded_tables)

    command.upgrade(configuration, "head")
    assert handover_tables.issubset(asyncio.run(schema_state())[0])


@pytest.mark.integration
def test_phase_1l_migration_roundtrip_only_controls_evidence_capture_tables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = get_test_database_url()
    monkeypatch.setenv("TIRODHAN_DATABASE_URL", database_url)
    configuration = Config("alembic.ini")
    evidence_tables = {
        "evidence_capture",
        "pickup_evidence_link",
        "handover_evidence_link",
    }

    async def schema_state() -> tuple[
        set[str],
        set[str],
        dict[str, object],
        dict[str, object],
        dict[str, object],
        set[tuple[tuple[str, ...], str]],
        set[tuple[tuple[str, ...], str]],
        set[tuple[tuple[str, ...], str]],
    ]:
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as connection:
                return await connection.run_sync(
                    lambda sync_connection: (
                        set(sqlalchemy_inspect(sync_connection).get_table_names()),
                        {
                            column["name"]
                            for column in sqlalchemy_inspect(sync_connection).get_columns(
                                "evidence_capture"
                            )
                        },
                        sqlalchemy_inspect(sync_connection).get_pk_constraint("evidence_capture"),
                        sqlalchemy_inspect(sync_connection).get_pk_constraint(
                            "pickup_evidence_link"
                        ),
                        sqlalchemy_inspect(sync_connection).get_pk_constraint(
                            "handover_evidence_link"
                        ),
                        {
                            (tuple(constraint["column_names"]), "UNIQUE")
                            for constraint in sqlalchemy_inspect(
                                sync_connection
                            ).get_unique_constraints("evidence_capture")
                        }
                        | {
                            (
                                tuple(constraint["constrained_columns"]),
                                constraint["referred_table"],
                            )
                            for constraint in sqlalchemy_inspect(sync_connection).get_foreign_keys(
                                "evidence_capture"
                            )
                        },
                        {
                            (
                                tuple(constraint["constrained_columns"]),
                                constraint["referred_table"],
                            )
                            for constraint in sqlalchemy_inspect(sync_connection).get_foreign_keys(
                                "pickup_evidence_link"
                            )
                        }
                        | {
                            (
                                tuple(constraint["column_names"]),
                                "UNIQUE",
                            )
                            for constraint in sqlalchemy_inspect(
                                sync_connection
                            ).get_unique_constraints("pickup_evidence_link")
                        },
                        {
                            (
                                tuple(constraint["constrained_columns"]),
                                constraint["referred_table"],
                            )
                            for constraint in sqlalchemy_inspect(sync_connection).get_foreign_keys(
                                "handover_evidence_link"
                            )
                        }
                        | {
                            (
                                tuple(constraint["column_names"]),
                                "UNIQUE",
                            )
                            for constraint in sqlalchemy_inspect(
                                sync_connection
                            ).get_unique_constraints("handover_evidence_link")
                        },
                    )
                )
        finally:
            await engine.dispose()

    command.downgrade(configuration, "0011_receiving_point_handover")
    command.upgrade(configuration, "0012_evidence_capture_foundation")
    (
        tables,
        capture_columns,
        capture_pk,
        pickup_pk,
        handover_pk,
        capture_constraints,
        pickup_constraints,
        handover_constraints,
    ) = asyncio.run(schema_state())
    assert evidence_tables.issubset(tables)
    assert "media_asset" not in tables
    assert capture_columns == {
        "evidence_capture_id",
        "client_capture_id",
        "captured_by_user_id",
        "captured_at",
        "created_at",
    }
    assert {"status", "validated_at", "evidence_type", "capture_location"}.isdisjoint(
        capture_columns
    )
    assert capture_pk["constrained_columns"] == ["evidence_capture_id"]
    assert pickup_pk["constrained_columns"] == [
        "pickup_execution_id",
        "evidence_capture_id",
    ]
    assert handover_pk["constrained_columns"] == [
        "handover_event_id",
        "evidence_capture_id",
    ]
    assert capture_constraints == {
        (("client_capture_id",), "UNIQUE"),
        (("captured_by_user_id",), "app_user"),
    }
    assert pickup_constraints == {
        (("pickup_execution_id",), "pickup_execution"),
        (("evidence_capture_id",), "evidence_capture"),
        (("evidence_capture_id",), "UNIQUE"),
    }
    assert handover_constraints == {
        (("handover_event_id",), "handover_event"),
        (("evidence_capture_id",), "evidence_capture"),
        (("evidence_capture_id",), "UNIQUE"),
    }

    command.downgrade(configuration, "0011_receiving_point_handover")
    downgraded_tables = asyncio.run(_table_names_for_database(database_url))
    assert evidence_tables.isdisjoint(downgraded_tables)
    assert {"pickup_execution", "handover_event", "rider_assignment"}.issubset(downgraded_tables)

    command.upgrade(configuration, "0012_evidence_capture_foundation")
    assert evidence_tables.issubset(asyncio.run(schema_state())[0])
    command.upgrade(configuration, "head")


@pytest.mark.integration
def test_phase_1m_migration_roundtrip_only_controls_media_asset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = get_test_database_url()
    monkeypatch.setenv("TIRODHAN_DATABASE_URL", database_url)
    configuration = Config("alembic.ini")

    async def schema_state() -> tuple[
        set[str],
        dict[str, tuple[bool, str]],
        list[str],
        set[str],
        set[str],
        dict[str, str],
    ]:
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as connection:
                return await connection.run_sync(
                    lambda sync_connection: (
                        set(sqlalchemy_inspect(sync_connection).get_table_names()),
                        {
                            column["name"]: (
                                bool(column["nullable"]),
                                str(column["type"]),
                            )
                            for column in sqlalchemy_inspect(sync_connection).get_columns(
                                "media_asset"
                            )
                        },
                        sqlalchemy_inspect(sync_connection)
                        .get_pk_constraint("media_asset")
                        .get("constrained_columns", []),
                        {
                            constraint["name"]
                            for constraint in sqlalchemy_inspect(
                                sync_connection
                            ).get_unique_constraints("media_asset")
                        },
                        {
                            constraint["referred_table"]
                            for constraint in sqlalchemy_inspect(sync_connection).get_foreign_keys(
                                "media_asset"
                            )
                        },
                        {
                            constraint["name"]: constraint["sqltext"]
                            for constraint in sqlalchemy_inspect(
                                sync_connection
                            ).get_check_constraints("media_asset")
                        },
                    )
                )
        finally:
            await engine.dispose()

    command.upgrade(configuration, "head")
    tables, columns, primary_key, uniques, foreign_tables, checks = asyncio.run(schema_state())
    assert "media_asset" in tables
    assert set(columns) == {
        "media_asset_id",
        "client_media_id",
        "evidence_capture_id",
        "media_type",
        "object_key",
        "expected_content_type",
        "stored_content_type",
        "size_bytes",
        "upload_status",
        "created_at",
        "finalized_at",
    }
    assert {"content_hash", "uploaded_at", "original_filename", "uploader_id"}.isdisjoint(columns)
    assert columns["stored_content_type"][0]
    assert columns["size_bytes"][0]
    assert columns["finalized_at"][0]
    assert primary_key == ["media_asset_id"]
    assert uniques == {
        "uq_media_asset_client_media",
        "uq_media_asset_evidence_capture",
        "uq_media_asset_object_key",
    }
    assert foreign_tables == {"evidence_capture"}
    assert set(checks) == {
        "ck_media_asset_media_type",
        "ck_media_asset_upload_status",
        "ck_media_asset_nonnegative_size",
        "ck_media_asset_lifecycle_consistency",
    }
    assert all(value in checks["ck_media_asset_media_type"] for value in ("PHOTO", "VIDEO"))
    assert all(
        value in checks["ck_media_asset_upload_status"] for value in ("PENDING_UPLOAD", "FINALIZED")
    )
    assert all(
        value in checks["ck_media_asset_lifecycle_consistency"]
        for value in ("stored_content_type", "size_bytes", "finalized_at")
    )

    user_id = new_uuid7()
    evidence_id = new_uuid7()

    async def seed_and_count_evidence() -> int:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO app_user "
                        "(user_id, status, created_at, updated_at) "
                        "VALUES (:user_id, 'ACTIVE', now(), now())"
                    ),
                    {"user_id": user_id},
                )
                await connection.execute(
                    text(
                        "INSERT INTO evidence_capture "
                        "(evidence_capture_id, client_capture_id, captured_by_user_id, "
                        "captured_at, created_at) "
                        "VALUES (:evidence_id, :client_id, :user_id, now(), now())"
                    ),
                    {
                        "evidence_id": evidence_id,
                        "client_id": new_uuid7(),
                        "user_id": user_id,
                    },
                )
                return int(
                    (
                        await connection.execute(
                            text(
                                "SELECT count(*) FROM evidence_capture "
                                "WHERE evidence_capture_id = :evidence_id"
                            ),
                            {"evidence_id": evidence_id},
                        )
                    ).scalar_one()
                )
        finally:
            await engine.dispose()

    assert asyncio.run(seed_and_count_evidence()) == 1
    command.downgrade(configuration, "0012_evidence_capture_foundation")
    downgraded_tables = asyncio.run(_table_names_for_database(database_url))
    assert "media_asset" not in downgraded_tables
    assert {"evidence_capture", "pickup_evidence_link", "handover_evidence_link"}.issubset(
        downgraded_tables
    )

    async def retained_evidence_count() -> int:
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as connection:
                return int(
                    (
                        await connection.execute(
                            text(
                                "SELECT count(*) FROM evidence_capture "
                                "WHERE evidence_capture_id = :evidence_id"
                            ),
                            {"evidence_id": evidence_id},
                        )
                    ).scalar_one()
                )
        finally:
            await engine.dispose()

    assert asyncio.run(retained_evidence_count()) == 1
    command.upgrade(configuration, "head")
    assert "media_asset" in asyncio.run(_table_names_for_database(database_url))
    assert asyncio.run(retained_evidence_count()) == 1


async def _table_names_for_database(database_url: str) -> set[str]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            return await connection.run_sync(
                lambda sync_connection: set(sqlalchemy_inspect(sync_connection).get_table_names())
            )
    finally:
        await engine.dispose()


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
