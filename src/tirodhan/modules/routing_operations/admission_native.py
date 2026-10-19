"""Dense, complete-route native neighbourhoods over a sparse regional witness.

Neighbourhood size is a byte budget, never a regional booking limit. Failure to
find a witness in a neighbourhood is UNKNOWN, not proof of exhausted capacity.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any
from uuid import UUID

from tirodhan.modules.planning.routing.contracts import (
    ResourceKind,
    RoadArc,
    RoutingLimits,
    RoutingProblem,
)
from tirodhan.modules.planning.routing.execution import RoutingAdapterError
from tirodhan.modules.planning.routing.osrm import RoadNode

from .admission_roads import DirectedRoadCache


async def native_neighbourhoods(
    problem: RoutingProblem,
    orders: dict[UUID, tuple[UUID, ...]],
    lot_id: UUID,
    sources: dict[str, Any],
    providers: dict[str, DirectedRoadCache],
) -> AsyncIterator[tuple[RoutingProblem, RoutingProblem, set[UUID]]]:
    stops = {s.stop_id: s for s in problem.stops}
    lots_by_vehicle = {
        v.vehicle_id: {stops[s].lot_id for s in orders.get(v.vehicle_id, ()) if s in stops}
        | {c.lot_id for c in v.initial_cargo}
        for v in problem.vehicles
    }
    covered = set().union(*lots_by_vehicle.values())
    unowned = {lot.lot_id for lot in problem.lots} - covered - {lot_id}
    pickup = next(s for s in problem.stops if s.lot_id == lot_id)
    nodes = {
        r["request_id"]: RoadNode(r["request_id"], r["longitude"], r["latitude"])
        for r in sources["requests"]
    }
    nodes.update(
        {
            v["position_id"]: RoadNode(v["position_id"], v["longitude"], v["latitude"])
            for v in sources["vehicles"]
        }
    )
    nodes.update(
        {
            p["receiving_point_id"]: RoadNode(
                p["receiving_point_id"], p["longitude"], p["latitude"]
            )
            for p in sources["points"]
        }
    )
    vehicles = sorted(
        problem.vehicles,
        key=lambda v: (
            v.resource_kind != ResourceKind.COMMITTED,
            len(orders.get(v.vehicle_id, ())),
            v.vehicle_id.int,
        ),
    )
    # A cold legacy witness must cover every unowned accepted obligation together.
    # Resource closures cannot be silently omitted to make a smaller model feasible.
    groups: list[list[UUID]] = []
    current: list[UUID] = []
    for vehicle in vehicles:
        if vehicle.vehicle_id not in pickup.eligible_vehicle_ids and not unowned:
            continue
        trial = current + [vehicle.vehicle_id]
        ids = {lot_id} | unowned | set().union(*(lots_by_vehicle[v] for v in trial))
        required = {s.node_id for s in problem.stops if s.lot_id in ids}
        required.update(
            n
            for v in problem.vehicles
            if v.vehicle_id in trial
            for n in (v.start_node_id, v.end_node_id)
        )
        budget = min(p.max_bytes for p in providers.values())
        if current and len(required) ** 2 * 80 > budget:
            groups.append(current)
            current = [vehicle.vehicle_id]
        else:
            current = trial
    if current:
        groups.append(current)
    if unowned:
        groups = [[v.vehicle_id for v in vehicles]]
    skipped = False
    for group in groups:
        chosen = set(group)
        ids = {lot_id} | unowned | set().union(*(lots_by_vehicle[v] for v in group))
        local_stops = tuple(
            replace(s, eligible_vehicle_ids=tuple(v for v in s.eligible_vehicle_ids if v in chosen))
            for s in problem.stops
            if s.lot_id in ids
        )
        local_vehicles = tuple(v for v in problem.vehicles if v.vehicle_id in chosen)
        local_lots = tuple(lot for lot in problem.lots if lot.lot_id in ids)
        point_ids = {p for lot in local_lots for p in lot.authorized_receiving_point_ids}
        local_points = tuple(
            p for p in problem.receiving_points if p.receiving_point_id in point_ids
        )
        required = {s.node_id for s in local_stops}
        required.update(n for v in local_vehicles for n in (v.start_node_id, v.end_node_id))
        required.update(p.node_id for p in local_points)
        local_nodes = tuple(nodes[key] for key in sorted(required, key=str))
        used_profiles = {
            m.profile_version
            for m in problem.matrices
            if m.matrix_id in {v.matrix_id for v in local_vehicles}
        }
        if any(len(required) ** 2 * 80 > providers[p].max_bytes for p in used_profiles):
            skipped = True
            continue
        matrices = []
        augmented = []
        used = {v.matrix_id for v in local_vehicles}
        for matrix in problem.matrices:
            if matrix.matrix_id not in used:
                augmented.append(matrix)
                continue
            provider = providers[matrix.profile_version]
            dense = await provider.acquire(local_nodes)
            if (dense.graph_version, dense.profile_version) != (
                matrix.graph_version,
                matrix.profile_version,
            ):
                raise RoutingAdapterError("MATRIX_PROVENANCE_CHANGED")
            matrices.append(replace(dense, matrix_id=matrix.matrix_id))
            arcs = {(a.origin, a.destination): a for a in matrix.sparse_arcs or ()}
            index = {node: i for i, node in enumerate(dense.node_ids)}
            for a in dense.node_ids:
                for b in dense.node_ids:
                    arcs[(a, b)] = RoadArc(a, b, *dense.arc(a, b, index))
            augmented.append(
                replace(
                    matrix,
                    sparse_arcs=tuple(
                        arcs[key] for key in sorted(arcs, key=lambda p: (p[0].int, p[1].int))
                    ),
                )
            )
        local = replace(
            problem,
            vehicles=local_vehicles,
            stops=local_stops,
            lots=local_lots,
            receiving_points=local_points,
            matrices=tuple(matrices),
            limits=RoutingLimits(
                max(1, len(required)),
                max(1, len(local_stops)),
                max(1, len(group)),
                max(1, len(matrices)),
            ),
        )
        yield replace(problem, matrices=tuple(augmented)), local, chosen

    if skipped:
        raise RoutingAdapterError("UNKNOWN_MODEL_MEMORY_BUDGET")
