"""Localized fixed-order repair. Results have no assignment or custody authority."""

from __future__ import annotations

import time
from dataclasses import replace
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tirodhan.modules.planning.routing.contracts import (
    ProposedRoute,
    RouteProposal,
    RoutingProblem,
    SelectionEngine,
)
from tirodhan.modules.planning.routing.validation import RoutingValidationError, validate_proposal

from .dispatch_models import RouteRevision, RouteRevisionStop, RouteTask, VehicleRoute
from .economics import measure
from .rolling import FleetPolicy, incumbent_orders
from .snapshot import OperationalSnapshot


async def local_sources(
    session: AsyncSession, sources: dict[str, Any], target: UUID, rescued: set[UUID]
) -> dict[str, Any]:
    """Retain every target obligation; other routes are external facility occupancy.

    Source dictionaries are copied, never edited in place. Actual source fingerprints
    are compared again under the shared authority locks before a localized commit.
    """
    vehicle = next((v for v in sources["vehicles"] if v["vehicle_id"] == target), None)
    if vehicle is None:
        from .authority import RouteAuthorityError

        raise RouteAuthorityError("ACTIVE_SHIFT_REQUIRED")
    identities = rescued | {
        r["request_id"]
        for r in sources["requests"]
        if (r.get("collector_id") if r["pickup_status"] == "COLLECTED" else r["owner_id"])
        == vehicle["rider_id"]
        or r.get("replacement_shift_id") == vehicle["shift_id"]
    }
    result = dict(sources)
    result["vehicles"] = [dict(vehicle)]
    result["requests"] = [dict(r) for r in sources["requests"] if r["request_id"] in identities]
    result["routes"] = [r for r in sources["routes"] if r["vehicle_id"] == target]
    route_ids = {r["route_id"] for r in result["routes"]}
    result["tasks"] = [t for t in sources["tasks"] if t["route_id"] in route_ids]
    result["orphaned_routes"] = [
        r for r in sources.get("orphaned_routes", []) if r["vehicle_id"] == target
    ]
    from .capacity_models import RiderReplacement

    replacements = set(
        await session.scalars(
            select(RiderReplacement.replacement_id).where(
                RiderReplacement.request_id.in_(identities)
            )
        )
    )
    from tirodhan.modules.collection_requests.models import CollectionRequest

    cells = set(
        await session.scalars(
            select(CollectionRequest.cell_id).where(CollectionRequest.request_id.in_(identities))
        )
    )
    point_ids = {r["receiving_point_id"] for r in result["requests"]} | {
        vehicle["end_receiving_point_id"]
    }
    result["points"] = [p for p in sources["points"] if p["receiving_point_id"] in point_ids]
    result["windows"] = [w for w in sources["windows"] if w["receiving_point_id"] in point_ids]
    result["restrictions"] = [
        r for r in sources["restrictions"] if r["receiving_point_id"] in point_ids
    ]
    result["released"] = [r for r in sources["released"] if r["vehicle_id"] == target]
    from .orchestration_models import RegionVehicle

    membership = await session.get(RegionVehicle, target)
    inputs = dict(sources.get("operational_inputs", {}))
    inputs["recovery_owner_version"] = membership.version if membership else None
    for key, values in list(inputs.items()):
        if key == "replacements":
            inputs[key] = [r for r in values if r["replacement_id"] in replacements]
        elif key == "region_cells":
            inputs[key] = [r for r in values if r[0] in cells]
        elif key == "region_vehicles":
            inputs[key] = [r for r in values if r[0] == str(target)]
        elif key in ("breaks", "extensions", "flexible_commitments", "flexible_admissions"):
            inputs[key] = [r for r in values if r.get("shift_id") == vehicle["shift_id"]]
        elif (
            isinstance(values, list)
            and values
            and isinstance(values[0], dict)
            and "request_id" in values[0]
        ):
            inputs[key] = [r for r in values if r.get("request_id") in identities]
    result["operational_inputs"] = inputs
    # Query all current discharge visits outside the repaired vehicle, including
    # other operating regions sharing a facility. Current revision joins exclude history.
    visits = (
        await session.execute(
            select(
                RouteTask.receiving_point_id,
                RouteRevisionStop.planned_start,
                RouteRevisionStop.planned_end,
            )
            .join(RouteRevisionStop, RouteRevisionStop.task_id == RouteTask.task_id)
            .join(RouteRevision, RouteRevision.revision_id == RouteRevisionStop.revision_id)
            .join(VehicleRoute, VehicleRoute.route_id == RouteTask.route_id)
            .where(
                RouteTask.receiving_point_id.in_(point_ids),
                VehicleRoute.vehicle_id != target,
                VehicleRoute.status != "CLOSED",
                RouteRevision.version == VehicleRoute.version,
                RouteTask.kind == "DISCHARGE",
                RouteTask.state.in_(("PENDING", "STARTED")),
            )
            .order_by(RouteTask.task_id)
        )
    ).all()
    result["external_facility_visits"] = [
        dict(receiving_point_id=p, planned_start=a, planned_end=b) for p, a, b in visits
    ]
    return result


