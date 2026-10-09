import asyncio

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.integration


def test_capture_metadata_migration_roundtrip_retains_existing_event(migrated_database_url):
    config = Config("alembic.ini")

    async def seed():
        engine = create_async_engine(migrated_database_url)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO payment_provider_event "
                        "(payment_provider_event_id, provider, external_event_id, "
                        "event_type, processing_status, received_at) VALUES "
                        "('11111111-1111-4111-8111-111111111111', 'TEST', "
                        "'migration-retained', 'test', 'RECEIVED', now())"
                    )
                )
        finally:
            await engine.dispose()

    asyncio.run(seed())
    command.downgrade(config, "0018_customer_mobile_core")
    command.upgrade(config, "head")

    async def verify():
        engine = create_async_engine(migrated_database_url)
        try:
            async with engine.begin() as connection:
                columns = await connection.run_sync(
                    lambda c: inspect(c).get_columns("payment_provider_event")
                )
                for field in ("provider_payment_id", "amount_minor", "currency"):
                    assert any(c["name"] == field and c["nullable"] for c in columns)
                assert (
                    await connection.scalar(
                        text(
                            "SELECT count(*) FROM payment_provider_event "
                            "WHERE external_event_id='migration-retained' "
                            "AND provider_payment_id IS NULL AND amount_minor IS NULL "
                            "AND currency IS NULL"
                        )
                    )
                    == 1
                )
                with pytest.raises(IntegrityError):
                    async with connection.begin_nested():
                        await connection.execute(
                            text(
                                "UPDATE payment_provider_event SET amount_minor=0 "
                                "WHERE external_event_id='migration-retained'"
                            )
                        )
                await connection.execute(
                    text(
                        "DELETE FROM payment_provider_event "
                        "WHERE external_event_id='migration-retained'"
                    )
                )
        finally:
            await engine.dispose()

    asyncio.run(verify())
