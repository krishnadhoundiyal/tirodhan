"""Regional forecast inputs and publication. No road/solver dependency."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from tirodhan.modules.collection_requests.scheduling import SlotWindow

from .admission_queries import in_ids
from .allowance_estimator import (
    Workload,
    footprint,
    free_intervals,
    pickup_supply,
    remaining_allowance,
)
from .capacity_models import SlotReservationDebit
from .orchestration_models import CapacityReservation, OperatingRegion


async def forecast(
    session: AsyncSession,
    region: OperatingRegion,
    slots: list[SlotWindow],
    *,
    now: datetime,
    generation: int,
) -> tuple[list[dict[str, Any]], datetime, list[dict[str, Any]]]:
    """One MVCC snapshot; caller fences source version before publishing."""
    from .allowance_policy import current_policy
    from .slot_projection import calendar_supply

    policy = await current_policy(session, region.region_id)
    params = {"region": region.region_id, "start": slots[0].start, "end": slots[-1].end, "now": now}
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
          AND ur.role_code='RIDER' AND ur.revoked_at IS NULL)
        AND s.starts_at<:end AND
          (s.starts_at>:now OR (a.availability_intent='AVAILABLE' AND
            EXISTS(SELECT 1 FROM routing_shift_confirmation c WHERE c.shift_id=s.shift_id)))
    """),
                params,
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
                params,
            )
        )
        .mappings()
        .all()
    )
    excluded: dict[UUID, list[tuple[datetime, datetime]]] = defaultdict(list)
    for row in breaks:
        excluded[row["shift_id"]].append((row["starts_at"], row["ends_at"]))
    # An active task reserves only its predicted travel/service interval, not
    # every hour between shift start and an afternoon appointment.
    tasks = (
        (
            await session.execute(
                text("""
      SELECT vr.vehicle_id,p.request_id,t.kind,rs.planned_start,rs.planned_end
      FROM vehicle_route vr JOIN operating_region_vehicle rv USING(vehicle_id)
      JOIN route_revision r ON r.route_id=vr.route_id AND r.version=vr.version
      JOIN route_revision_stop rs USING(revision_id) JOIN route_task t USING(task_id)
      JOIN pickup_execution p USING(pickup_execution_id)
      WHERE rv.region_id=:region AND vr.status<>'CLOSED' AND t.state IN ('PENDING','STARTED')
        AND rs.planned_start<:end
    """),
                params,
            )
        )
        .mappings()
        .all()
    )
    represented = {row["request_id"] for row in tasks}
    discharging = {row["vehicle_id"] for row in tasks if row["kind"] == "DISCHARGE"}
    by_vehicle: dict[UUID, list[tuple[datetime, datetime]]] = defaultdict(list)
    for row in tasks:
        active_policy = policy.at(row["planned_start"]) if policy else None
        travel = (
            0
            if active_policy is None
            else (
                active_policy.facility_travel_seconds
                if row["kind"] == "DISCHARGE"
                else active_policy.local_travel_seconds
            )
        )
        start = row["planned_start"] - timedelta(seconds=travel)
        end = row["planned_end"]
        if end <= now:
            duration = max(1, int((end - start).total_seconds()))
            start, end = now, now + timedelta(seconds=duration)
        by_vehicle[row["vehicle_id"]].append((start, end))
    loaded = set(
        await session.scalars(
            text("""
      WITH candidates AS (
        SELECT cv.request_id FROM cargo_vehicle_observation cv JOIN operating_region_vehicle rv
          USING(vehicle_id) WHERE rv.region_id=:region
        UNION SELECT ct.request_id FROM cargo_transfer ct JOIN operating_region_vehicle rv
          ON rv.vehicle_id=ct.to_vehicle_id WHERE rv.region_id=:region
      ) SELECT DISTINCT coalesce((SELECT ct.to_vehicle_id FROM cargo_transfer ct
          WHERE ct.request_id=cv.request_id ORDER BY ct.sequence DESC LIMIT 1),cv.vehicle_id)
      FROM candidates JOIN cargo_vehicle_observation cv USING(request_id)
      JOIN pickup_execution p USING(request_id)
      WHERE NOT EXISTS(SELECT 1 FROM handover_event_item hi
        WHERE hi.pickup_execution_id=p.pickup_execution_id AND hi.status='VALIDATED')
    """),
            params,
        )
    )
    windows = (
        (
            await session.execute(
                text("""
      SELECT w.starts_at,w.ends_at FROM routing_receiving_window w
      JOIN operating_region r ON r.default_warehouse_id=w.receiving_point_id
      JOIN receiving_point p ON p.receiving_point_id=w.receiving_point_id
      WHERE r.region_id=:region AND p.status='ACTIVE' AND p.official_identifier IS NOT NULL
        AND w.starts_at<=:end AND w.ends_at>=:start
    """),
                params,
            )
        )
        .mappings()
        .all()
    )
    restrictions = (
        (
            await session.execute(
                text("""
      SELECT f.starts_at,f.ends_at FROM routing_facility_restriction f
      JOIN operating_region r ON r.default_warehouse_id=f.receiving_point_id
      WHERE r.region_id=:region AND f.starts_at<:end AND f.ends_at>:start
    """),
                params,
            )
        )
        .mappings()
        .all()
    )
    openings = [
        piece
        for w in windows
        for piece in free_intervals(
            w["starts_at"], w["ends_at"], [(f["starts_at"], f["ends_at"]) for f in restrictions]
        )
    ]
    # Latest customer-approved appointment supersedes the original slot for
    # capacity forecasting; the original financial/booking snapshot is untouched.
    obligations = (
        (
            await session.execute(
                text("""
      SELECT b.request_id AS identity,b.cell_id,
        coalesce(ac.slot_start,b.slot_start) AS slot_start,
        coalesce(ac.slot_end,b.slot_end) AS slot_end,
        coalesce(m.pickup_seconds,(r.policy->>'minimum_pickup_seconds')::int) AS pickup_seconds,
        coalesce(m.discharge_seconds,
          (r.policy->>'minimum_discharge_seconds')::int) AS discharge_seconds,
        false AS held,NULL::timestamptz AS expires_at
      FROM collection_request b JOIN operating_region_cell c USING(cell_id)
      JOIN operating_region r ON r.region_id=c.region_id
      LEFT JOIN routing_manifest m USING(request_id) LEFT JOIN pickup_execution p USING(request_id)
      LEFT JOIN LATERAL(SELECT slot_start,slot_end FROM appointment_consent
        WHERE request_id=b.request_id ORDER BY consented_at DESC LIMIT 1) ac ON true
      WHERE c.region_id=:region AND b.status IN ('ACCEPTED','PRE_PLANNING','PLANNED')
        AND (p.status IS NULL OR p.status<>'COLLECTED')
        AND coalesce(ac.slot_start,b.slot_start)<:end
      UNION ALL
      SELECT h.reservation_id,c.cell_id,h.slot_start,h.slot_end,
        h.pickup_seconds,h.discharge_seconds,
        true,least(h.expires_at,coalesce(b.payment_expires_at,h.expires_at))
      FROM regional_capacity_reservation h
      JOIN serviceability_context c ON c.serviceability_context_id=h.context_id
      LEFT JOIN collection_request b ON b.request_id=h.request_id
      WHERE h.region_id=:region AND h.expires_at>:now AND
        (h.request_id IS NULL OR (b.status='PENDING_PAYMENT' AND b.payment_expires_at>:now))
        AND h.slot_end>=:start AND h.slot_start<:end
    """),
                params,
            )
        )
        .mappings()
        .all()
    )
    counts = Counter((r["slot_start"], r["cell_id"]) for r in obligations)
    identities = [r["identity"] for r in obligations]
    reservations = list(
        await session.scalars(
            select(CapacityReservation).where(
                CapacityReservation.region_id == region.region_id,
                or_(
                    in_ids(CapacityReservation.reservation_id, identities),
                    in_ids(CapacityReservation.request_id, identities),
                ),
            )
        )
    )
    reservation_for = {r.reservation_id: r.reservation_id for r in reservations}
    slots_by_identity = {o["identity"]: o["slot_start"] for o in obligations}
    for r in sorted(reservations, key=lambda r: r.created_at):
        if r.request_id and slots_by_identity.get(r.request_id) == r.slot_start:
            reservation_for[r.request_id] = r.reservation_id
    preserved: dict[UUID, dict[tuple[datetime, str], int]] = defaultdict(dict)
    for debit in await session.scalars(
        select(SlotReservationDebit).where(
            in_ids(SlotReservationDebit.reservation_id, [r.reservation_id for r in reservations])
        )
    ):
        preserved[debit.reservation_id][(debit.bucket_start, debit.kind)] = debit.seconds
    seen_cells: Counter[tuple[datetime, str]] = Counter()
    totals: dict[datetime, dict[str, int]] = defaultdict(
        lambda: dict(bookings=0, holds=0, pickup_seconds=0)
    )
    minimum_pickup = int(region.policy["minimum_pickup_seconds"])
    minimum_discharge = int(region.policy["minimum_discharge_seconds"])
    free_shifts: list[dict[str, Any]] = []
    pickup_capacity = {s.start: 0 for s in slots}
    for shift in shifts:
        blocked = excluded[shift["shift_id"]] + by_vehicle[shift["vehicle_id"]]
        if shift["vehicle_id"] in loaded and shift["vehicle_id"] not in discharging:
            blocked.append((now, shift["ends_at"]))
        start = max(
            now, shift["starts_at"] + timedelta(seconds=policy.stem_seconds if policy else 0)
        )
        free = free_intervals(start, shift["ends_at"], blocked)
        free_shifts.extend(
            dict(shift_id=shift["shift_id"], starts_at=a, ends_at=b) for a, b in free
        )
        if policy:
            eligible = pickup_supply(
                slots,
                free,
                openings,
                pickup_seconds=minimum_pickup,
                discharge_seconds=minimum_discharge,
                policy=policy,
            )
            for key, value in eligible.items():
                pickup_capacity[key] += value
    supply = calendar_supply(slots, free_shifts, {})
    duty_used: dict[datetime, int] = defaultdict(int)
    pickup_used: dict[datetime, int] = defaultdict(int)
    for row in sorted(obligations, key=lambda r: str(r["identity"])):
        key = row["slot_start"]
        totals[key]["holds" if row["held"] else "bookings"] += 1
        totals[key]["pickup_seconds"] += row["pickup_seconds"]
        if row["identity"] in represented or policy is None:
            continue
        work_start = max(key, slots[0].start)
        cell_key = (key, row["cell_id"])
        clustered = (
            counts[cell_key] >= policy.cluster_threshold
            and not row["held"]
            and seen_cells[cell_key] >= policy.cluster_threshold - 1
        )
        seen_cells[cell_key] += 1
        work = Workload(
            work_start,
            work_start + timedelta(minutes=30),
            row["pickup_seconds"],
            row["discharge_seconds"],
            clustered,
        )
        charges = {(bucket, "DUTY"): amount for bucket, amount in footprint(work, policy).items()}
        charges[(work_start, "PICKUP")] = work.pickup_seconds
        for key_pair, amount in (
            preserved[reservation_for[row["identity"]]]
            if row["identity"] in reservation_for
            else {}
        ).items():
            if key_pair[0] >= slots[0].start:
                charges[key_pair] = max(charges.get(key_pair, 0), amount)
        for (bucket, kind), amount in charges.items():
            (duty_used if kind == "DUTY" else pickup_used)[bucket] += amount
    remaining = {}
    budgets = []
    for slot in slots:
        for kind, cap, used in (
            ("DUTY", supply[slot.start], duty_used[slot.start]),
            ("PICKUP", pickup_capacity[slot.start], pickup_used[slot.start]),
        ):
            remaining[(slot.start, kind)] = max(0, cap - used)
            budgets.append(
                dict(
                    region_id=region.region_id,
                    bucket_start=slot.start,
                    kind=kind,
                    generation=generation,
                    capacity_seconds=cap,
                    used_seconds=used,
                )
            )
    expiry = min(
        [now + timedelta(seconds=60)]
        + [r["expires_at"] for r in obligations if r["expires_at"]]
        + [s["starts_at"] for s in shifts if s["starts_at"] > now]
    )
    rows = []
    for slot in slots:
        allowance = (
            remaining_allowance(
                Workload(slot.start, slot.end, minimum_pickup, minimum_discharge), policy, remaining
            )
            if policy
            else 0
        )
        rows.append(
            dict(
                region_id=region.region_id,
                slot_start=slot.start,
                slot_end=slot.end,
                generation=generation,
                policy_version=policy.version if policy else 0,
                remaining_bookings=allowance,
                status="UNKNOWN"
                if policy is None
                else "FULL"
                if allowance == 0
                else "LIMITED"
                if allowance <= policy.limited_bookings
                else "AVAILABLE",
                vehicle_seconds=supply[slot.start],
                busy_seconds=0,
                **totals[slot.start],
            )
        )
    return rows, expiry, budgets
