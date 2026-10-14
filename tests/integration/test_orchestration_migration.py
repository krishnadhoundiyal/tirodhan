"""Upgrade/downgrade financial preservation and strict application schema comparison."""

import asyncio

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_operational_orchestration import admission, regional, reserve
from test_payment_preparation import assert_readiness_schema
from test_routing_dispatch import prepared

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_0027_preserves_populated_prior_financial_history_and_schema(
    database_session_factory,
):
    factory = database_session_factory
    await prepared(factory)
    async with factory() as session:
        before = (
            await session.execute(
                text(
                    "SELECT (SELECT count(*) FROM payment), "
                    "(SELECT count(*) FROM payment_attempt), "
                    "(SELECT count(*) FROM collection_request_item), "
                    "(SELECT jsonb_agg(to_jsonb(p) ORDER BY payment_id) "
                    "FROM payment p)"
                )
            )
        ).one()
    config = Config("alembic.ini")
    try:
        await asyncio.to_thread(command.downgrade, config, "0026_continuous_fleet")
        async with factory() as session:
            after = (
                await session.execute(
                    text(
                        "SELECT (SELECT count(*) FROM payment), "
                        "(SELECT count(*) FROM payment_attempt), "
                        "(SELECT count(*) FROM collection_request_item), "
                        "(SELECT jsonb_agg(to_jsonb(p) ORDER BY payment_id) "
                        "FROM payment p)"
                    )
                )
            ).one()
        assert after == before
    finally:
        await asyncio.to_thread(command.upgrade, config, "head")
    async with factory() as session:
        connection = await session.connection()
        await connection.run_sync(assert_readiness_schema)
        names = set(
            await session.scalars(text("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal"))
        )
        assert {
            "capacity_identity",
            "capacity_reservation_binding",
            "vehicle_safety_generation",
            "operating_region_history",
            "cargo_transfer_authority",
        } <= names


async def test_0027_downgrade_refuses_to_destroy_regional_history(database_session_factory):
    factory = database_session_factory
    await regional(factory)
    with pytest.raises(DBAPIError, match="preservation of operational history"):
        await asyncio.to_thread(command.downgrade, Config("alembic.ini"), "0026_continuous_fleet")
    async with factory() as session:
        assert (
            await session.scalar(text("SELECT version_num FROM alembic_version"))
            == "0027_operational_orchestration"
        )


async def test_capacity_generation_and_binding_are_sql_protected(database_session_factory):
    factory = database_session_factory
    data = await regional(factory, empty=True)
    clock, customer, context = data[6], data[8], data[9]
    command = admission(context, clock[0])
    certificate = await reserve(factory, customer, command, clock[0])
    with pytest.raises(DBAPIError, match="immutable and binds once"):
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE regional_capacity_reservation SET mass_g=mass_g+1 "
                    "WHERE reservation_id=:id"
                ),
                {"id": certificate},
            )
    with pytest.raises(DBAPIError, match="capacity binding does not match"):
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE regional_capacity_reservation SET request_id=:booking "
                    "WHERE reservation_id=:id"
                ),
                {"id": certificate, "booking": data[3][0]},
            )
