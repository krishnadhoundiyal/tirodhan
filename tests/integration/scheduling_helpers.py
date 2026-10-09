"""Explicit synthetic policy for domain regression tests; never a runtime default."""

from datetime import date, datetime, timedelta
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from tirodhan.modules.collection_requests.scheduling import SERVICE_TIMEZONE, SlotWindow


class TestSlotAvailability:
    __test__ = False

    async def service_dates(
        self, session: AsyncSession, *, cell_id: str, now: datetime
    ) -> tuple[date, ...]:
        return tuple(
            (now.astimezone(SERVICE_TIMEZONE) + timedelta(days=i)).date() for i in range(8)
        )

    async def availability(
        self, session: AsyncSession, *, cell_id: str, slot: SlotWindow, now: datetime
    ) -> Literal["AVAILABLE", "FULL"] | None:
        return "AVAILABLE"
