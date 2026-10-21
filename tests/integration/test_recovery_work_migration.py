"""Populated generation cutover preserves delivery IDs and financial history."""

import asyncio
from datetime import timedelta

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_payment_preparation import assert_readiness_schema
from test_recovery_work_requests import allocation

from tirodhan.db.values import new_uuid7
from tirodhan.modules.reliability.models import OutboxEvent
from tirodhan.modules.routing_operations.failure_execution import claim_activation
from tirodhan.modules.routing_operations.failure_models import RecoveryAssignment
from tirodhan.modules.routing_operations.recovery_work import current_work, delivery_current

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
PREVIOUS = "0035_rider_failure_recovery"


@pytest.mark.parametrize("legacy_retry", [False, True])
async def test_populated_upgrade_adopts_published_legacy_identity_and_coalesces_pending(
    database_session_factory,
    legacy_retry,
):
    factory = database_session_factory
    data, _, leg, work, _ = await allocation(factory)
    now, retained, recommendation_delivery = data[6][0], new_uuid7(), new_uuid7()
    async with factory() as session:
        financial = await session.scalar(
            text("SELECT jsonb_agg(to_jsonb(p) ORDER BY payment_id) FROM payment p")
        )
    with pytest.raises(DBAPIError, match="export adopted recovery work requests"):
        await asyncio.to_thread(command.downgrade, Config("alembic.ini"), PREVIOUS)
    # Export notification state for the fixture, then populate the exact pre-0036 schema.
    async with factory() as session, session.begin():
        await session.execute(text("DELETE FROM recovery_work_request"))
    await asyncio.to_thread(command.downgrade, Config("alembic.ini"), PREVIOUS)
    try:
        async with factory() as session, session.begin():
            if legacy_retry:
                await session.execute(
                    text(
                        "UPDATE recovery_assignment SET attempt=1,due_at=:due "
                        "WHERE assignment_id=:id"
                    ),
                    {"id": leg.assignment_id, "due": now + timedelta(seconds=10)},
                )
                replacement = await session.scalar(
                    text(
                        "SELECT replacement_id FROM rider_replacement "
                        "WHERE status='REQUESTED' LIMIT 1"
                    )
                )
                await session.execute(
                    text(
                        "UPDATE rider_replacement SET next_attempt_at=:due,attempt=7 "
                        "WHERE replacement_id=:id"
                    ),
                    {"id": replacement, "due": now + timedelta(seconds=10)},
                )
                session.add(
                    OutboxEvent(
                        outbox_event_id=recommendation_delivery,
                        event_key=f"legacy-recovery:{recommendation_delivery}",
                        aggregate_id=replacement,
                        aggregate_type="routing_recovery",
                        event_type="RecoveryWorkRequested",
                        status="PUBLISHED",
                        payload={"work_id": str(replacement), "kind": "RECOMMEND"},
                        created_at=now - timedelta(minutes=1),
                        available_at=now,
                        published_at=now,
                        publish_attempt_count=1,
                    )
                )
            session.add(
                OutboxEvent(
                    outbox_event_id=retained,
                    event_key=f"legacy-recovery:{retained}",
                    aggregate_id=leg.assignment_id,
                    aggregate_type="routing_recovery",
                    event_type="RecoveryWorkRequested",
                    status="PUBLISHED",
                    payload={"work_id": str(leg.assignment_id), "kind": "ACTIVATE"},
                    created_at=now - timedelta(minutes=1),
                    available_at=now,
                    published_at=now,
                    publish_attempt_count=1,
                )
            )
        await asyncio.to_thread(command.upgrade, Config("alembic.ini"), "head")
        async with factory() as session:
            current = await current_work(session, leg.assignment_id, "ACTIVATE")
            if legacy_retry:
                assert current is None
                assert await current_work(session, replacement, "RECOMMEND") is None
            else:
                assert current.outbox_event_id == retained and current.state == "PUBLISHED"
                assert current.generation == 1
            assert (await session.get(OutboxEvent, work.outbox_event_id)).status == "CANCELLED"
            assert (await session.get(RecoveryAssignment, leg.assignment_id)).attempt == int(
                legacy_retry
            )
            assert (
                await session.scalar(
                    text("SELECT jsonb_agg(to_jsonb(p) ORDER BY payment_id) FROM payment p")
                )
                == financial
            )
            await (await session.connection()).run_sync(assert_readiness_schema)
        assert await delivery_current(factory, retained, leg.assignment_id, "ACTIVATE", None) == (
            None if legacy_retry else 1
        )
        assert (
            await delivery_current(factory, work.outbox_event_id, leg.assignment_id, "ACTIVATE", 1)
            is None
        )
        if legacy_retry:
            from tirodhan.modules.routing_operations.failure_runtime import reconcile_recovery

            assert await reconcile_recovery(factory, now) == 0
            assert await reconcile_recovery(factory, now + timedelta(seconds=10)) == 2
            async with factory() as session:
                assert (await current_work(session, leg.assignment_id, "ACTIVATE")).generation == 2
                assert (await current_work(session, replacement, "RECOMMEND")).generation == 2
        async with factory() as session, session.begin():
            current = await current_work(session, leg.assignment_id, "ACTIVATE", lock=True)
            current.generation += 1
            with pytest.raises(DBAPIError, match="identity is immutable"):
                await session.flush()
            await session.rollback()
    finally:
        await asyncio.to_thread(command.upgrade, Config("alembic.ini"), "head")


async def test_upgrade_requires_execution_drain_and_empty_roundtrip_is_supported(
    database_session_factory,
):
    factory = database_session_factory
    # Empty upgrade/downgrade is reversible with the strict application drift assertion.
    await asyncio.to_thread(command.downgrade, Config("alembic.ini"), PREVIOUS)
    await asyncio.to_thread(command.upgrade, Config("alembic.ini"), "head")
    async with factory() as session:
        await (await session.connection()).run_sync(assert_readiness_schema)
    data, _, leg, _, _ = await allocation(factory)
    assert await claim_activation(factory, leg.assignment_id, data[6][0])
    async with factory() as session, session.begin():
        # Ensure the simulated historical lease is live against PostgreSQL's actual clock.
        await session.execute(
            text(
                "UPDATE recovery_assignment SET lease_until=now()+interval '1 minute' "
                "WHERE assignment_id=:id"
            ),
            {"id": leg.assignment_id},
        )
        await session.execute(text("DELETE FROM recovery_work_request"))
    await asyncio.to_thread(command.downgrade, Config("alembic.ini"), PREVIOUS)
    try:
        with pytest.raises(DBAPIError, match="drain recovery execution leases"):
            await asyncio.to_thread(command.upgrade, Config("alembic.ini"), "head")
    finally:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE recovery_assignment SET lease_token=NULL,lease_until=NULL,"
                    "status='PENDING'"
                )
            )
        await asyncio.to_thread(command.upgrade, Config("alembic.ini"), "head")