def repair(
    problem: RoutingProblem,
    sources: dict[str, Any],
    rescued: set[UUID],
    policy: FleetPolicy,
    seconds: float = 5,
) -> RouteProposal:
    """Fixed incumbent order plus bounded pickup/discharge insertion.

    Enumerate precedence-respecting positions. A failed bounded search is UNKNOWN.
    Both approved native engines remain available to the caller as bounded fallback.
    """
    vehicle = problem.vehicles[0]
    by_stop = {s.stop_id: s for s in problem.stops}
    order = tuple(
        s
        for s in incumbent_orders(OperationalSnapshot(sources, b"")).get(vehicle.vehicle_id, ())
        if s in by_stop and by_stop[s].lot_id not in rescued
    )
    deadline = time.monotonic() + seconds
    evaluations = 0
    for identity in sorted(rescued, key=str):
        additions = tuple(s.stop_id for s in problem.stops if s.lot_id == identity)
        if not additions:
            continue
        best = None
        # One discharge for actual onboard cargo; two ordered visits before pickup.
        additions = tuple(sorted(additions, key=lambda s: by_stop[s].kind.value != "PICKUP"))
        for i in range(len(vehicle.protected_prefix), len(order) + 1):
            first = order[:i] + additions[:1] + order[i:]
            positions = range(i + 1, len(first) + 1) if len(additions) > 1 else (None,)
            for j in positions:
                if (
                    evaluations >= policy.economics.max_candidate_evaluations
                    or time.monotonic() >= deadline
                ):
                    raise RoutingValidationError("UNKNOWN_RECOVERY_INSERTION_BUDGET")
                evaluations += 1
                changed = first if j is None else first[:j] + additions[1:] + first[j:]
                present = set(changed)
                partial = replace(
                    problem,
                    stops=tuple(s for s in problem.stops if s.stop_id in present),
                    lots=tuple(
                        lot
                        for lot in problem.lots
                        if any(
                            s.lot_id == lot.lot_id for s in problem.stops if s.stop_id in present
                        )
                    ),
                )
                proposal = RouteProposal(
                    problem.problem_id,
                    problem.snapshot_id,
                    problem.contract_version,
                    problem.policy_version,
                    SelectionEngine.LOCAL_REPAIR,
                    "LOCAL_PAIR_INSERTION_V1",
                    (ProposedRoute(vehicle.vehicle_id, changed),),
                    (),
                )
                try:
                    cost = measure(partial, proposal, policy.economics, {})
                except RoutingValidationError:
                    continue
                if best is None or cost.rank < best[0]:
                    best = (cost.rank, changed)
        if best is None:
            raise RoutingValidationError("UNKNOWN_NO_LOCAL_CONTINUATION")
        order = best[1]
    proposal = RouteProposal(
        problem.problem_id,
        problem.snapshot_id,
        problem.contract_version,
        problem.policy_version,
        SelectionEngine.LOCAL_REPAIR,
        "LOCAL_PAIR_INSERTION_V1",
        (ProposedRoute(vehicle.vehicle_id, order),),
        (),
    )
    validate_proposal(problem, proposal)
    return proposal


async def donor_positions(session: AsyncSession, vehicle_ids: set[UUID]) -> dict[str, Any]:
    """Actual append-only positions, independently of duty eligibility. Never supply."""
    from .models import VehiclePosition
    from .snapshot import _coordinates

    rows = (
        await session.execute(
            select(
                VehiclePosition.vehicle_id,
                VehiclePosition.position_id,
                VehiclePosition.observed_at,
                VehiclePosition.revision,
                *_coordinates(VehiclePosition.location),
            )
            .where(VehiclePosition.vehicle_id.in_(vehicle_ids))
            .distinct(VehiclePosition.vehicle_id)
            .order_by(VehiclePosition.vehicle_id, VehiclePosition.revision.desc())
        )
    ).mappings()
    return {str(row["vehicle_id"]): dict(row) for row in rows}
