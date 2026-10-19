"""Coalesced regional read model. Availability never grants a reservation or authority."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.modules.collection_requests.scheduling import (
    SERVICE_TIMEZONE,
    SlotWindow,
    operating_grid,
)

from .admission_models import AdmissionSourceHead
from .capacity_models import RegionalSlotProjection, RegionalSlotRefresh


def available_seconds(
    start: datetime,
    end: datetime,
    shift_start: datetime,
    shift_end: datetime,
    breaks: list[tuple[datetime, datetime]],
) -> int:
    """Interval union avoids double subtraction of overlapping calendar exclusions."""
    left, right = max(start, shift_start), min(end, shift_end)
    if right <= left:
        return 0
    blocked = sorted((max(left, a), min(right, b)) for a, b in breaks if a < right and b > left)
    seconds, cursor = 0.0, left
    for a, b in blocked:
        seconds += max(0, (a - cursor).total_seconds())
        cursor = max(cursor, b)
    return int(seconds + max(0, (right - cursor).total_seconds()))


def calendar_supply(
    slots: list[SlotWindow],
    shifts: Sequence[Any],
    breaks: dict[UUID, list[tuple[datetime, datetime]]],
) -> dict[datetime, int]:
    """Sweep free-interval endpoints once: O((V+B+S) log(V+B+S)).

    Integrating simultaneous eligible vehicles gives vehicle-seconds per slot;
    it is an availability bound, never a road/cargo feasibility certificate.
    """
    events: dict[datetime, int] = {}
    for shift in shifts:
        start, end = max(slots[0].start, shift["starts_at"]), min(slots[-1].end, shift["ends_at"])
        if start >= end:
            continue
        cursor = start
        for a, b in sorted(breaks.get(shift["shift_id"], [])):
            a, b = max(start, a), min(end, b)
            if a >= b:
                continue
            if a > cursor:
                events[cursor] = events.get(cursor, 0) + 1
                events[a] = events.get(a, 0) - 1
            cursor = max(cursor, b)
        if cursor < end:
            events[cursor] = events.get(cursor, 0) + 1
            events[end] = events.get(end, 0) - 1
    boundaries = {t for slot in slots for t in (slot.start, slot.end)}
    before, running, integral = slots[0].start, 0, 0.0
    area: dict[datetime, float] = {}
    for at in sorted(set(events) | boundaries):
        integral += running * (at - before).total_seconds()
        area[at] = integral
        running += events.get(at, 0)
        before = at
    return {slot.start: int(area[slot.end] - area[slot.start]) for slot in slots}


async def refresh_slots(
    factory: async_sessionmaker[AsyncSession],
    region_id: UUID,
    *,
    now: datetime,
    days: int = 7,
    freshness_seconds: int = 60,
) -> int:
    from sqlalchemy import func

    from .allowance_projection import forecast
    from .capacity_models import RegionalCapacityBudget
    from .orchestration_models import OperatingRegion

    if not 1 <= days <= 7 or not 1 <= freshness_seconds <= 3600:
        raise ValueError("INVALID_SLOT_REFRESH_CONFIGURATION")
    today = now.astimezone(SERVICE_TIMEZONE).date()
    slots = [
        slot
        for d in range(days)
        for slot in operating_grid(today + timedelta(days=d))
        if slot.end > now
    ]
    if not slots:
        return 0
    async with factory() as session:
        await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        head = await session.get(AdmissionSourceHead, region_id)
        region = await session.get(OperatingRegion, region_id)
        if head is None or region is None:
            return 0
        version = head.version
        generation = 1 + (
            await session.scalar(
                select(func.max(RegionalSlotProjection.generation)).where(
                    RegionalSlotProjection.region_id == region_id
                )
            )
            or 0
        )
        rows, expiry, budgets = await forecast(
            session, region, slots, now=now, generation=generation
        )
        expiry = min(expiry, now + timedelta(seconds=freshness_seconds))
        await session.refresh(head, with_for_update=True)
        if head.version != version:
            raise ValueError("SLOT_PROJECTION_STALE_RETRY")
        # Publication follows head -> budget order, matching customer holds.
        statement_budget = insert(RegionalCapacityBudget).values(budgets)
        await session.execute(
            statement_budget.on_conflict_do_update(
                index_elements=[
                    RegionalCapacityBudget.region_id,
                    RegionalCapacityBudget.bucket_start,
                    RegionalCapacityBudget.kind,
                ],
                set_={
                    key: getattr(statement_budget.excluded, key)
                    for key in ("generation", "capacity_seconds", "used_seconds")
                },
            )
        )
        for row in rows:
            row.update(source_version=version, computed_at=now, valid_until=expiry)
        statement = insert(RegionalSlotProjection).values(rows)
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=[
                    RegionalSlotProjection.region_id,
                    RegionalSlotProjection.slot_start,
                ],
                set_={
                    key: getattr(statement.excluded, key)
                    for key in rows[0]
                    if key not in ("region_id", "slot_start")
                },
            )
        )
        await session.execute(
            delete(RegionalSlotProjection).where(
                RegionalSlotProjection.region_id == region_id,
                RegionalSlotProjection.slot_end <= now,
            )
        )
        await session.execute(
            insert(RegionalSlotRefresh)
            .values(region_id=region_id, source_version=version, due_at=expiry)
            .on_conflict_do_update(
                index_elements=[RegionalSlotRefresh.region_id],
                set_={"source_version": version, "due_at": expiry},
            )
        )
        await session.commit()
    return len(rows)


async def due_regions(session: AsyncSession, now: datetime, *, limit: int = 16) -> list[UUID]:
    return list(
        await session.scalars(
            select(RegionalSlotRefresh.region_id)
            .where(RegionalSlotRefresh.due_at <= now)
            .order_by(RegionalSlotRefresh.due_at)
            .limit(limit)
        )
    )


async def read_slots(
    session: AsyncSession,
    region_id: UUID,
    *,
    now: datetime,
) -> list[dict[str, object]]:
    rows = (
        await session.execute(
            select(RegionalSlotProjection, AdmissionSourceHead.version)
            .join(
                AdmissionSourceHead,
                AdmissionSourceHead.region_id == RegionalSlotProjection.region_id,
            )
            .where(
                RegionalSlotProjection.region_id == region_id,
                RegionalSlotProjection.slot_start > now,
            )
            .order_by(RegionalSlotProjection.slot_start)
        )
    ).all()
    return [
        dict(
            slot_start=r.slot_start,
            slot_end=r.slot_end,
            status=r.status if r.source_version == version and r.valid_until > now else "UNKNOWN",
            remaining_bookings=r.remaining_bookings
            if r.source_version == version and r.valid_until > now
            else 0,
            policy_version=r.policy_version,
            generation=r.generation,
            valid_until=r.valid_until,
            admission_basis="PROJECTED_ALLOWANCE",
            source_version=r.source_version,
            computed_at=r.computed_at,
            capacity_certified=False,
        )
        for r, version in rows
    ]
