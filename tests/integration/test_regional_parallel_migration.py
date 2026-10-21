"""Populated upgrade/downgrade, strict drift detection and relational history guards."""

import asyncio

import pytest
from alembic import command
from alembic.config import Config
from legacy_financial_fixture import legacy_financial_fixture as financial_fixture
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_payment_preparation import assert_readiness_schema

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_0028_round_trip_preserves_financial_history_and_application_schema(
    database_session_factory,
):
    factory = database_session_factory
    await financial_fixture(factory)

    async def history():
        async with factory() as session:
            return (
                await session.execute(
                    text(
                        "SELECT (SELECT jsonb_agg(to_jsonb(p) ORDER BY payment_id) FROM payment p),"
                        "(SELECT jsonb_agg(to_jsonb(a) ORDER BY payment_attempt_id) "
                        "FROM payment_attempt a)"
                    )
                )
            ).one()

    before = await history()
    try:
        await asyncio.to_thread(
            command.downgrade, Config("alembic.ini"), "0027_operational_orchestration"
        )
        assert await history() == before
    finally:
        await asyncio.to_thread(command.upgrade, Config("alembic.ini"), "head")
    assert await history() == before
    async with factory() as session:
        await (await session.connection()).run_sync(assert_readiness_schema)
        assert (
            await session.scalar(
                text(
                    "SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal AND "
                    "tgname IN ('initiated_item_terms','initiated_quote_terms',"
                    "'initiated_payment_terms')"
                )
            )
            == 3
        )


async def test_0028_refuses_history_loss_and_sql_mutation(database_session_factory):
    pytest.importorskip("ortools", reason="composite activation requires routing extra")
    from test_regional_parallel import prepared, run

    factory = database_session_factory
    values, _ = await prepared(factory)
    await run(factory, values)
    with pytest.raises(DBAPIError, match="regional history|export adopted business"):
        await asyncio.to_thread(
            command.downgrade, Config("alembic.ini"), "0027_operational_orchestration"
        )
    for sql in (
        "UPDATE regional_planning_outcome SET reason='erased'",
        "DELETE FROM routing_shift_confirmation",
    ):
        with pytest.raises(DBAPIError, match="immutable"):
            async with factory() as session, session.begin():
                await session.execute(text(sql))
