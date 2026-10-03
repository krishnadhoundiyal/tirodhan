from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.db.values import utc_now
from tirodhan.modules.dispatch.models import PushRegistration
from tirodhan.modules.dispatch.service import _lock_rider, _require_fresh_rider_authorization


async def register_push_device(
    factory: async_sessionmaker[AsyncSession],
    *,
    rider_id: UUID,
    client_device_id: str,
    platform: str,
    registration_token: str,
) -> PushRegistration:
    if (
        not 0 < len(client_device_id) <= 200
        or platform not in ("ANDROID", "IOS")
        or not 0 < len(registration_token) <= 4096
    ):
        raise ValueError("invalid push registration")
    async with factory() as session, session.begin():
        await _lock_rider(session, rider_id)
        await _require_fresh_rider_authorization(session, rider_id)
        registration = await session.scalar(
            select(PushRegistration)
            .where(
                PushRegistration.rider_id == rider_id,
                PushRegistration.client_device_id == client_device_id,
                PushRegistration.revoked_at.is_(None),
            )
            .with_for_update()
        )
        now = utc_now()
        if registration is None:
            registration = PushRegistration(
                rider_id=rider_id,
                client_device_id=client_device_id,
                platform=platform,
                provider="FCM",
                registration_token=registration_token,
                created_at=now,
                updated_at=now,
            )
            session.add(registration)
        elif (
            registration.registration_token != registration_token
            or registration.platform != platform
        ):
            registration.registration_token = registration_token
            registration.platform = platform
            registration.updated_at = now
        await session.flush()
        return registration


async def revoke_push_device(
    factory: async_sessionmaker[AsyncSession], *, rider_id: UUID, client_device_id: str
) -> None:
    async with factory() as session, session.begin():
        await _lock_rider(session, rider_id)
        await _require_fresh_rider_authorization(session, rider_id)
        registration = await session.scalar(
            select(PushRegistration)
            .where(
                PushRegistration.rider_id == rider_id,
                PushRegistration.client_device_id == client_device_id,
                PushRegistration.revoked_at.is_(None),
            )
            .with_for_update()
        )
        if registration is not None:
            registration.revoked_at = utc_now()
            registration.updated_at = registration.revoked_at
