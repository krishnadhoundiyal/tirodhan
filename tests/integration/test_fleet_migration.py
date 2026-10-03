from __future__ import annotations

import asyncio
import os

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

from tirodhan.db.base import Base
from tirodhan.modules.dispatch import models  # noqa: F401

TABLES = {
    "fleet",
    "fleet_membership",
    "fleet_service_cell",
    "rider_service_cell",
    "push_registration",
    "offer_notification_delivery",
}


@pytest.mark.integration
def test_0017_roundtrip_and_metadata_alignment(monkeypatch):
    url = os.environ.get("TIRODHAN_TEST_DATABASE_URL")
    if not url:
        pytest.skip("disposable PostgreSQL is required")
    monkeypatch.setenv("TIRODHAN_DATABASE_URL", url)
    config = Config("alembic.ini")

    async def schema(upgraded):
        engine = create_async_engine(url)
        try:
            async with engine.connect() as connection:

                def check(sync):
                    inspector = inspect(sync)
                    assert (
                        TABLES.issubset(inspector.get_table_names())
                        if upgraded
                        else TABLES.isdisjoint(inspector.get_table_names())
                    )
                    columns = {
                        column["name"] for column in inspector.get_columns("assignment_offer")
                    }
                    assert (
                        {"fleet_id", "audience_kind"}.issubset(columns)
                        if upgraded
                        else {"fleet_id", "audience_kind"}.isdisjoint(columns)
                    )
                    if upgraded:

                        def include_object(obj, name, kind, reflected, compare_to):
                            if kind == "table":
                                return name in TABLES | {"assignment_offer"}
                            return True

                        context = MigrationContext.configure(
                            sync,
                            opts={
                                "include_object": include_object,
                                "compare_type": True,
                                "compare_server_default": True,
                            },
                        )
                        assert compare_metadata(context, Base.metadata) == []
                        for table in TABLES:
                            expected = {
                                constraint.name
                                for constraint in Base.metadata.tables[table].constraints
                                if constraint.__class__.__name__ == "CheckConstraint"
                            }
                            assert {
                                constraint["name"]
                                for constraint in inspector.get_check_constraints(table)
                            } == expected

                await connection.run_sync(check)
        finally:
            await engine.dispose()

    command.upgrade(config, "head")
    asyncio.run(schema(True))
    try:
        command.downgrade(config, "0016_refund_lifecycle")
        asyncio.run(schema(False))
    finally:
        command.upgrade(config, "head")
    asyncio.run(schema(True))
