from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from azure.identity.aio import DefaultAzureCredential
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from tirodhan.core.config import Settings


async def _async_creator(settings: Settings) -> Any:
    import ssl

    import asyncpg
    from sqlalchemy.engine.url import make_url

    url = settings.database_url.get_secret_value()
    # Ensure it is a valid format before parsing
    if not url.startswith("postgresql+asyncpg://"):
        raise ValueError("Invalid URL scheme")

    parsed = make_url(url)

    host = parsed.host
    port = parsed.port or 5432
    user = parsed.username
    database = parsed.database
    password = parsed.password

    ssl_context = None

    if settings.database_entra_authentication:
        async with DefaultAzureCredential(
            managed_identity_client_id=settings.database_managed_identity_client_id
        ) as credential:
            token = await credential.get_token("https://ossrdbms-aad.database.windows.net/.default")
            password = token.token
        ssl_context = ssl.create_default_context()

    return await asyncpg.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
        ssl=ssl_context,
    )


def create_database_engine(settings: Settings, *, use_null_pool: bool = False) -> AsyncEngine:
    kwargs: dict[str, Any] = {
        "echo": settings.database_echo,
    }

    if settings.database_entra_authentication:
        kwargs["async_creator"] = lambda: _async_creator(settings)

    if use_null_pool:
        kwargs["poolclass"] = NullPool
    else:
        kwargs["pool_pre_ping"] = True
        if settings.db_pool_size is not None:
            kwargs["pool_size"] = settings.db_pool_size
        if settings.db_max_overflow is not None:
            kwargs["max_overflow"] = settings.db_max_overflow
        if settings.db_pool_timeout is not None:
            kwargs["pool_timeout"] = settings.db_pool_timeout

    url = settings.database_url.get_secret_value()
    if settings.database_entra_authentication:
        # async_creator doesn't use the password from URL, but SQLAlchemy still needs a valid URL.
        # We can pass the URL as is. It will just use async_creator to get the actual connection.
        return create_async_engine(url, **kwargs)

    return create_async_engine(url, **kwargs)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def session_scope(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session


async def is_database_ready(engine: AsyncEngine) -> bool:
    async with engine.connect() as connection:
        result = await connection.execute(text("SELECT postgis_version()"))
        return result.scalar_one_or_none() is not None
