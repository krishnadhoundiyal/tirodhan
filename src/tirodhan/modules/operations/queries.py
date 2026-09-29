from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tirodhan.modules.pickups.models import PickupIncident


async def list_open_incidents(
    session: AsyncSession,
    *,
    limit: int,
) -> list[PickupIncident]:
    return list(
        await session.scalars(
            select(PickupIncident)
            .where(PickupIncident.status == "OPEN")
            .order_by(PickupIncident.opened_at, PickupIncident.incident_id)
            .limit(limit)
        )
    )
