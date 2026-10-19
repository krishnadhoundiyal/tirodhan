"""Finite regional commercial writer leases, fenced again at publication."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .admission_models import AdmissionPreparationLease
from .commands import OperationalInputError
from .orchestration_models import CapacityAdmissionRequest


async def claim(
    session: AsyncSession, region: UUID, token: UUID, now: datetime, until: datetime
) -> bool:
    statement = insert(AdmissionPreparationLease).values(
        region_id=region,
        token=token,
        expires_at=until,
    )
    acquired = await session.scalar(
        statement.on_conflict_do_update(
            index_elements=[AdmissionPreparationLease.region_id],
            set_={"token": token, "expires_at": until},
            where=or_(
                AdmissionPreparationLease.token == token,
                AdmissionPreparationLease.expires_at <= now,
            ),
        ).returning(AdmissionPreparationLease.region_id)
    )
    return acquired is not None


async def fence(session: AsyncSession, region: UUID, token: UUID, now: datetime) -> None:
    owner = await session.scalar(
        select(AdmissionPreparationLease)
        .where(
            AdmissionPreparationLease.region_id == region,
        )
        .with_for_update()
    )
    if owner is None or owner.token != token or owner.expires_at <= now:
        raise OperationalInputError("ADMISSION_REGIONAL_LEASE_LOST")


async def progress(
    factory: async_sessionmaker[AsyncSession],
    region: UUID,
    token: UUID,
    intent: UUID,
    intent_token: UUID,
    now: datetime,
    cursor: dict[str, Any],
) -> None:
    async with factory() as session, session.begin():
        await fence(session, region, token, now)
        # Publication also fences the region first. Do not wait on a claim's
        # intent lock: that claim may itself be waiting for this regional lease.
        # A checkpoint is per command, never shared by two yielded intents.
        job = await session.scalar(
            select(CapacityAdmissionRequest)
            .where(
                CapacityAdmissionRequest.admission_id == intent,
                CapacityAdmissionRequest.region_id == region,
                CapacityAdmissionRequest.lease_token == intent_token,
                CapacityAdmissionRequest.lease_until > now,
                CapacityAdmissionRequest.status == "RUNNING",
            )
            .with_for_update(skip_locked=True)
        )
        if job is None:
            raise OperationalInputError("ADMISSION_LEASE_LOST")
        job.preparation_cursor = cursor


async def release(factory: async_sessionmaker[AsyncSession], token: UUID) -> None:
    async with factory() as session, session.begin():
        # Checkpoints belong to individual intents and survive owner release.
        await session.execute(
            update(AdmissionPreparationLease)
            .where(
                AdmissionPreparationLease.token == token,
            )
            .values(expires_at=datetime.min.replace(tzinfo=timezone.utc))
        )
