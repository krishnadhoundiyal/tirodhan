"""Finite single-owner collective planning, live road rebase and fenced authority."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.dispatch.models import RiderAssignment
from tirodhan.modules.planning.routing.adapters import ORTOOLS_VERSION, VROOM_VERSION
from tirodhan.modules.planning.routing.contracts import (
    OptimizerEngine,
    ProposedRoute,
    RouteOptimizer,
    RouteProposal,
)
from tirodhan.modules.planning.routing.execution import RoutingAdapterError
from tirodhan.modules.planning.routing.validation import RoutingValidationError, validate_proposal
from tirodhan.modules.reliability.primitives import (
    append_outbox_event,
    claim_idempotency_record,
    complete_idempotency_record,
)

from .authority import ActivationCommand, RouteAuthorityError, activate_proposal, lock_resources
from .commands import OperationalInputError, require_live_role
from .dispatch_models import RouteTask, VehicleRoute
from .economics import (
    EconomicPolicy,
    FleetCost,
    insertion_search,
    make_candidate,
    measure,
    nearest_feasible_baseline,
)
from .fleet_models import FleetControl, FleetCycle, FleetException, FleetPositionAnchor
from .models import RoutingShadowRun, VehiclePosition
from .service import PROBLEM_CODEC, PROPOSAL_CODEC
from .snapshot import (
    MatrixSource,
    OperationalSnapshot,
    ShadowConfiguration,
    build_problem,
    capture_snapshot,
    source_digest,
)
from .telemetry import TelemetryPolicy

SCOPE = "COMMITTED_FLEET"
VERSIONS = {OptimizerEngine.VROOM: VROOM_VERSION, OptimizerEngine.OR_TOOLS: ORTOOLS_VERSION}


class FleetPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: str = Field(min_length=1, max_length=100)
    horizon_seconds: int = Field(strict=True, ge=60, le=86400)
    minimum_interval_seconds: int = Field(strict=True, ge=1, le=3600)
    maximum_interval_seconds: int = Field(strict=True, ge=1, le=86400)
    displacement_m: int = Field(strict=True, ge=1, le=10000)
    destination_divergence_m: int = Field(strict=True, ge=1, le=10000)
    publication_attempts: int = Field(strict=True, ge=1, le=10)
    max_requests: int = Field(strict=True, ge=1, le=128)
    max_vehicles: int = Field(strict=True, ge=1, le=32)
    max_nodes: int = Field(strict=True, ge=1, le=512)
    max_profiles: int = Field(strict=True, ge=1, le=8)
    search_seconds: int = Field(strict=True, ge=1, le=30)
    lease_seconds: int = Field(strict=True, ge=60, le=3600)
    telemetry: TelemetryPolicy
    economics: EconomicPolicy

    @model_validator(mode="after")
    def ordered(self) -> FleetPolicy:
        if self.maximum_interval_seconds < self.minimum_interval_seconds:
            raise ValueError("maximum interval precedes minimum interval")
        if self.lease_seconds < self.search_seconds * 2 + 30:
            raise ValueError("lease must cover both solver budgets and publication")
        return self

    def snapshot_configuration(self, now: datetime) -> ShadowConfiguration:
        epoch = now.replace(microsecond=0)
        return ShadowConfiguration(
            epoch=epoch,
            horizon_end=epoch + timedelta(seconds=self.horizon_seconds),
            policy_version=self.version,
            max_position_age_seconds=self.telemetry.max_age_seconds,
            max_requests=self.max_requests,
            max_vehicles=self.max_vehicles,
            max_nodes=self.max_nodes,
            max_profiles=self.max_profiles,
            search_seconds=self.search_seconds,
            lease_seconds=self.lease_seconds,
            fleet_mode=True,
        )


class ConfigureFleet(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    client_command_id: UUID
    expected_version: int = Field(strict=True, ge=0)
    policy: FleetPolicy


async def configure_fleet(
    factory: async_sessionmaker[AsyncSession],
    *,
    manager_id: UUID,
    command: ConfigureFleet,
    expires_at: datetime,
    now: datetime | None = None,
) -> UUID:
    now = now or utc_now()
    async with factory() as session, session.begin():
        await require_live_role(session, manager_id, "MANAGER")
        claim = await claim_idempotency_record(
            session,
            scope=f"routing.fleet.configure:{manager_id}",
            idempotency_key=str(command.client_command_id),
            request_fingerprint=hashlib.sha256(command.model_dump_json().encode()).digest(),
            expires_at=expires_at,
            now=now,
        )
        if claim.record.status == "COMPLETED":
            assert claim.record.result_resource_id is not None
            return claim.record.result_resource_id
        if not claim.created:
            raise RouteAuthorityError("COMMAND_IN_PROGRESS")
        created = await session.scalar(
            insert(FleetControl)
            .values(
                scope=SCOPE,
                manager_id=manager_id,
                version=1,
                configuration=command.policy.model_dump(mode="json"),
                next_due_at=now,
            )
            .on_conflict_do_nothing(index_elements=["scope"])
            .returning(FleetControl.scope)
        )
        control = await session.scalar(
            select(FleetControl).where(FleetControl.scope == SCOPE).with_for_update()
        )
        assert control is not None
        if command.expected_version == 0:
            if created is None:
                raise RouteAuthorityError("FLEET_POLICY_VERSION_CONFLICT")
        else:
            if control.version != command.expected_version:
                raise RouteAuthorityError("FLEET_POLICY_VERSION_CONFLICT")
            control.version += 1
            control.manager_id, control.configuration = (
                manager_id,
                command.policy.model_dump(mode="json"),
            )
            control.lease_token = control.lease_until = None
            control.business_fingerprint = None
            control.next_due_at = now
        # The client UUID is the durable command result, never policy/coordinate payload.
        await complete_idempotency_record(
            session,
            claim.record,
            result_resource_id=command.client_command_id,
            result_status_code=200,
            completed_at=now,
        )
        return command.client_command_id


async def _claim(
    factory: async_sessionmaker[AsyncSession],
    now: datetime,
) -> tuple[FleetCycle, FleetPolicy, UUID] | None:
    async with factory() as session, session.begin():
        control = await session.scalar(
            select(FleetControl).where(FleetControl.scope == SCOPE).with_for_update()
        )
        if (
            control is None
            or control.next_due_at > now
            or (control.lease_until is not None and control.lease_until > now)
        ):
            return None
        await require_live_role(session, control.manager_id, "MANAGER")
        policy = FleetPolicy.model_validate(control.configuration)
        abandoned = list(
            await session.scalars(
                select(FleetCycle).where(FleetCycle.scope == SCOPE, FleetCycle.status == "RUNNING")
            )
        )
        for old in abandoned:
            old.status, old.failure_code, old.completed_at = "STALE", "WORKER_LEASE_EXPIRED", now
            session.add(
                FleetException(
                    cycle_id=old.cycle_id,
                    reported_by=control.manager_id,
                    reason="WORKER_LEASE_EXPIRED",
                    created_at=now,
                )
            )
        token = new_uuid7()
        control.lease_token, control.lease_until = (
            token,
            now + timedelta(seconds=policy.lease_seconds),
        )
        cycle = FleetCycle(
            scope=SCOPE,
            control_version=control.version,
            requested_by=control.manager_id,
            lease_token=token,
            measurements={"policy": policy.model_dump(mode="json")},
            status="RUNNING",
            created_at=now,
        )
        session.add(cycle)
        await session.flush([cycle])
        return cycle, policy, control.manager_id


def incumbent_orders(snapshot: OperationalSnapshot) -> dict[UUID, tuple[UUID, ...]]:
    return {
        route["vehicle_id"]: tuple(
            t["stop_id"] for t in snapshot.sources["tasks"] if t["route_id"] == route["route_id"]
        )
        for route in snapshot.sources["routes"]
    }


def _receiving_window_possible(
    sources: dict[str, Any], request: dict[str, Any], now: datetime, end: datetime
) -> bool:
    """Prove static impossibility without roads; travel can only tighten this envelope."""
    point_id = request["receiving_point_id"]
    if not any(
        p["receiving_point_id"] == point_id and p["status"] == "ACTIVE" for p in sources["points"]
    ):
        return False
    end = min(end, request["discharge_deadline"])
    intervals: list[tuple[datetime, datetime]] = []
    for window in sources["windows"]:
        if window["receiving_point_id"] != point_id:
            continue
        start, finish = max(now, window["starts_at"]), min(end, window["ends_at"])
        if finish < start:
            continue
        if intervals and start <= intervals[-1][1]:
            intervals[-1] = (intervals[-1][0], max(finish, intervals[-1][1]))
        else:
            intervals.append((start, finish))
    for restriction in sources["restrictions"]:
        if restriction["receiving_point_id"] != point_id:
            continue
        left, right = restriction["starts_at"], restriction["ends_at"]
        remaining = []
        for start, finish in intervals:
            if right <= start or left >= finish:
                remaining.append((start, finish))
            else:
                if start < left:
                    remaining.append((start, left))
                if right < finish:
                    remaining.append((right, finish))
        intervals = remaining
    return any(
        (finish - start).total_seconds() >= request["discharge_seconds"]
        for start, finish in intervals
    )


def ready_snapshot(
    snapshot: OperationalSnapshot,
    config: ShadowConfiguration,
    now: datetime,
) -> tuple[OperationalSnapshot, list[tuple[UUID | None, UUID | None, str]]]:
    """Quarantine explicit exceptions; neither drop ownership nor synthesize physical inputs."""
    sources = dict(snapshot.sources)
    issues: list[tuple[UUID | None, UUID | None, str]] = []
    issues.extend(
        (None, r["route_id"], "SHIFT_END_REQUIRES_RECOVERY") for r in sources["orphaned_routes"]
    )
    excluded: set[UUID] = set()
    for vehicle in sources["vehicles"]:
        route = next(
            (r for r in sources["routes"] if r["vehicle_id"] == vehicle["vehicle_id"]), None
        )
        reason = None
        if vehicle["blocked_reason"] is not None or (
            route and route["status"] == "RECOVERY_REQUIRED"
        ):
            reason = "CONTROLLED_RECOVERY_REQUIRED"
        elif vehicle["legacy_assignment"]:
            reason = "LEGACY_ASSIGNMENT_MUST_DRAIN"
        elif vehicle["starts_at"] > now:
            reason = "SHIFT_NOT_ACTIVE"
        elif (
            vehicle["observed_at"] is None
            or not 0
            <= (now - vehicle["observed_at"]).total_seconds()
            <= config.max_position_age_seconds
        ):
            reason = "FRESH_POSITION_REQUIRED"
        elif (
            vehicle["user_status"] != "ACTIVE"
            or vehicle["rider_status"] != "ACTIVE"
            or not vehicle["has_role"]
        ):
            reason = "RESOURCE_AUTHORIZATION_STALE"
        if reason:
            excluded.add(vehicle["vehicle_id"])
            if route:
                issues.append((None, route["route_id"], reason))
            else:
                issues.extend(
                    (r["request_id"], None, reason)
                    for r in sources["requests"]
                    if r["owner_id"] == vehicle["rider_id"]
                    or r["collector_id"] == vehicle["rider_id"]
                )
            continue
        owned = [
            r
            for r in sources["requests"]
            if r["owner_id"] == vehicle["rider_id"] or r["collector_id"] == vehicle["rider_id"]
        ]
        onboard = [r for r in owned if r["pickup_status"] == "COLLECTED"]
        if any(r["load_uncertain"] for r in owned):
            reason = "AUDITED_LOAD_CORRECTION_REQUIRED"
        elif any(r["mass_g"] is None for r in owned):
            reason = "MANIFEST_REQUIRED"
        elif (
            sum(r["mass_g"] for r in onboard) > vehicle["mass_capacity_g"]
            or sum(r["volume_ml"] for r in onboard) > vehicle["volume_capacity_ml"]
        ):
            reason = "PHYSICAL_CAPACITY_EXCEEDED"
        elif any(r["discharge_deadline"] <= now for r in onboard):
            reason = "CUSTODY_DEADLINE_MISSED"
        elif any(
            not _receiving_window_possible(
                sources, r, now, min(vehicle["ends_at"], config.horizon_end)
            )
            for r in owned
        ):
            reason = "NO_AUTHORIZED_DISCHARGE_WINDOW"
        if reason:
            excluded.add(vehicle["vehicle_id"])
            if route:
                issues.append((None, route["route_id"], reason))
    excluded_riders = {v["rider_id"] for v in sources["vehicles"] if v["vehicle_id"] in excluded}
    sources["vehicles"] = [v for v in sources["vehicles"] if v["vehicle_id"] not in excluded]
    sources["routes"] = [r for r in sources["routes"] if r["vehicle_id"] not in excluded]
    roots = {r["route_id"] for r in sources["routes"]}
    sources["tasks"] = [t for t in sources["tasks"] if t["route_id"] in roots]
    sources["released"] = [r for r in sources["released"] if r["vehicle_id"] not in excluded]
    requests = []
    for request in sources["requests"]:
        if request["owner_id"] in excluded_riders or request["collector_id"] in excluded_riders:
            continue  # These obligations stay owned/custodied by the quarantined route.
        if request["owner_id"] is None and request["collector_id"] is None:
            reason = (
                "AUDITED_LOAD_CORRECTION_REQUIRED"
                if request["load_uncertain"]
                else "MANIFEST_REQUIRED"
                if request["mass_g"] is None
                else "COMMITMENT_WINDOW_EXPIRED"
                if request["slot_end"] < now
                else "LEGACY_BATCH_MUST_DRAIN"
                if request["status"] == "PRE_PLANNING"
                else None
            )
            if reason:
                issues.append((request["request_id"], None, reason))
                continue
        requests.append(request)
    sources["requests"] = requests
    return OperationalSnapshot(sources, snapshot.fingerprint), issues


async def _should_evaluate(
    factory: async_sessionmaker[AsyncSession],
    snapshot: OperationalSnapshot,
    policy: FleetPolicy,
    now: datetime,
) -> bool:
    async with factory() as session:
        control = await session.get(FleetControl, SCOPE)
        assert control is not None
        if control.business_fingerprint != source_digest(snapshot.sources, business_only=True):
            return True
        if (
            control.last_evaluated_at is None
            or (now - control.last_evaluated_at).total_seconds() >= policy.maximum_interval_seconds
        ):
            return True
        for vehicle in snapshot.sources["vehicles"]:
            anchor = await session.get(FleetPositionAnchor, vehicle["vehicle_id"])
            if anchor is None or vehicle["position_id"] is None:
                return True
            distance = await session.scalar(
                select(
                    func.ST_Distance(
                        VehiclePosition.location,
                        func.ST_GeogFromText(
                            f"SRID=4326;POINT({vehicle['longitude']} {vehicle['latitude']})"
                        ),
                    )
                ).where(VehiclePosition.position_id == anchor.position_id)
            )
            if distance is not None and distance >= policy.displacement_m:
                return True
            route = next(
                (r for r in snapshot.sources["routes"] if r["vehicle_id"] == vehicle["vehicle_id"]),
                None,
            )
            committed = next(
                (
                    t
                    for t in snapshot.sources["tasks"]
                    if route and t["route_id"] == route["route_id"] and t["committed_at"]
                ),
                None,
            )
            if committed:
                # Displacement/time/business progress are sufficient triggers. Destination
                # divergence is evaluated against task locations separately in _destination_drift.
                if await _destination_drift(session, committed, vehicle, anchor, policy):
                    return True
        return False


async def _destination_drift(
    session: AsyncSession,
    task: dict[str, Any],
    vehicle: dict[str, Any],
    anchor: FleetPositionAnchor,
    policy: FleetPolicy,
) -> bool:
    from tirodhan.modules.collection_requests.models import CollectionRequest
    from tirodhan.modules.planning.models import PickupExecution
    from tirodhan.modules.receiving_points.models import ReceivingPoint

    row = await session.get(RouteTask, task["task_id"])
    assert row is not None
    if row.kind == "PICKUP":
        pickup = await session.get(PickupExecution, row.pickup_execution_id)
        assert pickup is not None
        destination = (
            select(CollectionRequest.pickup_location)
            .where(CollectionRequest.request_id == pickup.request_id)
            .scalar_subquery()
        )
    else:
        destination = (
            select(ReceivingPoint.location)
            .where(ReceivingPoint.receiving_point_id == row.receiving_point_id)
            .scalar_subquery()
        )
    delta = await session.scalar(
        select(
            func.ST_Distance(
                func.ST_GeogFromText(
                    f"SRID=4326;POINT({vehicle['longitude']} {vehicle['latitude']})"
                ),
                destination,
            )
            - func.ST_Distance(VehiclePosition.location, destination)
        ).where(VehiclePosition.position_id == anchor.position_id)
    )
    return delta is not None and delta >= policy.destination_divergence_m


async def _finish(
    factory: async_sessionmaker[AsyncSession],
    cycle: FleetCycle,
    policy: FleetPolicy,
    actor: UUID,
    status: str,
    code: str | None,
    snapshot: OperationalSnapshot | None,
    issues: list[tuple[UUID | None, UUID | None, str]],
    now: datetime,
) -> FleetCycle:
    async with factory() as session, session.begin():
        control = await session.scalar(
            select(FleetControl).where(FleetControl.scope == SCOPE).with_for_update()
        )
        current = await session.get(FleetCycle, cycle.cycle_id)
        assert current is not None
        if (
            control is None
            or control.lease_token != cycle.lease_token
            or control.version != cycle.control_version
            or control.lease_until is None
            or control.lease_until <= now
        ):
            if current.status == "ACTIVATED":
                return current  # Authority committed before an ambiguous worker acknowledgement.
            raise RouteAuthorityError("FLEET_LEASE_LOST")
        if current.status != "ACTIVATED":
            current.status, current.failure_code, current.completed_at = status, code, now
        control.lease_token = control.lease_until = None
        control.next_due_at = now + timedelta(seconds=policy.minimum_interval_seconds)
        if snapshot is not None and current.status in ("ACTIVATED", "UNCHANGED"):
            control.business_fingerprint = source_digest(snapshot.sources, business_only=True)
            control.last_evaluated_at = now
            await session.execute(
                delete(FleetPositionAnchor).where(FleetPositionAnchor.scope == SCOPE)
            )
            for vehicle in snapshot.sources["vehicles"]:
                if vehicle["position_id"]:
                    session.add(
                        FleetPositionAnchor(
                            scope=SCOPE,
                            vehicle_id=vehicle["vehicle_id"],
                            position_id=vehicle["position_id"],
                        )
                    )
        if code:
            issues.append((None, None, code))
        for request, route, reason in set(issues):
            exception = FleetException(
                cycle_id=cycle.cycle_id,
                request_id=request,
                route_id=route,
                reported_by=actor,
                reason=reason,
                created_at=now,
            )
            session.add(exception)
            await session.flush([exception])
            await append_outbox_event(
                session,
                event_key=f"fleet-exception:{exception.exception_id}",
                aggregate_type="FleetCycle",
                aggregate_id=cycle.cycle_id,
                event_type="FleetPlanningAttentionRequired",
                payload={"exception_id": str(exception.exception_id)},
            )
        return current


async def _hold_unsafe_routes(
    factory: async_sessionmaker[AsyncSession],
    actor: UUID,
    issues: list[tuple[UUID | None, UUID | None, str]],
    snapshot: OperationalSnapshot,
    cycle: FleetCycle,
    config: ShadowConfiguration,
    now: datetime,
) -> None:
    expected_sources = deepcopy(snapshot.sources)
    # A safety hold preserves ownership and cargo; it is not an ordinary optimization revision.
    for _, route_id, reason in issues:
        if route_id is None:
            continue
        async with factory() as session, session.begin():
            root = await session.get(VehicleRoute, route_id)
            if root is None or root.status != "ACTIVE":
                continue
            assignment = await session.get(RiderAssignment, root.assignment_id)
            assert assignment is not None
            await require_live_role(session, actor, "MANAGER")
            await lock_resources(session, [assignment.rider_id], [root.vehicle_id], authorize=False)
            await session.refresh(root)
            control = await session.scalar(
                select(FleetControl).where(FleetControl.scope == SCOPE).with_for_update()
            )
            expected = next(
                (
                    r["version"]
                    for r in snapshot.sources["routes"] + snapshot.sources["orphaned_routes"]
                    if r["route_id"] == route_id
                ),
                None,
            )
            if (
                control is None
                or control.lease_token != cycle.lease_token
                or control.lease_until is None
                or control.lease_until <= now
                or control.version != cycle.control_version
            ):
                raise RouteAuthorityError("FLEET_LEASE_LOST")
            if reason == "FRESH_POSITION_REQUIRED":
                fresh = await session.scalar(
                    select(VehiclePosition.observed_at)
                    .where(VehiclePosition.vehicle_id == root.vehicle_id)
                    .order_by(VehiclePosition.revision.desc())
                    .limit(1)
                )
                if (
                    fresh is not None
                    and 0 <= (now - fresh).total_seconds() <= config.max_position_age_seconds
                ):
                    continue
            if root.status == "ACTIVE" and root.version == expected:
                from .snapshot import _read_sources

                current = await _read_sources(session, config)
                if source_digest(current, business_only=True) != source_digest(
                    expected_sources, business_only=True
                ):
                    continue
                root.status = "RECOVERY_REQUIRED"
                for expected_route in (
                    expected_sources["routes"] + expected_sources["orphaned_routes"]
                ):
                    if expected_route["route_id"] == route_id:
                        expected_route["status"] = "RECOVERY_REQUIRED"
                await append_outbox_event(
                    session,
                    event_key=f"fleet-safety-hold:{cycle.cycle_id}:{root.route_id}",
                    aggregate_type="VehicleRoute",
                    aggregate_id=root.route_id,
                    event_type="VehicleRouteRecoveryRequired",
                    payload={"route_id": str(root.route_id)},
                )


async def run_fleet_cycle(
    factory: async_sessionmaker[AsyncSession],
    *,
    providers: dict[str, MatrixSource],
    optimizers: dict[OptimizerEngine, RouteOptimizer],
    clock: Callable[[], datetime] = utc_now,
) -> FleetCycle | None:
    if set(optimizers) != set(OptimizerEngine):
        raise OperationalInputError("BOTH_ENGINES_REQUIRED")
    claimed = await _claim(factory, clock())
    if claimed is None:
        return None
    cycle, policy, actor = claimed
    config = policy.snapshot_configuration(clock())
    issues: list[tuple[UUID | None, UUID | None, str]] = []
    try:
        return await asyncio.wait_for(
            _plan(factory, cycle, policy, actor, config, providers, optimizers, issues, clock),
            timeout=policy.lease_seconds - 5,
        )
    except (OperationalInputError, RoutingAdapterError, RoutingValidationError) as error:
        return await _finish(
            factory, cycle, policy, actor, "FAILED", str(error), None, issues, clock()
        )
    except asyncio.TimeoutError:
        return await _finish(
            factory,
            cycle,
            policy,
            actor,
            "FAILED",
            "FLEET_DEADLINE_EXCEEDED",
            None,
            issues,
            clock(),
        )
    except Exception:
        return await _finish(
            factory,
            cycle,
            policy,
            actor,
            "FAILED",
            "FLEET_EVALUATION_FAILED",
            None,
            issues,
            clock(),
        )
    # Cancellation/kill deliberately retains the finite lease; a successor records abandonment.


async def _plan(
    factory: async_sessionmaker[AsyncSession],
    cycle: FleetCycle,
    policy: FleetPolicy,
    actor: UUID,
    config: ShadowConfiguration,
    providers: dict[str, MatrixSource],
    optimizers: dict[OptimizerEngine, RouteOptimizer],
    issues: list[tuple[UUID | None, UUID | None, str]],
    clock: Callable[[], datetime],
) -> FleetCycle:
    snapshot = await capture_snapshot(factory, config)
    if not await _should_evaluate(factory, snapshot, policy, clock()):
        return await _finish(
            factory, cycle, policy, actor, "UNCHANGED", None, None, issues, clock()
        )
    ready, readiness = ready_snapshot(snapshot, config, clock())
    issues.extend(readiness)
    await _hold_unsafe_routes(factory, actor, readiness, snapshot, cycle, config, clock())
    if any(route is not None for _, route, _ in readiness):
        # Holds are new business facts. Recapture before evaluating healthy resources.
        snapshot = await capture_snapshot(factory, config)
        ready, _ = ready_snapshot(snapshot, config, clock())
    fingerprint = source_digest(snapshot.sources, business_only=True)
    problem = await build_problem(ready, config, new_uuid7(), providers, now=clock())
    # Check the authoritative incumbent before invoking solvers; solver failure cannot
    # conceal a known unsafe route. Each resource is assessed separately to avoid
    # putting healthy routes on hold for another vehicle's exception.
    orders = incumbent_orders(ready)
    unsafe: list[tuple[UUID | None, UUID | None, str]] = []
    for vehicle in problem.vehicles:
        order = orders.get(vehicle.vehicle_id, ())
        if not order:
            continue
        lot_ids = {s.lot_id for s in problem.stops if s.stop_id in order}
        subproblem = replace(
            problem,
            vehicles=(vehicle,),
            lots=tuple(lot for lot in problem.lots if lot.lot_id in lot_ids),
            stops=tuple(
                replace(s, eligible_vehicle_ids=(vehicle.vehicle_id,))
                for s in problem.stops
                if s.lot_id in lot_ids
            ),
        )
        incumbent = RouteProposal(
            problem.problem_id,
            problem.snapshot_id,
            problem.contract_version,
            problem.policy_version,
            OptimizerEngine.VROOM,
            VROOM_VERSION,
            (ProposedRoute(vehicle.vehicle_id, order),),
            (),
        )
        try:
            validate_proposal(subproblem, incumbent)
        except RoutingValidationError as error:
            route = next(
                r for r in ready.sources["routes"] if r["vehicle_id"] == vehicle.vehicle_id
            )
            unsafe.append((None, route["route_id"], str(error)))
    if unsafe:
        issues.extend(unsafe)
        await _hold_unsafe_routes(factory, actor, unsafe, snapshot, cycle, config, clock())
        raise OperationalInputError("INCUMBENT_REQUIRES_RECOVERY")
    candidates: dict[OptimizerEngine, RouteProposal] = {}
    raw_results: dict[str, Any] = {}
    for engine in OptimizerEngine:
        try:
            proposal = await optimizers[engine].propose(
                problem, search_seconds=policy.search_seconds
            )
            if proposal.engine != engine or proposal.engine_version != VERSIONS[engine]:
                raise OperationalInputError("ENGINE_IDENTITY_MISMATCH")
            validate_proposal(problem, proposal)
            candidates[engine] = proposal
            raw_results[engine.value] = {
                "proposal": PROPOSAL_CODEC.dump_python(proposal, mode="json")
            }
        except (OperationalInputError, RoutingAdapterError, RoutingValidationError) as error:
            raw_results[engine.value] = {"failure_code": str(error)}
        except Exception:
            raw_results[engine.value] = {"failure_code": "ENGINE_FAILED"}
    async with factory() as session, session.begin():
        session.add(
            RoutingShadowRun(
                run_id=problem.problem_id,
                requested_by=actor,
                client_command_id=problem.problem_id,
                fingerprint=hashlib.sha256(config.model_dump_json().encode()).digest(),
                configuration=config.model_dump(mode="json"),
                status="SUCCEEDED" if len(candidates) == 2 else "FAILED",
                problem=PROBLEM_CODEC.dump_python(problem, mode="json"),
                source_fingerprint=snapshot.fingerprint,
                business_fingerprint=fingerprint,
                results=raw_results,
                failure_code=None if len(candidates) == 2 else "ENGINE_EVALUATION_FAILED",
                completed_at=clock(),
            )
        )
    if not candidates:
        raise OperationalInputError("NO_HEALTHY_ENGINE")
    for _ in range(policy.publication_attempts):
        current = await capture_snapshot(factory, config)
        if source_digest(current.sources, business_only=True) != fingerprint:
            return await _finish(
                factory,
                cycle,
                policy,
                actor,
                "STALE",
                "BUSINESS_SOURCE_CHANGED",
                None,
                issues,
                clock(),
            )
        live, _ = ready_snapshot(current, config, clock())
        rebased = await build_problem(live, config, new_uuid7(), providers, now=clock())
        if {(m.profile_version, m.graph_version) for m in problem.matrices} != {
            (m.profile_version, m.graph_version) for m in rebased.matrices
        }:
            raise OperationalInputError("ROAD_GRAPH_CHANGED")
        orders = incumbent_orders(live)
        measurements: dict[str, Any] = {"policy": policy.model_dump(mode="json")}
        viable: list[tuple[RouteProposal, FleetCost]] = []
        template = next(iter(candidates.values()))
        baseline = make_candidate(template, rebased, orders)
        try:
            baseline_cost = measure(rebased, baseline, policy.economics, orders)
            measurements["incumbent"] = asdict(baseline_cost)
            _, nearest_cost = nearest_feasible_baseline(rebased, baseline, policy.economics, orders)
            measurements["nearest_feasible_baseline"] = asdict(nearest_cost)
            greedy, greedy_cost, count = insertion_search(
                rebased, baseline, policy.economics, orders
            )
            measurements["collective_insertion_baseline"] = dict(
                asdict(greedy_cost), evaluations=count
            )
        except RoutingValidationError as error:
            # Do not blindly execute an incumbent invalidated by new physical/time facts.
            publication_unsafe: list[tuple[UUID | None, UUID | None, str]] = [
                (None, r["route_id"], str(error)) for r in live.sources["routes"]
            ]
            issues.extend(publication_unsafe)
            await _hold_unsafe_routes(
                factory, actor, publication_unsafe, current, cycle, config, clock()
            )
            raise OperationalInputError("INCUMBENT_REQUIRES_RECOVERY") from None
        results: dict[str, Any] = {}
        for engine, proposal in candidates.items():
            refreshed = replace(
                proposal, problem_id=rebased.problem_id, snapshot_id=rebased.snapshot_id
            )
            try:
                native_cost = measure(rebased, refreshed, policy.economics, orders)
                improved, cost, count = insertion_search(
                    rebased, refreshed, policy.economics, orders
                )
                if greedy_cost.rank < cost.rank:
                    improved = replace(greedy, engine=engine, engine_version=VERSIONS[engine])
                    cost = greedy_cost
                viable.append((improved, cost))
                results[engine.value] = {
                    "proposal": PROPOSAL_CODEC.dump_python(improved, mode="json"),
                    "construction": "NATIVE_SEED_AND_FEASIBLE_INSERTION",
                }
                measurements[engine.value] = dict(
                    asdict(cost), evaluations=count, native_seed=asdict(native_cost)
                )
            except RoutingValidationError as error:
                # A previously healthy native seed can cease to fit after movement. A
                # newly validated incumbent/insertion suffix is a safe fallback; a total
                # native failure above still never authorizes baseline-only dispatch.
                fallback = replace(greedy, engine=engine, engine_version=VERSIONS[engine])
                viable.append((fallback, greedy_cost))
                results[engine.value] = {
                    "proposal": PROPOSAL_CODEC.dump_python(fallback, mode="json"),
                    "construction": "LIVE_FEASIBLE_INSERTION_FALLBACK",
                    "seed_failure_code": str(error),
                }
                measurements[engine.value] = dict(
                    asdict(greedy_cost), native_seed_failure=str(error)
                )
        if not viable:
            raise OperationalInputError("NO_LIVE_FEASIBLE_PROPOSAL")
        selected, selected_cost = min(viable, key=lambda c: (c[1].rank, c[0].engine.value))
        selected_orders = {r.vehicle_id: r.stop_ids for r in selected.routes if r.stop_ids}
        for stop_id in selected.unserved_stop_ids:
            stop = next(s for s in rebased.stops if s.stop_id == stop_id)
            issues.append((stop.lot_id, None, "CAPACITY_OR_WINDOW_SHORTFALL"))
        if selected_orders == {v: order for v, order in orders.items() if order}:
            async with factory() as session, session.begin():
                stored_cycle = await session.get(FleetCycle, cycle.cycle_id)
                assert stored_cycle is not None
                stored_cycle.measurements = measurements
            return await _finish(
                factory, cycle, policy, actor, "UNCHANGED", None, current, issues, clock()
            )
        async with factory() as session, session.begin():
            session.add(
                RoutingShadowRun(
                    run_id=rebased.problem_id,
                    requested_by=actor,
                    client_command_id=rebased.problem_id,
                    fingerprint=hashlib.sha256(config.model_dump_json().encode()).digest(),
                    configuration=config.model_dump(mode="json"),
                    status="SUCCEEDED",
                    problem=PROBLEM_CODEC.dump_python(rebased, mode="json"),
                    source_fingerprint=source_digest(
                        current.sources,
                        position_vehicle_ids={v.vehicle_id for v in rebased.vehicles},
                    ),
                    business_fingerprint=fingerprint,
                    results=results,
                    completed_at=clock(),
                )
            )
            await session.flush()
            stored_cycle = await session.get(FleetCycle, cycle.cycle_id)
            assert stored_cycle is not None
            stored_cycle.run_id, stored_cycle.measurements = rebased.problem_id, measurements
            stored_cycle.selected_engine = selected.engine.value
        try:
            await activate_proposal(
                factory,
                manager_id=actor,
                command=ActivationCommand(
                    client_command_id=rebased.problem_id,
                    run_id=rebased.problem_id,
                    engine=selected.engine,
                    fleet_cycle_id=cycle.cycle_id,
                ),
                expires_at=clock() + timedelta(seconds=policy.lease_seconds),
                now=clock(),
                clock=clock,
            )
        except RouteAuthorityError as error:
            if str(error) in {
                "SOURCE_CHANGED",
                "RETRYABLE_TRANSACTION_CONFLICT",
                "FLEET_SELECTION_CHANGED_REBASE_REQUIRED",
            }:
                continue  # Rebase/validate current road positions, do not repeat long solver calls.
            raise
        return await _finish(
            factory, cycle, policy, actor, "ACTIVATED", None, current, issues, clock()
        )
    raise OperationalInputError("PUBLICATION_CONTENTION_RETRY_REQUIRED")
