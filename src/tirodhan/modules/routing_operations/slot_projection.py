"""Coalesced regional read model. Availability never grants a reservation or authority."""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence
from datetime import datetime, timedelta
from itertools import accumulate
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select, text
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
        if head is None:
            return 0
        version = head.version
        # Aggregate on the database, not a complete booking/route/matrix snapshot.
        pressure = (
            (
                await session.execute(
                    text("""
          SELECT slot_start, count(*) FILTER(WHERE NOT held) AS bookings,
            count(*) FILTER(WHERE held) AS holds, sum(service) AS service FROM (
            SELECT b.slot_start, false AS held, coalesce(m.pickup_seconds,
              (r.policy->>'minimum_pickup_seconds')::int) AS service
            FROM collection_request b JOIN operating_region_cell c USING(cell_id)
            JOIN operating_region r ON r.region_id=c.region_id
            LEFT JOIN routing_manifest m USING(request_id)
            LEFT JOIN pickup_execution p USING(request_id)
            WHERE c.region_id=:region AND b.status IN ('ACCEPTED','PRE_PLANNING','PLANNED')
              AND (p.status IS NULL OR p.status<>'COLLECTED')
              AND b.slot_start>=:start AND b.slot_start<:end
            UNION ALL
            SELECT h.slot_start, true, h.pickup_seconds
            FROM regional_capacity_reservation h
            LEFT JOIN collection_request b ON b.request_id=h.request_id
            WHERE h.region_id=:region AND h.expires_at>:now AND
              (h.request_id IS NULL OR (b.status='PENDING_PAYMENT' AND b.payment_expires_at>:now))
              AND h.slot_start>=:start AND h.slot_start<:end
          ) p GROUP BY slot_start
        """),
                    {
                        "region": region_id,
                        "start": slots[0].start,
                        "end": slots[-1].end,
                        "now": now,
                    },
                )
            )
            .mappings()
            .all()
        )
        by_start = {row["slot_start"]: row for row in pressure}
        shifts = (
            (
                await session.execute(
                    text("""
          SELECT s.shift_id,s.vehicle_id,s.starts_at,
            greatest(s.ends_at,coalesce((SELECT max(e.ends_at) FROM operating_shift_extension e
              WHERE e.shift_id=s.shift_id),s.ends_at)) AS ends_at
          FROM operating_shift s JOIN operating_region_vehicle rv USING(vehicle_id)
          JOIN operating_vehicle v USING(vehicle_id) JOIN rider_profile p USING(rider_id)
          JOIN app_user u ON u.user_id=s.rider_id
          LEFT JOIN rider_availability a USING(rider_id)
          WHERE rv.region_id=:region AND s.closed_at IS NULL AND s.resource_kind='COMMITTED'
            AND v.blocked_reason IS NULL AND p.status='ACTIVE' AND u.status='ACTIVE'
            AND EXISTS(SELECT 1 FROM user_role ur WHERE ur.user_id=s.rider_id
              AND role_code='RIDER' AND ur.revoked_at IS NULL)
            AND s.starts_at<=:end AND
            (s.starts_at>:now OR a.availability_intent='AVAILABLE')
        """),
                    {"region": region_id, "end": slots[-1].end, "now": now},
                )
            )
            .mappings()
            .all()
        )
        breaks = (
            (
                await session.execute(
                    text("""
          SELECT b.shift_id,b.starts_at,b.ends_at FROM operating_break b
          JOIN operating_shift s USING(shift_id) JOIN operating_region_vehicle rv USING(vehicle_id)
          WHERE rv.region_id=:region AND b.starts_at<:end AND b.ends_at>:start
        """),
                    {"region": region_id, "start": slots[0].start, "end": slots[-1].end},
                )
            )
            .mappings()
            .all()
        )
        by_shift: dict[UUID, list[tuple[datetime, datetime]]] = {}
        for row in breaks:
            by_shift.setdefault(row["shift_id"], []).append((row["starts_at"], row["ends_at"]))
        rows = []
        # Current execution spans and unhanded cargo are workload signals, not
        # independently spendable capacity. No household coordinates are read.
        occupied = (
            (
                await session.execute(
                    text("""
          SELECT vr.vehicle_id, min(rs.planned_start) AS starts_at, max(rs.planned_end) AS ends_at
          FROM vehicle_route vr JOIN operating_region_vehicle rv USING(vehicle_id)
          JOIN route_revision r ON r.route_id=vr.route_id AND r.version=vr.version
          JOIN route_revision_stop rs USING(revision_id) JOIN route_task t USING(task_id)
          WHERE rv.region_id=:region AND vr.status<>'CLOSED' AND t.state IN ('PENDING','STARTED')
          GROUP BY vr.vehicle_id
        """),
                    {"region": region_id},
                )
            )
            .mappings()
            .all()
        )
        loaded = set(
            await session.scalars(
                text("""
          WITH candidate AS (
            SELECT cv.request_id FROM cargo_vehicle_observation cv
              JOIN operating_region_vehicle rv USING(vehicle_id) WHERE rv.region_id=:region
            UNION SELECT ct.request_id FROM cargo_transfer ct JOIN operating_region_vehicle rv
              ON rv.vehicle_id=ct.to_vehicle_id WHERE rv.region_id=:region
          ) SELECT DISTINCT coalesce((SELECT ct.to_vehicle_id FROM cargo_transfer ct
            WHERE ct.request_id=cv.request_id ORDER BY ct.sequence DESC LIMIT 1),cv.vehicle_id)
          FROM candidate JOIN cargo_vehicle_observation cv USING(request_id)
          JOIN pickup_execution p USING(request_id)
          WHERE NOT EXISTS(SELECT 1 FROM handover_event_item hi
            WHERE hi.pickup_execution_id=p.pickup_execution_id AND hi.status='VALIDATED')
          AND EXISTS(SELECT 1 FROM operating_region_vehicle rv WHERE rv.region_id=:region
            AND rv.vehicle_id=coalesce((SELECT ct.to_vehicle_id FROM cargo_transfer ct
              WHERE ct.request_id=cv.request_id ORDER BY ct.sequence DESC LIMIT 1),cv.vehicle_id))
        """),
                {"region": region_id},
            )
        )
        spans = {r["vehicle_id"]: r for r in occupied}
        busy = []
        for shift in shifts:
            span = spans.get(shift["vehicle_id"])
            if span or shift["vehicle_id"] in loaded:
                busy.append(
                    dict(
                        shift_id=shift["shift_id"],
                        starts_at=max(now, shift["starts_at"]),
                        ends_at=min(shift["ends_at"], span["ends_at"])
                        if span and span["ends_at"] > now
                        else shift["ends_at"],
                    )
                )
        busy_by_start = calendar_supply(slots, busy, by_shift)
        supply_by_start = calendar_supply(slots, shifts, by_shift)
        duty_ranges = sorted((s["starts_at"], s["ends_at"]) for s in shifts)
        duty_starts = [start for start, _ in duty_ranges]
        latest_end = list(accumulate((end for _, end in duty_ranges), max))
        for slot in slots:
            supply = supply_by_start[slot.start]
            boundary = bisect_right(duty_starts, slot.end)
            boundary_candidate = boundary > 0 and latest_end[boundary - 1] > slot.start
            p = by_start.get(slot.start)
            demand = int(p["service"] or 0) if p else 0
            rows.append(
                dict(
                    region_id=region_id,
                    slot_start=slot.start,
                    slot_end=slot.end,
                    source_version=version,
                    computed_at=now,
                    valid_until=now + timedelta(seconds=freshness_seconds),
                    status="FULL"
                    if not supply and not boundary_candidate
                    else "LIMITED"
                    if max(demand, busy_by_start[slot.start]) >= supply
                    else "AVAILABLE",
                    bookings=int(p["bookings"]) if p else 0,
                    holds=int(p["holds"]) if p else 0,
                    vehicle_seconds=supply,
                    pickup_seconds=demand,
                    busy_seconds=busy_by_start[slot.start],
                )
            )
        # Domain writers use the same head; a newer source cannot be overwritten.
        await session.refresh(head, with_for_update=True)
        if head.version != version:
            raise ValueError("SLOT_PROJECTION_STALE_RETRY")
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
            .values(
                region_id=region_id,
                source_version=version,
                due_at=now + timedelta(seconds=freshness_seconds),
            )
            .on_conflict_do_update(
                index_elements=[RegionalSlotRefresh.region_id],
                set_={
                    "source_version": version,
                    "due_at": now + timedelta(seconds=freshness_seconds),
                },
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
            source_version=r.source_version,
            computed_at=r.computed_at,
            capacity_certified=False,
        )
        for r, version in rows
    ]
