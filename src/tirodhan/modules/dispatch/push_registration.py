from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.dispatch.models import PushRegistration


async def register_push_device(
    session: AsyncSession,
    rider_id: UUID,
    client_device_id: str,
    provider: str,
    platform: str,
    registration_token: str,
) -> PushRegistration:
    existing = await session.scalar(
        select(PushRegistration)
        .where(
            PushRegistration.rider_id == rider_id,
            PushRegistration.client_device_id == client_device_id,
            PushRegistration.revoked_at.is_(None),
        )
        .with_for_update()
    )
    if existing:
        existing.provider = provider
        existing.platform = platform
        existing.registration_token = registration_token
        existing.updated_at = utc_now()
        await session.flush()
        return existing
    registration = PushRegistration(
        push_registration_id=new_uuid7(),
        rider_id=rider_id,
        client_device_id=client_device_id,
        provider=provider,
        platform=platform,
        registration_token=registration_token,
    )
    session.add(registration)
    await session.flush()
    return registration


async def revoke_push_device(session: AsyncSession, rider_id: UUID, client_device_id: str) -> None:
    registrations = await session.scalars(
        select(PushRegistration)
        .where(
            PushRegistration.rider_id == rider_id,
            PushRegistration.client_device_id == client_device_id,
            PushRegistration.revoked_at.is_(None),
        )
        .with_for_update()
    )
    for reg in registrations:
        reg.revoked_at = utc_now()
    await session.flush()
