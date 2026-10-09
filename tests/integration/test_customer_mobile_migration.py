import asyncio

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.integration


def test_customer_mobile_migration_roundtrip(migrated_database_url: str) -> None:
    config = Config("alembic.ini")
    command.downgrade(config, "0017_fleet_dispatch_notification")
    command.upgrade(config, "0018_customer_mobile_core")

    async def verify() -> None:
        engine = create_async_engine(migrated_database_url)
        try:
            async with engine.connect() as connection:
                names = await connection.run_sync(lambda c: inspect(c).get_table_names())
                assert {
                    "catalogue_media",
                    "catalogue_category",
                    "catalogue_group",
                    "catalogue_artwork",
                } <= set(names)
                assert await connection.scalar(text("SELECT count(*) FROM catalogue_category")) == 0
                columns = await connection.run_sync(
                    lambda c: inspect(c).get_columns("collection_request_item")
                )
                assert any(c["name"] == "display_name_snapshot" and c["nullable"] for c in columns)
                indexes = await connection.run_sync(
                    lambda c: inspect(c).get_indexes("collection_request")
                )
                assert any(i["name"] == "ix_customer_collection_page" for i in indexes)
        finally:
            await engine.dispose()

    asyncio.run(verify())
