"""Finite regional owner: bounded planning work, parallel proposals, atomic authority.

Production entry points remain default-disabled. No native or road I/O is performed
inside the publication transaction. A wake is a hint; per-booking outcomes are history.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from pydantic import Field
from sqlalchemy import any_, case, cast, func, select
from sqlalchemy.dialects.postgresql import ARRAY, insert
from sqlalchemy.dialects.postgresql import UUID as SQLUUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.planning.routing.contracts import (
    OptimizerEngine,
    ProposedRoute,
    RouteProposal,
    RoutingProblem,
    SelectionEngine,
)
from tirodhan.modules.planning.routing.execution import RoutingAdapterError
from tirodhan.modules.planning.routing.validation import RoutingValidationError, validate_proposal

from .authority import ActivationCommand, RouteAuthorityError, _activate
from .commands import OperationalInputError, require_live_role
from .economics import measure
from .fleet_models import FleetControl, FleetCycle, FleetException
from .models import RoutingShadowRun
from .orchestration_models import FleetWakeup, OperatingRegion
from .parallel import (
    VERSION,
    Candidate,
    FacilityLimit,
    Frozen,
    Selection,
    WorkerResult,
    WorkerTask,
    candidate,
    conflict_alternatives,
    consolidate,
    group_problem,
    local_workers,
    selected_routes,
    slice_problem,
)
from .parallel_models import PlanningOutcome
from .rolling import FleetPolicy, incumbent_orders, ready_snapshot
from .service import PROBLEM_CODEC, PROPOSAL_CODEC
from .snapshot import (
    MatrixSource,
    OperationalSnapshot,
    build_problem,
    capture_snapshot,
    source_digest,
)


def neighbourhoods(
    problem: Any, size: int, priorities: dict[UUID, int], incumbent: dict[UUID, tuple[UUID, ...]]
) -> dict[UUID, tuple[set[UUID], set[UUID]]]:
    """Trajectory-aware necessary reachability and balanced seeds; never assignments."""
    from tirodhan.modules.planning.routing.preparation import prepare

    prepared = prepare(problem)
    nodes = sorted(
        {node for matrix in problem.matrices for node in matrix.node_ids}, key=lambda node: node.int
    )
    vehicles = {v.vehicle_id: v for v in problem.vehicles}
    starts = {
        prepared.vehicle_ids[v["id"] - 1]: (nodes[v["start"]], v["available"])
        for v in prepared.payload["vehicles"]
    }
    matrices = {m.matrix_id: m for m in problem.matrices}
    indices = {
        identity: {node: i for i, node in enumerate(matrix.node_ids)}
        for identity, matrix in matrices.items()
    }
    by_stop = {stop.stop_id: stop for stop in problem.stops}
    lots = {lot.lot_id: lot for lot in problem.lots}
    ranked: dict[UUID, list[tuple[int, UUID]]] = {v: [] for v in vehicles}
    primary: dict[UUID, set[UUID]] = {v: set() for v in vehicles}
    pickups = sorted(
        (s for s in problem.stops if s.kind.value == "PICKUP" and not s.required),
        key=lambda s: (-priorities.get(s.lot_id, 0), s.service_start_window.end, str(s.lot_id)),
    )
    for stop in pickups:
        eligible = []
        for vehicle_id in stop.eligible_vehicle_ids:
            v = vehicles[vehicle_id]
            matrix = matrices[v.matrix_id]
            index = indices[v.matrix_id]
            node, available = starts[vehicle_id]
            seconds = matrix.travel_seconds[index[node]][index[stop.node_id]]
            lot = lots[stop.lot_id]
            if (
                seconds is None
                or available + seconds > stop.service_start_window.end
                or any(
                    amount > capacity
                    for amount, capacity in zip(lot.unit_load, v.capacity, strict=True)
                )
            ):
                continue
            # Prefer proximity to a feasible continuation trajectory, not only GPS.
            trajectory = [node] + [
                by_stop[identity].node_id
                for identity in incumbent.get(vehicle_id, ())
                if identity in by_stop
            ]
            detour = min(
                (
                    matrix.travel_seconds[index[n]][index[stop.node_id]]
                    for n in trajectory
                    if matrix.travel_seconds[index[n]][index[stop.node_id]] is not None
                ),
                default=seconds,
            )
            eligible.append((len(primary[vehicle_id]), detour, str(vehicle_id), vehicle_id))
            ranked[vehicle_id].append((seconds, stop.lot_id))
        if eligible:
            primary[min(eligible)[-1]].add(stop.lot_id)
    result = {}
    for vehicle_id in vehicles:
        core = set(
            sorted(primary[vehicle_id], key=lambda lot: (-priorities.get(lot, 0), str(lot)))[:size]
        )
        border = [lot for _, lot in sorted(ranked[vehicle_id]) if lot not in core]
        expanded = core | set(border[: min(2, max(0, size - len(core)))])
        result[vehicle_id] = (core, expanded)
    return result


class RegionalPolicy(Frozen):
    version: str = VERSION
    fleet: FleetPolicy
    processes: int = Field(ge=1, le=16)
    max_admitted: int = Field(ge=1, le=400)
    neighbourhood_size: int = Field(ge=1, le=128)
    capacity_multiplier: float = Field(gt=0, le=20, allow_inf_nan=False)
    max_discovery: int = Field(ge=1, le=100000)
    max_adjustments: int = Field(ge=1, le=20000)
    selection_seconds: int = Field(ge=1, le=120)
    starvation_seconds: int = Field(ge=1, le=604800)
    facility_limits: tuple[FacilityLimit, ...] = ()


def scope(region: UUID) -> str:
    return f"REGION:{region}"


async def configure_region(
    factory: async_sessionmaker[AsyncSession],
    *,
    region_id: UUID,
    manager_id: UUID,
    policy: RegionalPolicy,
    now: datetime,
) -> None:
    """Insert-once configuration identity; changed configuration requires a new version."""
    if not policy.fleet.regional:
        raise OperationalInputError("REGIONAL_POLICY_REQUIRED")
    async with factory() as session, session.begin():
        await require_live_role(session, manager_id, "MANAGER")
        from tirodhan.modules.business_admin.history import attribute_change

        await attribute_change(session, manager_id, region_id, "REGIONAL_CONFIGURATION")
        if await session.get(OperatingRegion, region_id) is None:
            raise OperationalInputError("REGION_REQUIRED")
        from tirodhan.modules.receiving_points.models import ReceivingPoint

        from .orchestration_models import RegionReceivingPoint

        for limit in sorted(
            policy.facility_limits, key=lambda value: str(value.receiving_point_id)
        ):
            point = await session.scalar(
                select(ReceivingPoint)
                .where(ReceivingPoint.receiving_point_id == limit.receiving_point_id)
                .with_for_update()
            )
            if (
                point is None
                or point.status != "ACTIVE"
                or await session.get(RegionReceivingPoint, (region_id, limit.receiving_point_id))
                is None
            ):
                raise OperationalInputError("AUTHORIZED_FACILITY_REQUIRED")
            if point.unloading_capacity not in (None, limit.concurrent_unloads):
                raise OperationalInputError("FACILITY_CAPACITY_CONFIGURATION_CONFLICT")
            if point.unloading_capacity is None:
                point.unloading_capacity = limit.concurrent_unloads
                point.version += 1
        payload = policy.model_dump(mode="json")
        created = await session.scalar(
            insert(FleetControl)
            .values(
                scope=scope(region_id),
                region_id=region_id,
                manager_id=manager_id,
                version=1,
                configuration=payload,
                next_due_at=now,
            )
            .on_conflict_do_nothing(index_elements=["scope"])
            .returning(FleetControl.scope)
        )
        existing = await session.scalar(
            select(FleetControl).where(FleetControl.scope == scope(region_id)).with_for_update()
        )
        assert existing is not None
        if existing.manager_id != manager_id or existing.configuration != payload:
            raise OperationalInputError("REGIONAL_CONFIGURATION_CONFLICT")
        if created is not None:
            await regional_wake(session, region_id, now)


async def regional_wake(
    session: AsyncSession, region_id: UUID, now: datetime, *, urgent: bool = True
) -> None:
    control = await session.get(FleetControl, scope(region_id))
    if control is None:
        return
    delay = 0 if urgent else min(control.configuration["fleet"]["minimum_interval_seconds"], 60)
    due = now + timedelta(seconds=delay)
    await session.execute(
        insert(FleetWakeup)
        .values(
            scope=scope(region_id),
            generation=1,
            acknowledged=0,
            due_at=due,
            reason="REGIONAL_INPUT_CHANGED",
        )
        .on_conflict_do_update(
            index_elements=["scope"],
            set_={
                "generation": FleetWakeup.generation + 1,
                "due_at": case(
                    (FleetWakeup.generation <= FleetWakeup.acknowledged, due),
                    else_=func.least(FleetWakeup.due_at, due),
                ),
                "reason": "REGIONAL_INPUT_CHANGED",
            },
        )
    )


def distance(a: dict[str, Any], b: dict[str, Any]) -> float:
    # Candidate ordering only, never road travel or a proof of unreachable work.
    return float(
        (
            (a["latitude"] - b["latitude"]) ** 2
            + ((a["longitude"] - b["longitude"]) * math.cos(math.radians(a["latitude"]))) ** 2
        )
        ** 0.5
    )


def required(request: dict[str, Any]) -> bool:
    return bool(
        request.get("hold")
        or request["owner_id"] is not None
        or request["pickup_status"] == "COLLECTED"
    )


def admit(
    snapshot: OperationalSnapshot,
    policy: RegionalPolicy,
    now: datetime,
    first_wait: dict[UUID, datetime],
) -> tuple[set[UUID], dict[UUID, int]]:
    requests, vehicles = snapshot.sources["requests"], snapshot.sources["vehicles"]
    must = {r["request_id"] for r in requests if required(r)}
    priorities = {}
    for r in requests:
        if r.get("hold"):
            continue
        urgent = now >= r["slot_start"] - timedelta(seconds=policy.fleet.planning_lead_seconds)
        aged = (
            now - first_wait.get(r["request_id"], now)
        ).total_seconds() >= policy.starvation_seconds
        priorities[r["request_id"]] = (
            3 if aged else 2 if urgent and r.get("retry_eligible_at") else 1 if urgent else 0
        )
    options = [r for r in requests if not required(r)]

    def eligible_screen(r: dict[str, Any], v: dict[str, Any]) -> bool:
        return bool(
            r["mass_g"] <= v["mass_capacity_g"]
            and r["volume_ml"] <= v["volume_capacity_ml"]
            and r["slot_end"] >= max(now, v["starts_at"])
            and r["slot_start"] < v["ends_at"]
            and (
                v["resource_kind"] == "COMMITTED"
                or r["request_id"] in v.get("accepted_requests", ())
            )
        )

    opportunities: float = 0
    for v in vehicles:
        plausible = [r for r in options if eligible_screen(r, v)]
        if not plausible:
            continue
        end = min(
            v["ends_at"],
            now + timedelta(seconds=policy.fleet.horizon_seconds),
            max(r["slot_end"] for r in plausible),
        )
        break_seconds = sum(
            max(0, (min(end, b) - max(now, a)).total_seconds()) for a, b in v.get("breaks", ())
        )
        speed = policy.fleet.telemetry.max_speed_m_per_second
        points = {point["receiving_point_id"]: point for point in snapshot.sources["points"]}
        # A geographic travel estimate bounds computation only; it is not used as road
        # feasibility or a customer capacity certificate. Actual matrices follow below.
        travel_estimate = min(
            (distance(v, r) + distance(r, points[r["receiving_point_id"]])) * 111320 / speed
            for r in plausible
            if r["receiving_point_id"] in points
        )
        service = min(
            (max(1, r["pickup_seconds"] + r["discharge_seconds"]) for r in plausible), default=1
        )
        committed_seconds = sum(
            (0 if r["pickup_status"] == "COLLECTED" else r["pickup_seconds"])
            + r["discharge_seconds"]
            for r in requests
            if r["owner_id"] == v["rider_id"]
        )
        # Optimistic service opportunities, deliberately not an admission certificate.
        opportunities += max(
            0,
            int((end - now).total_seconds() - committed_seconds - break_seconds),
        ) // max(1, service + travel_estimate)
    budget = min(policy.max_admitted, max(0, math.ceil(opportunities * policy.capacity_multiplier)))
    if len(must) > policy.max_admitted:
        raise OperationalInputError("MANDATORY_WORK_EXCEEDS_WORKING_SET")
    ranked = sorted(
        options,
        key=lambda r: (
            -priorities.get(r["request_id"], 0),
            first_wait.get(r["request_id"], r["slot_start"]),
            r["slot_end"],
            str(r["request_id"]),
        ),
    )
    # Round-robin reachable neighbourhoods retain geographic/rider alternatives at each
    # priority tier, rather than allowing one dense seed to consume the entire budget.
    admitted = set(must)
    for tier in (3, 2, 1, 0):
        buckets: list[list[UUID]] = [[] for _ in vehicles]
        for r in ranked:
            if priorities.get(r["request_id"]) != tier:
                continue
            eligible = [
                (distance(v, r), i) for i, v in enumerate(vehicles) if eligible_screen(r, v)
            ]
            if eligible:
                buckets[min(eligible)[1]].append(r["request_id"])
        while any(buckets) and len(admitted) - len(must) < budget - len(must):
            for bucket in buckets:
                if bucket and len(admitted) < budget:
                    admitted.add(bucket.pop(0))
    return admitted, priorities


def subset(
    snapshot: OperationalSnapshot, request_ids: set[UUID], vehicle_ids: set[UUID] | None = None
) -> OperationalSnapshot:
    sources = dict(snapshot.sources)
    sources["requests"] = [r for r in sources["requests"] if r["request_id"] in request_ids]
    if vehicle_ids is not None:
        sources["vehicles"] = [v for v in sources["vehicles"] if v["vehicle_id"] in vehicle_ids]
        sources["routes"] = [r for r in sources["routes"] if r["vehicle_id"] in vehicle_ids]
        route_ids = {r["route_id"] for r in sources["routes"]}
        sources["tasks"] = [t for t in sources["tasks"] if t["route_id"] in route_ids]
    return OperationalSnapshot(sources, snapshot.fingerprint)


async def complete_cycle(session: AsyncSession, cycle: FleetCycle, now: datetime) -> None:
    """Called inside the same activation transaction; no acknowledgement before outcomes."""
    control = await session.scalar(
        select(FleetControl).where(FleetControl.scope == cycle.scope).with_for_update()
    )
    if (
        control is None
        or control.lease_token != cycle.lease_token
        or control.lease_until is None
        or control.lease_until <= now
    ):
        raise RouteAuthorityError("REGIONAL_LEASE_LOST")
    data = cycle.measurements or {}
    region = control.region_id
    assert region is not None
    generation = data["generation"]
    rows = []
    assigned = set(data.get("assigned", ()))
    for item in data["inputs"]:
        request = item["request_id"]
        served = request in assigned
        rows.append(
            dict(
                cycle_id=cycle.cycle_id,
                request_id=UUID(request),
                region_id=region,
                generation=generation,
                outcome="EXISTING"
                if served and item["existing"]
                else "ASSIGNED"
                if served
                else "RECOVERY"
                if item.get("recovery")
                else "DEFERRED",
                reason="VALIDATED_ROUTE" if served else item["reason"],
                evaluated_at=now,
                retry_at=None if served else now + timedelta(seconds=data["retry_seconds"]),
            )
        )
    if rows:
        await session.execute(insert(PlanningOutcome), rows)
    from tirodhan.modules.reliability.primitives import append_outbox_event

    for item in data["inputs"]:
        if item["request_id"] in assigned or not (
            item.get("recovery") or item["reason"] == "WAIT_AGE_ESCALATED"
        ):
            continue
        request_id = UUID(item["request_id"])
        attention = await session.scalar(
            select(FleetException)
            .where(FleetException.request_id == request_id, FleetException.reason == item["reason"])
            .limit(1)
        )
        if attention is None:
            attention = FleetException(
                cycle_id=cycle.cycle_id,
                request_id=request_id,
                reported_by=control.manager_id,
                reason=item["reason"],
                created_at=now,
            )
            session.add(attention)
            await session.flush([attention])
            await append_outbox_event(
                session,
                event_key=f"regional-attention:{attention.exception_id}",
                aggregate_type="FleetCycle",
                aggregate_id=cycle.cycle_id,
                event_type="FleetPlanningAttentionRequired",
                payload={"exception_id": str(attention.exception_id)},
            )
    wake = await session.scalar(
        select(FleetWakeup).where(FleetWakeup.scope == cycle.scope).with_for_update()
    )
    if wake:
        wake.acknowledged = max(wake.acknowledged, generation)
    control.lease_token, control.lease_until = None, None
    control.last_evaluated_at = now
    control.next_due_at = now + timedelta(seconds=data["retry_seconds"])
    if wake and wake.generation > wake.acknowledged:
        control.next_due_at = min(control.next_due_at, wake.due_at)


def regional_ready(
    snapshot: OperationalSnapshot,
    config: Any,
    now: datetime,
) -> tuple[OperationalSnapshot, list[tuple[UUID | None, UUID | None, str]]]:
    ready, issues = ready_snapshot(snapshot, config, now)
    orphaned = {
        route["rider_id"]: route["route_id"] for route in snapshot.sources["orphaned_routes"]
    }
    if not orphaned:
        return ready, issues
    sources = dict(ready.sources)
    retained = []
    for request in ready.sources["requests"]:
        rider = (
            request["collector_id"]
            if request["pickup_status"] == "COLLECTED"
            else request["owner_id"]
        )
        if rider in orphaned:
            issues.append((request["request_id"], orphaned[rider], "SHIFT_END_REQUIRES_RECOVERY"))
        else:
            retained.append(request)
    sources["requests"] = retained
    return OperationalSnapshot(sources, ready.fingerprint), issues


async def offer_deferred(
    factory: async_sessionmaker[AsyncSession],
    requests: list[dict[str, Any]],
    covered: set[UUID],
    priorities: dict[UUID, int],
    policy: RegionalPolicy,
    now: datetime,
) -> None:
    """Bounded existing offer protocol, including when committed capacity is absent."""
    from .flexible import offer_supplementary

    remaining = sorted(
        (r for r in requests if not required(r) and r["request_id"] not in covered),
        key=lambda r: (-priorities.get(r["request_id"], 0), r["slot_end"], str(r["request_id"])),
    )
    for request in remaining[: policy.neighbourhood_size]:
        async with factory() as session, session.begin():
            await offer_supplementary(session, request, now, policy.fleet.planning_lead_seconds)


async def refresh_selection(
    factory: async_sessionmaker[AsyncSession],
    snapshot: OperationalSnapshot,
    config: Any,
    problem: RoutingProblem,
    selection: Selection,
    admitted: set[UUID],
    priorities: dict[UUID, int],
    policy: RegionalPolicy,
    providers: dict[str, MatrixSource],
    now: datetime,
) -> tuple[OperationalSnapshot, RoutingProblem, Selection, dict[UUID, tuple[UUID, ...]]]:
    """Refresh road origins and execution progress, then reselect whole routes.

    No new native solve and no post-selection splice. Newly arriving optional demand
    remains for its pending generation; mandatory obligations are never pruned.
    """
    current = await capture_snapshot(factory, config)
    live, issues = regional_ready(current, config, now)
    if any(
        route is not None
        and not any(
            row["route_id"] == route and row["status"] == "RECOVERY_REQUIRED"
            for row in snapshot.sources["routes"] + snapshot.sources["orphaned_routes"]
        )
        for _, route, _ in issues
    ):
        raise OperationalInputError("LIVE_COMMITMENT_REQUIRES_RECOVERY")
    ids = admitted | {r["request_id"] for r in live.sources["requests"] if required(r)}
    rebased = await build_problem(subset(live, ids), config, new_uuid7(), providers, now=now)
    if {(m.profile_version, m.graph_version) for m in problem.matrices} != {
        (m.profile_version, m.graph_version) for m in rebased.matrices
    }:
        raise OperationalInputError("ROAD_GRAPH_CHANGED")
    orders = incumbent_orders(live)
    pool: list[Candidate] = []
    for vehicle in rebased.vehicles:
        try:
            pool.append(
                candidate(
                    rebased,
                    ProposedRoute(vehicle.vehicle_id, orders.get(vehicle.vehicle_id, ())),
                    "INCUMBENT",
                    VERSION,
                    policy.fleet.economics,
                    orders,
                )
            )
        except RoutingValidationError:
            pass  # The selected native route may establish a new cargo obligation suffix.
    available = {s.stop_id for s in rebased.stops}
    prior = {c.candidate_id: c for c in selection.candidates}
    for identity in selection.selected_ids:
        original = prior[identity]
        if original.vehicle_id not in {v.vehicle_id for v in rebased.vehicles}:
            continue
        root = original
        while root.construction == "CONTESTED_REMOVAL":
            assert root.parent_id is not None
            root = prior[root.parent_id]
        route = ProposedRoute(
            original.vehicle_id, tuple(stop for stop in original.stop_ids if stop in available)
        )
        try:
            refreshed = candidate(
                rebased,
                route,
                root.construction,
                root.engine_version,
                policy.fleet.economics,
                orders,
                original.candidate_id,
            )
        except RoutingValidationError:
            continue
        pool.append(
            refreshed.model_copy(
                update={
                    "rebase_source_id": problem.problem_id,
                    "rebase_snapshot_id": problem.snapshot_id,
                }
            )
        )
    selected = await asyncio.to_thread(
        consolidate,
        pool,
        {v.vehicle_id for v in rebased.vehicles},
        {s.lot_id for s in rebased.stops if s.required},
        priorities,
        policy_version=policy.version,
        seconds=policy.selection_seconds,
        facility_limits=tuple(
            FacilityLimit(
                receiving_point_id=point["receiving_point_id"],
                concurrent_unloads=point["unloading_capacity"],
            )
            for point in current.sources["points"]
            if point.get("unloading_capacity") is not None
        ),
        external_visits=tuple(
            (
                visit["receiving_point_id"],
                int((visit["planned_start"] - rebased.epoch).total_seconds()),
                int((visit["planned_end"] - rebased.epoch).total_seconds()),
            )
            for visit in current.sources.get("external_facility_visits", [])
        ),
    )
    selected_routes(selected)  # No incumbent/native repair combination is a controlled failure.
    return current, rebased, selected, orders


async def run_region(
    factory: async_sessionmaker[AsyncSession],
    *,
    region_id: UUID,
    providers: dict[str, MatrixSource],
    clock: Callable[[], datetime] = utc_now,
    workers: Callable[[list[WorkerTask], int], Awaitable[list[WorkerResult]]] = local_workers,
    observations: dict[str, float] | None = None,
) -> FleetCycle | None:
    started = time.monotonic()
    now = clock()
    from .orchestration import prepare_due

    await prepare_due(factory, now, control_scope=scope(region_id))
    async with factory() as session, session.begin():
        control = await session.scalar(
            select(FleetControl).where(FleetControl.scope == scope(region_id)).with_for_update()
        )
        if control is None:
            raise OperationalInputError("REGIONAL_POLICY_REQUIRED")
        pending = await session.get(FleetWakeup, control.scope)
        due = pending and pending.generation > pending.acknowledged and pending.due_at <= now
        if (control.next_due_at > now and not due) or (
            control.lease_until and control.lease_until > now
        ):
            return None
        await require_live_role(session, control.manager_id, "MANAGER")
        policy = RegionalPolicy.model_validate(control.configuration)
        for abandoned in await session.scalars(
            select(FleetCycle).where(
                FleetCycle.scope == control.scope, FleetCycle.status == "RUNNING"
            )
        ):
            abandoned.status, abandoned.completed_at, abandoned.failure_code = (
                "STALE",
                now,
                "REGIONAL_LEASE_EXPIRED",
            )
        token = new_uuid7()
        control.lease_token, control.lease_until = (
            token,
            now + timedelta(seconds=policy.fleet.lease_seconds),
        )
        cycle = FleetCycle(
            scope=control.scope,
            control_version=control.version,
            requested_by=control.manager_id,
            lease_token=token,
            status="RUNNING",
            created_at=now,
        )
        session.add(cycle)
        await session.flush()
        generation = pending.generation if pending else 0
    try:
        from .reconciliation import reconcile_cancellations

        await reconcile_cancellations(factory, clock(), region_id=region_id)
        from tirodhan.modules.planning.routing.adapters import ORToolsOptimizer, VroomOptimizer

        from .destinations import recover_destinations

        await recover_destinations(
            factory,
            providers=providers,
            optimizers={
                OptimizerEngine.VROOM: VroomOptimizer(),
                OptimizerEngine.OR_TOOLS: ORToolsOptimizer(),
            },
            now=clock(),
            control_scope=scope(region_id),
            region_id=region_id,
        )
        config = policy.fleet.snapshot_configuration(now).model_copy(
            update={
                "region_id": region_id,
                "confirmed_shifts": True,
                "max_requests": policy.max_discovery,
                "policy_version": policy.version,
            }
        )
        snapshot = await capture_snapshot(factory, config)
        ready, issues = regional_ready(snapshot, config, clock())
        if any(route is not None for _, route, _ in issues):
            # Preserve incumbent obligations through the existing hold/recovery mechanism.
            from .rolling import _hold_unsafe_routes

            await _hold_unsafe_routes(
                factory, cycle.requested_by, issues, snapshot, cycle, config, clock()
            )
            snapshot = await capture_snapshot(factory, config)
            ready, issues = regional_ready(snapshot, config, clock())
        # An unchanged route is an alternative only after an independent live proof.
        # Quarantine an infeasible incumbent resource rather than block healthy riders.
        unsafe: list[tuple[UUID | None, UUID | None, str]] = []
        resource_riders = {v["vehicle_id"]: v["rider_id"] for v in ready.sources["vehicles"]}
        prior_orders = incumbent_orders(ready)
        for route in ready.sources["routes"]:
            vehicle_id = route["vehicle_id"]
            rider_id = resource_riders[vehicle_id]
            own = {
                r["request_id"]
                for r in ready.sources["requests"]
                if r["owner_id"] == rider_id or r["collector_id"] == rider_id
            }
            try:
                private = await build_problem(
                    subset(ready, own, {vehicle_id}), config, new_uuid7(), providers, now=clock()
                )
                candidate(
                    private,
                    ProposedRoute(vehicle_id, prior_orders.get(vehicle_id, ())),
                    "INCUMBENT",
                    VERSION,
                    policy.fleet.economics,
                    prior_orders,
                )
            except RoutingValidationError as error:
                if not any(
                    v["vehicle_id"] == vehicle_id and v.get("resume_authorized")
                    for v in ready.sources["vehicles"]
                ):
                    unsafe.append((None, route["route_id"], str(error)))
            except OperationalInputError as error:
                if str(error) != "COMMITMENT_WINDOW_EXPIRED":
                    raise
                unsafe.append((None, route["route_id"], str(error)))
        if unsafe:
            from .rolling import _hold_unsafe_routes

            await _hold_unsafe_routes(
                factory, cycle.requested_by, unsafe, snapshot, cycle, config, clock()
            )
            snapshot = await capture_snapshot(factory, config)
            ready, issues = regional_ready(snapshot, config, clock())
        async with factory() as session:
            first_wait: dict[UUID, datetime] = dict(
                (
                    await session.execute(
                        select(PlanningOutcome.request_id, func.min(PlanningOutcome.evaluated_at))
                        .where(
                            PlanningOutcome.region_id == region_id,
                            PlanningOutcome.request_id
                            == any_(
                                cast(
                                    [
                                        r["request_id"]
                                        for r in snapshot.sources["requests"]
                                        if not r.get("hold")
                                    ],
                                    ARRAY(SQLUUID(as_uuid=True)),
                                )
                            ),
                        )
                        .group_by(PlanningOutcome.request_id)
                    )
                )
                .tuples()
                .all()
            )
        admitted, priorities = admit(ready, policy, clock(), first_wait)
        inputs: list[dict[str, Any]] = [
            dict(
                request_id=str(r["request_id"]),
                existing=required(r),
                reason="COMPUTATION_BUDGET_DEFERRED"
                if r["request_id"] not in admitted
                else "NOT_SELECTED_IN_CANDIDATE_POOL",
            )
            for r in snapshot.sources["requests"]
            if not r.get("hold")
        ]
        request_issues = {request: reason for request, _, reason in issues if request is not None}
        vehicle_riders = {v["vehicle_id"]: v["rider_id"] for v in snapshot.sources["vehicles"]}
        route_riders = {
            r["route_id"]: vehicle_riders.get(r["vehicle_id"]) for r in snapshot.sources["routes"]
        }
        rider_issues = {
            route_riders[route]: reason
            for _, route, reason in issues
            if route in route_riders and route_riders[route] is not None
        }
        source_requests = {r["request_id"]: r for r in snapshot.sources["requests"]}
        for item in inputs:
            request = source_requests[UUID(item["request_id"])]
            reason = (
                request_issues.get(UUID(item["request_id"]))
                or rider_issues.get(request["owner_id"])
                or rider_issues.get(request["collector_id"])
            )
            if reason:
                item["reason"], item["recovery"] = reason, True
            if priorities.get(UUID(item["request_id"])) == 3:
                item["reason"] = "WAIT_AGE_ESCALATED"
        selected_snapshot = subset(ready, admitted)
        if not selected_snapshot.sources["vehicles"] or (
            not selected_snapshot.sources["requests"] and not selected_snapshot.sources["routes"]
        ):
            await offer_deferred(
                factory, ready.sources["requests"], set(), priorities, policy, clock()
            )
            async with factory() as session, session.begin():
                current = await session.get(FleetCycle, cycle.cycle_id)
                assert current is not None
                current.measurements = {
                    "generation": generation,
                    "inputs": [
                        item
                        if item.get("recovery") or item["reason"] == "WAIT_AGE_ESCALATED"
                        else dict(item, reason="CONFIRMED_ACTIVE_CAPACITY_REQUIRED")
                        for item in inputs
                    ],
                    "assigned": [],
                    "retry_seconds": policy.fleet.minimum_interval_seconds,
                }
                current.status, current.completed_at = "UNCHANGED", clock()
                await complete_cycle(session, current, clock())
                return current
        mark = time.monotonic()
        problem = await build_problem(
            selected_snapshot, config, new_uuid7(), providers, now=clock()
        )
        timings = {
            "snapshot_admission_seconds": mark - started,
            "matrix_build_seconds": time.monotonic() - mark,
        }
        orders = incumbent_orders(ready)
        pool: list[Candidate] = []
        for v in problem.vehicles:
            order = orders.get(v.vehicle_id, ())
            try:
                pool.append(
                    candidate(
                        problem,
                        ProposedRoute(v.vehicle_id, order),
                        "INCUMBENT",
                        VERSION,
                        policy.fleet.economics,
                        {v.vehicle_id: order},
                    )
                )
            except RoutingValidationError:
                if (order or v.protected_prefix) and not any(
                    row["vehicle_id"] == v.vehicle_id and row.get("resume_authorized")
                    for row in ready.sources["vehicles"]
                ):
                    raise OperationalInputError("INCUMBENT_REQUIRES_RECOVERY") from None
        tasks = []
        task_problems = {}
        neighbourhood = neighbourhoods(problem, policy.neighbourhood_size, priorities, orders)
        for v in selected_snapshot.sources["vehicles"]:
            own = {
                r["request_id"]
                for r in selected_snapshot.sources["requests"]
                if required(r)
                and (r["owner_id"] == v["rider_id"] or r["collector_id"] == v["rider_id"])
            }
            primary, expanded = neighbourhood[v["vehicle_id"]]
            ids = own | expanded
            # Passive reservations are mandatory capacity context in every eligible search.
            ids |= {r["request_id"] for r in selected_snapshot.sources["requests"] if r.get("hold")}
            local = slice_problem(problem, ids, v["vehicle_id"])
            holds = {
                r["request_id"] for r in selected_snapshot.sources["requests"] if r.get("hold")
            }
            # Individual searches may share reservation alternatives. Their exactly-once
            # coverage is mandatory in regional consolidation, not in every vehicle.
            local = replace(
                local,
                stops=tuple(
                    replace(s, required=False) if s.lot_id in holds else s for s in local.stops
                ),
            )
            for engine in OptimizerEngine:
                variants = [local]
                if primary != expanded:
                    keep = own | primary | holds
                    variants.append(
                        replace(
                            local,
                            lots=tuple(lot for lot in local.lots if lot.lot_id in keep),
                            stops=tuple(s for s in local.stops if s.lot_id in keep),
                        )
                    )
                for variant in variants:
                    task = WorkerTask(
                        task_id=new_uuid7(),
                        engine=engine,
                        problem=variant,
                        search_seconds=policy.fleet.search_seconds,
                    )
                    tasks.append(task)
                    task_problems[task.task_id] = variant
        # A bounded nearest-neighbour pairing permits transfer of future, unprotected
        # commitments. The common gate retains protected prefixes and onboard custody.
        paired: set[frozenset[UUID]] = set()
        resource_rows = selected_snapshot.sources["vehicles"]
        for row in resource_rows:
            others = [other for other in resource_rows if other["vehicle_id"] != row["vehicle_id"]]
            if not others:
                continue
            other = min(others, key=lambda item: (distance(row, item), str(item["vehicle_id"])))
            pair = frozenset((row["vehicle_id"], other["vehicle_id"]))
            if pair in paired:
                continue
            paired.add(pair)
            riders = {row["rider_id"], other["rider_id"]}
            own = {
                r["request_id"]
                for r in selected_snapshot.sources["requests"]
                if required(r) and (r["owner_id"] in riders or r["collector_id"] in riders)
            }
            holds = {
                r["request_id"] for r in selected_snapshot.sources["requests"] if r.get("hold")
            }
            options = set().union(*(neighbourhood[vehicle][0] for vehicle in pair))
            # Bound optional pair work to the neighbourhood budget; retain mandatory work.
            options = set(
                sorted(options, key=lambda lot: (-priorities.get(lot, 0), str(lot)))[
                    : policy.neighbourhood_size
                ]
            )
            local = group_problem(problem, own | holds | options, tuple(sorted(pair, key=str)))
            local = replace(
                local,
                stops=tuple(
                    replace(stop, required=False) if stop.lot_id in holds else stop
                    for stop in local.stops
                ),
            )
            for engine in OptimizerEngine:
                task = WorkerTask(
                    task_id=new_uuid7(),
                    engine=engine,
                    problem=local,
                    search_seconds=policy.fleet.search_seconds,
                )
                tasks.append(task)
                task_problems[task.task_id] = local
        mark = time.monotonic()
        results = await workers(tasks, policy.processes)
        if (
            len(results) != len(tasks)
            or {result.task_id for result in results} != {task.task_id for task in tasks}
            or any(result.version != VERSION for result in results)
        ):
            raise OperationalInputError("WORKER_RESULT_COVERAGE_OR_VERSION")
        task_engines = {task.task_id: task.engine for task in tasks}
        for result in results:
            if result.proposal is None:
                continue
            local = task_problems[result.task_id]
            if result.proposal.engine != task_engines[result.task_id]:
                raise OperationalInputError("WORKER_ENGINE_IDENTITY")
            validate_proposal(local, result.proposal)
            for route in result.proposal.routes:
                pool.append(
                    candidate(
                        local,
                        route,
                        result.proposal.engine.value,
                        result.proposal.engine_version,
                        policy.fleet.economics,
                        {route.vehicle_id: orders.get(route.vehicle_id, ())},
                    )
                )
        timings["worker_seconds"] = time.monotonic() - mark
        mark = time.monotonic()
        pool = conflict_alternatives(
            problem, pool, policy.fleet.economics, orders, policy.max_adjustments
        )
        selection = await asyncio.to_thread(
            consolidate,
            pool,
            {v.vehicle_id for v in problem.vehicles},
            {s.lot_id for s in problem.stops if s.required},
            priorities,
            policy_version=policy.version,
            seconds=policy.selection_seconds,
            facility_limits=tuple(
                FacilityLimit(
                    receiving_point_id=point["receiving_point_id"],
                    concurrent_unloads=point["unloading_capacity"],
                )
                for point in snapshot.sources["points"]
                if point.get("unloading_capacity") is not None
            ),
            external_visits=tuple(
                (
                    visit["receiving_point_id"],
                    int((visit["planned_start"] - problem.epoch).total_seconds()),
                    int((visit["planned_end"] - problem.epoch).total_seconds()),
                )
                for visit in snapshot.sources.get("external_facility_visits", [])
            ),
        )
        routes = selected_routes(selection)
        timings["conflict_selection_seconds"] = time.monotonic() - mark
        served = {s for route in routes for s in route.stop_ids}
        proposal = RouteProposal(
            problem.problem_id,
            problem.snapshot_id,
            problem.contract_version,
            problem.policy_version,
            SelectionEngine.COMPOSITE,
            VERSION,
            routes,
            tuple(s.stop_id for s in problem.stops if s.stop_id not in served),
        )
        # Supplementary capacity keeps the existing voluntary offer protocol. Offer
        # generation is bounded; accepting one never assigns a route by itself.
        covered = {s.lot_id for s in problem.stops if s.stop_id in served}
        await offer_deferred(
            factory, selected_snapshot.sources["requests"], covered, priorities, policy, clock()
        )
        discovery_fingerprint = source_digest(snapshot.sources).hex()
        # New unrelated optional arrivals belong to the next generation. Continue to
        # fence all live ownership/cargo, capacity holds, resources and facility context.
        config = config.model_copy(update={"request_ids": tuple(sorted(admitted, key=str))})
        admitted_text = {str(identity) for identity in admitted}
        seed_candidate_count = len(selection.candidates)
        history: list[dict[str, Any]] = []
        for attempt in range(policy.fleet.publication_attempts):
            history.append(
                {
                    "problem": PROBLEM_CODEC.dump_python(problem, mode="json"),
                    "selection": selection.model_dump(mode="json"),
                }
            )
            refresh_started = time.monotonic()
            snapshot, problem, selection, orders = await refresh_selection(
                factory,
                snapshot,
                config,
                problem,
                selection,
                admitted,
                priorities,
                policy,
                providers,
                clock(),
            )
            timings["publication_refresh_seconds"] = (
                timings.get("publication_refresh_seconds", 0) + time.monotonic() - refresh_started
            )
            routes = selected_routes(selection)
            served = {stop for route in routes for stop in route.stop_ids}
            proposal = RouteProposal(
                problem.problem_id,
                problem.snapshot_id,
                problem.contract_version,
                problem.policy_version,
                SelectionEngine.COMPOSITE,
                VERSION,
                routes,
                tuple(stop.stop_id for stop in problem.stops if stop.stop_id not in served),
            )
            # Captured inputs retain a durable outcome even if cancelled/completed meanwhile.
            live_ids = {str(request["request_id"]) for request in snapshot.sources["requests"]}
            inputs = [
                dict(item, reason="SOURCE_NO_LONGER_ELIGIBLE")
                if item["request_id"] in admitted_text and item["request_id"] not in live_ids
                else item
                for item in inputs
            ]
            assessment = validate_proposal(problem, proposal)
            measured = measure(problem, proposal, policy.fleet.economics, orders)
            routing_metrics: dict[str, Any] = {
                "travel_seconds": measured.travel_seconds,
                "distance_m": measured.distance_m,
                "waiting_seconds": measured.waiting_seconds,
                "fleet_cost_seconds": measured.generalized_seconds,
                "used_riders": sum(bool(route.stop_ids) for route in proposal.routes),
                "candidate_count": len(selection.candidates),
                "seed_candidate_count": seed_candidate_count,
                "task_count": len(tasks),
                "solver_timeouts": sum(r.failure == "SOLVER_TIMEOUT" for r in results),
                "selection_statuses": selection.stage_statuses,
                "selection_values": selection.stage_values,
                "selection_bounds": selection.stage_bounds,
                "occupied_route_seconds": sum(
                    route.end_at
                    - next(
                        v.available_at for v in problem.vehicles if v.vehicle_id == route.vehicle_id
                    )
                    for route in assessment.routes
                    if route.stops
                ),
            }
            async with factory() as session, session.begin():
                current = await session.get(FleetCycle, cycle.cycle_id)
                assert current is not None
                current.selected_engine = "COMPOSITE"
                current.measurements = {
                    "policy": policy.fleet.model_dump(mode="json"),
                    "generation": generation,
                    "discovery_fingerprint": discovery_fingerprint,
                    "inputs": inputs,
                    "assigned": [str(s.lot_id) for s in problem.stops if s.stop_id in served],
                    "retry_seconds": policy.fleet.minimum_interval_seconds,
                    "timings": timings,
                    "worker_failures": sum(r.failure is not None for r in results),
                    "routing_metrics": routing_metrics,
                }
                # The publication fence covers selected demand and all mandatory/physical
                # context; unrelated optional arrivals remain for their later generation.
                session.add(
                    RoutingShadowRun(
                        run_id=problem.problem_id,
                        requested_by=cycle.requested_by,
                        client_command_id=problem.problem_id,
                        fingerprint=source_digest(snapshot.sources),
                        configuration=config.model_dump(mode="json"),
                        status="SUCCEEDED",
                        problem=PROBLEM_CODEC.dump_python(problem, mode="json"),
                        source_fingerprint=source_digest(
                            snapshot.sources,
                            position_vehicle_ids={v.vehicle_id for v in problem.vehicles},
                        ),
                        business_fingerprint=source_digest(snapshot.sources, business_only=True),
                        results={
                            "COMPOSITE": {
                                "proposal": PROPOSAL_CODEC.dump_python(proposal, mode="json"),
                                "selection": selection.model_dump(mode="json"),
                                "rebase_history": history,
                            }
                        },
                        completed_at=clock(),
                    )
                )
                shadow = next(obj for obj in session.new if isinstance(obj, RoutingShadowRun))
                await session.flush([shadow])
                current.run_id = problem.problem_id
            try:
                mark = time.monotonic()
                async with factory() as session:
                    await session.connection(execution_options={"isolation_level": "SERIALIZABLE"})
                    await _activate(
                        session,
                        cycle.requested_by,
                        ActivationCommand(
                            client_command_id=cycle.cycle_id,
                            run_id=problem.problem_id,
                            engine=SelectionEngine.COMPOSITE,
                            fleet_cycle_id=cycle.cycle_id,
                            resume_recovery=any(
                                v.get("resume_authorized")
                                for v in snapshot.sources["vehicles"]
                                if v["vehicle_id"]
                                in {resource.vehicle_id for resource in problem.vehicles}
                            ),
                        ),
                        now + timedelta(days=1),
                        clock(),
                        clock,
                    )
                    await session.commit()
            except Exception as error:
                conflict = isinstance(error, RouteAuthorityError) and str(error) == "SOURCE_CHANGED"
                conflict = conflict or getattr(getattr(error, "orig", None), "sqlstate", None) in {
                    "40001",
                    "40P01",
                }
                if not conflict or attempt + 1 >= policy.fleet.publication_attempts:
                    raise
                continue
            break
        from .synchronization import publish_etas

        await publish_etas(
            factory,
            problem,
            proposal,
            now=clock(),
            freshness_seconds=policy.fleet.maximum_interval_seconds,
        )
        if observations is not None:
            observations.update(timings)
            observations["validation_commit_seconds"] = time.monotonic() - mark
            observations["total_seconds"] = time.monotonic() - started
        # Terminal cycle history is immutable. The caller measures the encompassing
        # commit latency; persisted timings intentionally end before activation.
        async with factory() as session:
            current = await session.get(FleetCycle, cycle.cycle_id)
            assert current is not None
            return current
    except Exception as error:
        async with factory() as session, session.begin():
            control = await session.scalar(
                select(FleetControl).where(FleetControl.scope == scope(region_id)).with_for_update()
            )
            current = await session.get(FleetCycle, cycle.cycle_id)
            assert current is not None and control is not None
            if current.status == "RUNNING" and control.lease_token == cycle.lease_token:
                current.status, current.completed_at, current.failure_code = (
                    "FAILED",
                    clock(),
                    str(error)[:100]
                    if isinstance(
                        error, (OperationalInputError, RoutingValidationError, RoutingAdapterError)
                    )
                    else "RETRYABLE_TRANSACTION_CONFLICT"
                    if getattr(getattr(error, "orig", None), "sqlstate", None) in {"40001", "40P01"}
                    else type(error).__name__,
                )
                control.lease_token, control.lease_until = None, None
                control.next_due_at = clock() + timedelta(
                    seconds=policy.fleet.minimum_interval_seconds
                )
        raise
