from __future__ import annotations

from collections.abc import AsyncIterator
from typing import cast
from uuid import UUID

from fastapi import HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.modules.customers.ports import AddressProtector


async def get_database_session(request: Request) -> AsyncIterator[AsyncSession]:
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.database_session_factory)
    async with factory() as session, session.begin():
        yield session


def get_current_user_id(request: Request) -> UUID:
    """Return identity established by the future authentication layer.

    Phase 1C intentionally does not accept a caller-controlled user ID or implement
    authentication. Tests may set this request state through a dependency override.
    """
    user_id = getattr(request.state, "authenticated_user_id", None)
    if not isinstance(user_id, UUID):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication integration is not configured",
        )
    return user_id


def get_address_protector(request: Request) -> AddressProtector:
    return cast(AddressProtector, request.app.state.address_protector)
