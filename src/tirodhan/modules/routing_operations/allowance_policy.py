"""Immutable, manager-attributed estimator policy versions."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .allowance_estimator import AllowancePolicy
from .capacity_models import SlotAllowanceConfiguration


async def current_policy(session: AsyncSession, region_id: UUID) -> AllowancePolicy | None:
    row = await session.scalar(
        select(SlotAllowanceConfiguration)
        .where(SlotAllowanceConfiguration.region_id == region_id)
        .order_by(SlotAllowanceConfiguration.version.desc())
        .limit(1)
    )
    return AllowancePolicy.model_validate(row.configuration) if row else None
