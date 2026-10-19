"""Common database arbitration for idle transfer and shift-close admission promises."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from .admission_models import AdmissionSourceHead
from .orchestration_models import RegionVehicle


async def has_capacity_commitments(
    session: AsyncSession,
    vehicle_id: UUID,
    now: datetime,
) -> bool:
    await session.execute(
        select(AdmissionSourceHead)
        .where(
            AdmissionSourceHead.region_id.in_(
                select(RegionVehicle.region_id).where(RegionVehicle.vehicle_id == vehicle_id)
            )
        )
        .order_by(AdmissionSourceHead.region_id)
        .with_for_update()
    )
    return bool(
        await session.scalar(
            text("SELECT routing_capacity_committed(:vehicle,:at_time)"),
            {"vehicle": vehicle_id, "at_time": now},
        )
    )
