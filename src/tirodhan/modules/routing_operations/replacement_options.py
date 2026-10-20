"""Bounded replacement alternatives under the existing regional builder/engines/gate.

Only immutable advisory witnesses are written. Neither these hypothetical problems nor
their proposals can be submitted to activation; selection wakes the ordinary regional
planner, which builds and validates actual current sources again.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.db.values import new_uuid7
from tirodhan.modules.planning.routing.contracts import (
    OptimizerEngine,
    ProposedRoute,
    RouteOptimizer,
    RouteProposal,
    RoutingProblem,
)
from tirodhan.modules.planning.routing.execution import RoutingAdapterError
from tirodhan.modules.planning.routing.validation import RoutingValidationError, validate_proposal

from .admission_models import AdmissionSourceHead
from .admission_state import read_cache
from .capacity_models import RiderReplacement
from .commands import OperationalInputError
from .economics import measure
from .orchestration_models import OperatingRegion
from .parallel import FacilityLimit, consolidate, selected_routes
from .parallel import candidate as route_candidate
from .regional_planner import regional_ready, subset
from .replacement_models import ReplacementCandidate, ReplacementEvaluation
from .rolling import FleetPolicy, incumbent_orders
from .service import PROBLEM_CODEC, PROPOSAL_CODEC
from .snapshot import MatrixSource, OperationalSnapshot, _read_sources, build_problem, source_digest


def pin_existing(
    problem: RoutingProblem, sources: dict[str, Any], rescued: set[UUID], target: UUID
) -> RoutingProblem:
    owners = {v["rider_id"]: v["vehicle_id"] for v in sources["vehicles"]}
    bound = {
        r["request_id"]: owners.get(
            r.get("collector_id") if r.get("pickup_status") == "COLLECTED" else r["owner_id"]
        )
        for r in sources["requests"]
    }
    return replace(
        problem,
        stops=tuple(
            replace(
                s,
                bound_vehicle_id=target
                if s.lot_id in rescued
                else bound.get(s.lot_id) or s.bound_vehicle_id,
                eligible_vehicle_ids=(target,) if s.lot_id in rescued else s.eligible_vehicle_ids,
                required=True,
            )
            for s in problem.stops
        ),
    )


def check_scope_budget(sources: dict[str, Any], policy: FleetPolicy) -> None:
    # Check before quadratic matrix acquisition. Every obligation remains in the
    # scope: exceeding a bound is UNKNOWN, never permission to prune committed work.
    nodes = {v["position_id"] for v in sources["vehicles"] if v["position_id"] is not None}
    nodes.update(p["receiving_point_id"] for p in sources["points"])
    nodes.update(r["request_id"] for r in sources["requests"] if r["pickup_status"] != "COLLECTED")
    if (
        len(nodes) > policy.max_nodes
        or len(sources["vehicles"]) > policy.max_vehicles
        or len({v["profile_version"] for v in sources["vehicles"]}) > policy.max_profiles
    ):
        raise OperationalInputError("REPLACEMENT_SCOPE_BUDGET")


def coordinate(
    problem: RoutingProblem,
    proposal: Any,
    sources: dict[str, Any],
    policy: FleetPolicy,
    orders: Any,
) -> None:
    """Reuse regional facility-concurrency arbitration, including external route visits."""
    pool = [
        route_candidate(
            problem, r, proposal.engine.value, proposal.engine_version, policy.economics, orders
        )
        for r in (
            *proposal.routes,
            *(
                ProposedRoute(v.vehicle_id, ())
                for v in problem.vehicles
                if v.vehicle_id not in {r.vehicle_id for r in proposal.routes}
            ),
        )
    ]
    limits = tuple(
        FacilityLimit(
            receiving_point_id=p["receiving_point_id"], concurrent_unloads=p["unloading_capacity"]
        )
        for p in sources["points"]
        if p.get("unloading_capacity") is not None
    )
    external = tuple(
        (
            v["receiving_point_id"],
            int((v["planned_start"] - problem.epoch).total_seconds()),
            int((v["planned_end"] - problem.epoch).total_seconds()),
        )
        for v in sources.get("external_facility_visits", [])
    )
    try:
        result = consolidate(
            pool,
            {v.vehicle_id for v in problem.vehicles},
            {s.lot_id for s in problem.stops},
            {},
            policy_version=problem.policy_version,
            seconds=1,
            facility_limits=limits,
            external_visits=external,
        )
        selected_routes(result)
    except ImportError:
        raise RoutingAdapterError("UNKNOWN_COORDINATOR_UNAVAILABLE") from None
    except ValueError:
        raise RoutingAdapterError("UNKNOWN_FACILITY_COORDINATION") from None


def incumbent_cost(
    problem: RoutingProblem, rescued: set[UUID], orders: Any, policy: FleetPolicy
) -> Any:
    """Measure the surviving incumbent only when independently feasible at this epoch.

    This is a comparison baseline, never permission to omit rescued obligations
    from the replacement witness. Missing/unplanned incumbent work makes the delta
    unavailable rather than inventing a zero baseline.
    """
    remaining = {s.stop_id for s in problem.stops if s.lot_id not in rescued}
    baseline = replace(
        problem,
        lots=tuple(lot for lot in problem.lots if lot.lot_id not in rescued),
        stops=tuple(s for s in problem.stops if s.stop_id in remaining),
        vehicles=tuple(
            replace(
                v,
                initial_cargo=tuple(c for c in v.initial_cargo if c.lot_id not in rescued),
                protected_prefix=tuple(s for s in v.protected_prefix if s in remaining),
            )
            for v in problem.vehicles
        ),
    )
    proposal = RouteProposal(
        problem.problem_id,
        problem.snapshot_id,
        problem.contract_version,
        problem.policy_version,
        OptimizerEngine.OR_TOOLS,
        "incumbent-comparison",
        tuple(
            ProposedRoute(
                v.vehicle_id, tuple(s for s in orders.get(v.vehicle_id, ()) if s in remaining)
            )
            for v in baseline.vehicles
        ),
        (),
    )
    try:
        return measure(baseline, proposal, policy.economics, orders)
    except RoutingValidationError:
        return None


async def evaluate_options(
    factory: async_sessionmaker[AsyncSession],
    replacement_id: UUID,
    *,
    providers: dict[str, MatrixSource],
    optimizers: dict[OptimizerEngine, RouteOptimizer],
    now: datetime,
    max_wall_seconds: float = 30,
) -> bool:
    async with factory() as session:
        await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        request = await session.get(RiderReplacement, replacement_id)
        if request is None or request.status not in {"REQUESTED", "APPROVED"}:
            return False
        if request.status == "APPROVED" and request.failure_code != "RECOMMENDATION_REQUESTED":
            return False
        region = await session.get(OperatingRegion, request.region_id)
        head = await session.get(AdmissionSourceHead, request.region_id)
        assert region and head
        policy = FleetPolicy.model_validate(region.policy["fleet"])
        config = policy.snapshot_configuration(now, region_id=region.region_id, admission_mode=True)
        config = config.model_copy(update={"confirmed_shifts": True})
        sources = await _read_sources(session, config)
        group = (
            list(
                await session.scalars(
                    select(RiderReplacement).where(
                        RiderReplacement.incident_id == request.incident_id,
                        RiderReplacement.status.in_(("REQUESTED", "RECOMMENDED", "APPROVED")),
                    )
                )
            )
            if request.incident_id
            else [request]
        )
        if request not in group:
            group.append(request)
        group_versions = {r.replacement_id: r.version for r in group}
        identities = {r.request_id for r in group}
        source_version, request_version, region_version = (
            head.version,
            request.version,
            region.version,
        )
    original = {r["request_id"]: r for r in sources["requests"] if r["request_id"] in identities}
    ready, issues = regional_ready(
        OperationalSnapshot(sources, source_digest(sources)), config, now
    )
    deadline = time.monotonic() + max_wall_seconds
    exclusions: list[dict[str, Any]] = []
    options: list[tuple[Any, dict[str, Any]]] = []
    roads = read_cache(factory).roads(providers)
    evaluated = 0
    prerequisites = len(original) == len(identities) and not any(
        rid not in identities for rid, _, _ in issues if rid is not None
    )
    if any(r.transfer_intent_id is not None for r in group):
        prerequisites = False
        exclusions.append({"outcome": "RESOLVE_PHYSICAL_TRANSFER_BEFORE_GROUP_RECOMMENDATION"})
    rescued_stops = {
        uuid5(NAMESPACE_URL, f"tirodhan:{kind}:{identity}")
        for identity in identities
        for kind in ("pickup", "discharge")
    }
    if any(t["state"] == "STARTED" and t["stop_id"] in rescued_stops for t in sources["tasks"]):
        prerequisites = False
    vehicles = sorted(ready.sources["vehicles"], key=lambda v: str(v["vehicle_id"]))
    for vehicle in vehicles if prerequisites else []:
        if time.monotonic() >= deadline or evaluated >= policy.max_vehicles:
            break
        if vehicle["rider_id"] in {r.original_rider_id for r in group}:
            continue
        if vehicle["resource_kind"] != "COMMITTED" and not identities <= set(
            vehicle.get("accepted_requests", ())
        ):
            exclusions.append(
                {"shift_id": str(vehicle["shift_id"]), "outcome": "VOLUNTARY_ACCEPTANCE_REQUIRED"}
            )
            continue
        evaluated += 1
        projected = subset(ready, {r["request_id"] for r in ready.sources["requests"]})
        projected.sources["requests"] = [
            r for r in projected.sources["requests"] if r["request_id"] not in identities
        ]
        handoffs = []
        for identity in sorted(identities):
            row = dict(original[identity], owner_id=None)
            row.pop("replacement_shift_id", None)
            if row["pickup_status"] == "COLLECTED":
                donor = next(
                    (v for v in sources["vehicles"] if v["vehicle_id"] == row["cargo_vehicle_id"]),
                    None,
                )
                if (
                    donor is None
                    or donor["position_id"] is None
                    or not (
                        0
                        <= (now - donor["observed_at"]).total_seconds()
                        <= policy.telemetry.max_age_seconds
                    )
                ):
                    prerequisites = False
                    break
                # Explicit hypothetical load acquisition at the failed vehicle's observed
                # position, using existing conservative handling time. It is NOT a new
                # household pickup or custody fact. Travel to the handoff must be feasible.
                row.update(
                    pickup_status="PENDING_ASSIGNMENT",
                    collector_id=None,
                    cargo_vehicle_id=None,
                    longitude=donor["longitude"],
                    latitude=donor["latitude"],
                    slot_start=now,
                    slot_end=config.horizon_end,
                )
                handoffs.append(str(identity))
            projected.sources["requests"].append(row)
        if not prerequisites:
            break
        # The hypothetical recovery removes only rescued source tasks. Other protected
        # work remains in the actual regional builder and is independently checked.
        projected.sources["tasks"] = [
            t for t in projected.sources["tasks"] if t["stop_id"] not in rescued_stops
        ]
        try:
            check_scope_budget(projected.sources, policy)
            problem = await asyncio.wait_for(
                build_problem(projected, config, new_uuid7(), roads, now=now),
                timeout=max(0.001, deadline - time.monotonic()),
            )
            if any(len(m.node_ids) > policy.max_nodes for m in problem.matrices):
                raise OperationalInputError("REPLACEMENT_NODE_BUDGET")
            problem = pin_existing(problem, projected.sources, identities, vehicle["vehicle_id"])
            orders = incumbent_orders(projected)
            baseline = incumbent_cost(problem, identities, orders, policy)
            successful = []
            attempts = []
            for engine in OptimizerEngine:
                if time.monotonic() >= deadline:
                    attempts.append({"engine": engine.value, "outcome": "NOT_INVOKED_TIME_BUDGET"})
                    continue
                try:
                    proposal = await asyncio.wait_for(
                        optimizers[engine].propose(
                            problem,
                            search_seconds=min(
                                policy.search_seconds, max(1, int(deadline - time.monotonic()))
                            ),
                        ),
                        timeout=max(0.001, deadline - time.monotonic()),
                    )
                    assessment = validate_proposal(problem, proposal)
                    coordinate(problem, proposal, projected.sources, policy, orders)
                    cost = measure(problem, proposal, policy.economics, orders)
                    successful.append((cost.rank, engine.value, proposal, assessment, cost))
                    attempts.append({"engine": engine.value, "outcome": "VALIDATED"})
                except (
                    RoutingAdapterError,
                    RoutingValidationError,
                    OperationalInputError,
                    asyncio.TimeoutError,
                ):
                    attempts.append(
                        {"engine": engine.value, "outcome": "UNKNOWN_NO_VALIDATED_CONTINUATION"}
                    )
            if not successful:
                exclusions.append(
                    {
                        "shift_id": str(vehicle["shift_id"]),
                        "outcome": "UNKNOWN",
                        "engines": attempts,
                    }
                )
                continue
            _, _, proposal, assessment, cost = min(successful, key=lambda x: (x[0], x[1]))
            route = next(r for r in assessment.routes if r.vehicle_id == vehicle["vehicle_id"])
            existing = sorted(
                str(r["request_id"])
                for r in projected.sources["requests"]
                if (r["collector_id"] if r["pickup_status"] == "COLLECTED" else r["owner_id"])
                == vehicle["rider_id"]
            )
            summary = {
                "rider_id": str(vehicle["rider_id"]),
                "vehicle_id": str(vehicle["vehicle_id"]),
                "shift_id": str(vehicle["shift_id"]),
                "region_id": str(request.region_id),
                "resource_kind": vehicle["resource_kind"],
                "existing_bookings": existing,
                "affected_bookings": sorted(map(str, identities)),
                "physical_handoff_required": handoffs,
                "handoff_basis": "ACTUAL_DONOR_POSITION_AND_EXISTING_HANDLING_TIME",
                "current_cargo_lots": [
                    str(c.lot_id)
                    for v in problem.vehicles
                    if v.vehicle_id == vehicle["vehicle_id"]
                    for c in v.initial_cargo
                ],
                "capacity": list(
                    next(
                        v.capacity
                        for v in problem.vehicles
                        if v.vehicle_id == vehicle["vehicle_id"]
                    )
                ),
                "proposed_stops": [asdict(s) for s in route.stops],
                "engine_attempts": attempts,
                "cost": asdict(cost),
                "incremental_impact": {
                    "baseline": "FEASIBLE_SURVIVING_INCUMBENT",
                    "travel_seconds": cost.travel_seconds - baseline.travel_seconds,
                    "distance_m": cost.distance_m - baseline.distance_m,
                    "generalized_seconds": cost.generalized_seconds - baseline.generalized_seconds,
                }
                if baseline
                else {"baseline": "UNAVAILABLE_OR_NOT_CURRENTLY_FEASIBLE"},
                "existing_commitments_preserved": True,
                "route_versions": [
                    {"route_id": str(r["route_id"]), "version": r["version"]}
                    for r in sources["routes"]
                ],
                "validation": "REGIONAL_GATE_AND_COORDINATOR",
            }
            rank = (
                int(vehicle["resource_kind"] != "COMMITTED"),
                cost.rank,
                str(vehicle["shift_id"]),
            )
            options.append(
                (
                    rank,
                    {
                        "shift_id": vehicle["shift_id"],
                        "problem": PROBLEM_CODEC.dump_python(problem, mode="json"),
                        "proposal": PROPOSAL_CODEC.dump_python(proposal, mode="json"),
                        "summary": json.loads(json.dumps(summary, default=str)),
                    },
                )
            )
        except (
            OperationalInputError,
            RoutingAdapterError,
            RoutingValidationError,
            asyncio.TimeoutError,
        ):
            exclusions.append(
                {"shift_id": str(vehicle["shift_id"]), "outcome": "UNKNOWN_INPUT_OR_PROVIDER"}
            )
    options.sort(key=lambda x: x[0])
    incomplete = (
        not prerequisites or time.monotonic() >= deadline or evaluated >= policy.max_vehicles
    )
    evaluation_id = new_uuid7()
    async with factory() as session, session.begin():
        current_rows = list(
            await session.scalars(
                select(RiderReplacement)
                .where(RiderReplacement.replacement_id.in_(group_versions))
                .order_by(RiderReplacement.replacement_id)
                .with_for_update()
            )
        )
        current_request = next(
            (r for r in current_rows if r.replacement_id == replacement_id), None
        )
        head = await session.get(AdmissionSourceHead, request.region_id, with_for_update=True)
        if (
            current_request is None
            or current_request.version != request_version
            or any(r.version != group_versions[r.replacement_id] for r in current_rows)
            or len(current_rows) != len(group_versions)
            or head is None
            or head.version != source_version
        ):
            return False
        evaluation = ReplacementEvaluation(
            evaluation_id=evaluation_id,
            replacement_id=replacement_id,
            replacement_version=request_version,
            region_version=region_version,
            source_version=source_version,
            source_fingerprint=source_digest(sources),
            created_at=now,
            expires_at=now + timedelta(seconds=min(300, policy.lease_seconds)),
            outcome="VALIDATED_OPTIONS" if options else "UNKNOWN",
            search={
                "evaluated_vehicles": evaluated,
                "bounded": True,
                "exhaustive": False,
                "incomplete": incomplete,
                "max_wall_seconds": max_wall_seconds,
                "exclusions": exclusions,
                "replacement_versions": {str(k): v for k, v in group_versions.items()},
                "input_source_version": source_version,
            },
        )
        for current_request in current_rows:
            current_request.evaluation_id = evaluation_id
            if options:
                if current_request.status != "APPROVED":
                    current_request.source_version = source_version
                    current_request.recommended_shift_id = options[0][1]["shift_id"]
                    current_request.status = "RECOMMENDED"
                current_request.recommended_at, current_request.failure_code = now, None
            else:
                current_request.failure_code = "UNKNOWN_REPLACEMENT_SEARCH"
                current_request.attempt += 1
                current_request.next_attempt_at = now + timedelta(
                    seconds=min(60, 5 * 2 ** min(current_request.attempt, 4))
                )
            current_request.version += 1
        # Existing APPROVED updates invalidate the source head. Keep that safeguard:
        # publish the witness against our resulting generation, under the same head
        # lock, without changing the previously selected operational target. The
        # deferred concrete FK permits this atomic pointer-before-evaluation insert.
        await session.flush()
        await session.refresh(head)
        evaluation.source_version = head.version
        session.add(evaluation)
        await session.flush()
        for position, (_, data) in enumerate(options, 1):
            session.add(
                ReplacementCandidate(
                    candidate_id=new_uuid7(), evaluation_id=evaluation_id, rank=position, **data
                )
            )
        await session.flush()
    return bool(options)
