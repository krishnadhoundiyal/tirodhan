"""One scheduling grid and eligibility calculation for offering and booking.

Operational policy is deliberately a port, with no inferred production availability.
Implementations run inside the transaction and share the planning work-unit lock for
eligibility changes. They must use PostgreSQL truth, not external I/O or rider guesses.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal, Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tirodhan.modules.planning.locking import acquire_work_unit_advisory_lock
from tirodhan.modules.planning.models import PlanningBatch
from tirodhan.modules.planning.policy import as_utc_instant, planning_cutoff_reached

SERVICE_TIMEZONE = ZoneInfo("Asia/Kolkata")
SLOT_DURATION = timedelta(minutes=30)


class SchedulingUnavailableError(RuntimeError):
    pass


class SlotConflictError(ValueError):
    def __init__(self, code: str = "SLOT_UNAVAILABLE") -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class SlotWindow:
    start: datetime
    end: datetime

    @property
    def slot_id(self) -> str:
        return "kolkata-" + self.start.astimezone(SERVICE_TIMEZONE).strftime("%Y%m%dT%H%M")

    @property
    def label(self) -> str:
        start = self.start.astimezone(SERVICE_TIMEZONE)
        end = self.end.astimezone(SERVICE_TIMEZONE)
        return f"{start:%d %b} · {start:%H:%M}–{end:%H:%M}"


class SlotAvailabilityPort(Protocol):
    async def service_dates(
        self, session: AsyncSession, *, cell_id: str, now: datetime
    ) -> tuple[date, ...]: ...

    async def availability(
        self, session: AsyncSession, *, cell_id: str, slot: SlotWindow, now: datetime
    ) -> Literal["AVAILABLE", "FULL"] | None: ...


class UnconfiguredSlotAvailability:
    async def service_dates(
        self, session: AsyncSession, *, cell_id: str, now: datetime
    ) -> tuple[date, ...]:
        raise SchedulingUnavailableError("operational slot availability policy is not approved")

    async def availability(
        self, session: AsyncSession, *, cell_id: str, slot: SlotWindow, now: datetime
    ) -> Literal["AVAILABLE", "FULL"] | None:
        raise SchedulingUnavailableError("operational slot availability policy is not approved")


def daily_grid(day: date) -> tuple[SlotWindow, ...]:
    midnight = datetime.combine(day, time(), SERVICE_TIMEZONE)
    return tuple(
        SlotWindow(
            (midnight + number * SLOT_DURATION).astimezone(timezone.utc),
            (midnight + (number + 1) * SLOT_DURATION).astimezone(timezone.utc),
        )
        for number in range(48)
    )


def validate_slot(start: datetime, end: datetime, *, now: datetime) -> SlotWindow:
    try:
        start, end, now = map(as_utc_instant, (start, end, now))
    except ValueError as error:
        raise SlotConflictError() from error
    local = start.astimezone(SERVICE_TIMEZONE)
    if (
        end - start != SLOT_DURATION
        or local.minute not in (0, 30)
        or local.second
        or local.microsecond
        or start <= now
    ):
        raise SlotConflictError()
    return SlotWindow(start, end)


async def evaluate_slot(
    session: AsyncSession,
    policy: SlotAvailabilityPort,
    *,
    cell_id: str,
    slot: SlotWindow,
    now: datetime,
    lead_time_minutes: int | None,
    booking: bool = False,
    frozen: bool | None = None,
) -> Literal["AVAILABLE", "FULL"] | None:
    validate_slot(slot.start, slot.end, now=now)
    if booking:
        await acquire_work_unit_advisory_lock(
            session, cell_id=cell_id, slot_start=slot.start, slot_end=slot.end
        )
        # Time must be evaluated again after a potentially long lock wait.
        from tirodhan.db.values import utc_now

        now = max(now, utc_now())
    if planning_cutoff_reached(slot.start, lead_time_minutes, now=now):
        raise SlotConflictError("PLANNING_CUTOFF_REACHED")
    if frozen is None or booking:
        frozen = (
            await session.scalar(
                select(PlanningBatch.planning_batch_id).where(
                    PlanningBatch.cell_id == cell_id,
                    PlanningBatch.slot_start == slot.start,
                    PlanningBatch.slot_end == slot.end,
                )
            )
            is not None
        )
    if frozen:
        raise SlotConflictError("PLANNING_STARTED")
    if booking:
        dates = await policy.service_dates(session, cell_id=cell_id, now=now)
        if slot.start.astimezone(SERVICE_TIMEZONE).date() not in dates:
            raise SlotConflictError()
    result = await policy.availability(session, cell_id=cell_id, slot=slot, now=now)
    if result not in (None, "AVAILABLE", "FULL"):
        raise SchedulingUnavailableError("invalid availability policy result")
    if booking and result != "AVAILABLE":
        raise SlotConflictError()
    return result
