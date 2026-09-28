from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from tirodhan.core.config import Settings
from tirodhan.db.base import Base
from tirodhan.modules.collection_requests import models as collection_request_models  # noqa: F401
from tirodhan.modules.customers import models as customer_models  # noqa: F401
from tirodhan.modules.dispatch import models as dispatch_models  # noqa: F401
from tirodhan.modules.identity import models as identity_models  # noqa: F401
from tirodhan.modules.payments import models as payment_models  # noqa: F401
from tirodhan.modules.pickups import models as pickup_models  # noqa: F401
from tirodhan.modules.planning import models as planning_models  # noqa: F401
from tirodhan.modules.reliability import models as reliability_models  # noqa: F401
from tirodhan.modules.serviceability import models as serviceability_models  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_database_url() -> str:
    return Settings().database_url.get_secret_value()


def run_migrations_offline() -> None:
    context.configure(
        url=get_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = get_database_url()
    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
