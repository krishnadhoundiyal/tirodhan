"""Populated pre-adoption upgrade, strict drift check and deliberate lossy-downgrade refusal."""

import asyncio

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_collection_payment import create_context, create_request, create_user
from test_manager_business import change, group, manager
from test_payment_preparation import assert_readiness_schema

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_manager_upgrade_preserves_populated_financial_facts_and_schema(
    database_session_factory,
):
    factory = database_session_factory
    config = Config("alembic.ini")
    await asyncio.to_thread(command.downgrade, config, "0032_projected_slot_allowances")
    try:
        user = await create_user(factory)
        context = await create_context(factory, user.user_id)
        await create_request(factory, user.user_id, context.serviceability_context_id)
        async with factory() as session:
            before = await session.scalar(
                text("SELECT jsonb_agg(to_jsonb(p) ORDER BY payment_id) FROM payment p")
            )
        await asyncio.to_thread(command.upgrade, config, "head")
        async with factory() as session:
            assert (
                await session.scalar(
                    text("SELECT jsonb_agg(to_jsonb(p) ORDER BY payment_id) FROM payment p")
                )
                == before
            )
            await (await session.connection()).run_sync(assert_readiness_schema)
    finally:
        await asyncio.to_thread(command.upgrade, config, "head")
    actor = await manager(factory)
    # A caller-supplied reason cannot impersonate a pre-migration baseline.
    await change(factory, actor, group().model_copy(update={"reason": "PRE_MIGRATION_BASELINE"}))
    with pytest.raises(DBAPIError, match="export adopted business revision history"):
        await asyncio.to_thread(command.downgrade, config, "0032_projected_slot_allowances")
    async with factory() as session:
        assert await session.scalar(text("SELECT count(*) FROM catalogue_group_revision")) == 1


async def test_downgrade_preserves_new_decision_metadata_on_a_legacy_replacement(
    database_session_factory,
):
    from test_regional_parallel import prepared

    from tirodhan.db.values import new_uuid7
    from tirodhan.modules.routing_operations.capacity_models import (
        ReplacementDecision,
        RiderReplacement,
    )

    factory = database_session_factory
    data, _ = await prepared(factory)
    async with factory() as session, session.begin():
        # Nullable new request metadata represents an existing pre-0034 request.
        identity = new_uuid7()
        session.add(
            RiderReplacement(
                replacement_id=identity,
                request_id=data[3][0],
                original_rider_id=data[0].rider_ids[0],
                region_id=data[7],
                status="REJECTED",
                requested_at=data[6][0],
                next_attempt_at=data[6][0],
            )
        )
        await session.flush()
        session.add(
            ReplacementDecision(
                decision_id=new_uuid7(),
                replacement_id=identity,
                manager_id=data[0].manager_id,
                decision="REJECT",
                version=1,
                decided_at=data[6][0],
                reason="MANAGER_DECISION",
            )
        )
    with pytest.raises(DBAPIError, match="export adopted replacement choices"):
        await asyncio.to_thread(
            command.downgrade, Config("alembic.ini"), "0033_manager_business_history"
        )
    async with factory() as session:
        assert (
            await session.scalar(
                text("SELECT reason FROM rider_replacement_decision WHERE replacement_id=:id"),
                {"id": identity},
            )
            == "MANAGER_DECISION"
        )
