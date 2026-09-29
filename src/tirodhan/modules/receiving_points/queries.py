from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tirodhan.modules.receiving_points.models import ReceivingPoint


async def list_active_receiving_points(session: AsyncSession) -> list[ReceivingPoint]:
    return list(
        await session.scalars(
            select(ReceivingPoint)
            .where(ReceivingPoint.status == "ACTIVE")
            .order_by(ReceivingPoint.official_name, ReceivingPoint.receiving_point_id)
        )
    )
