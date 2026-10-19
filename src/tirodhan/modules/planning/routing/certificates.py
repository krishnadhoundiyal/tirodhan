"""Independent route proofs and exact product composition (no shared facility quota).

Proof reuse requires equality of hard terms/used directed arcs, not solver claims.
Backward bounds below are deliberately independent of insertion's interval machinery.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from uuid import UUID

from pydantic import ConfigDict, TypeAdapter

from .contracts import (
    ROUTING_CONTRACT_V3,
    CargoLot,
    OptimizerEngine,
    ReceivingPointSnapshot,
    RoadArc,
    RoadMatrix,
    RouteProposal,
    RoutingLimits,
    RoutingProblem,
    RoutingStop,
    SelectionEngine,
    StopKind,
    TimeWindow,
    VehicleSnapshot,
)
from .validation import RouteAssessment, RoutingValidationError, validate_problem, validate_proposal


def _latest_action(end: int, duration: int, breaks: tuple[TimeWindow, ...]) -> int:
    start = end - duration
    for pause in reversed(breaks):
        if start < pause.end and start + duration > pause.start:
            start = pause.start - duration
    return start


def latest_route_entry(
    problem: RoutingProblem, vehicle: VehicleSnapshot, order: tuple[UUID, ...]
) -> int:
    stops = {s.stop_id: s for s in problem.stops}
    lots = {lot.lot_id: lot for lot in problem.lots}
    facilities = {p.receiving_point_id: p for p in problem.receiving_points}
    matrix = next(m for m in problem.matrices if m.matrix_id == vehicle.matrix_id)
    indices = {node: i for i, node in enumerate(matrix.node_ids)}
    target, limit = vehicle.end_node_id, vehicle.shift.end
    for identity in reversed(order):
        stop = stops[identity]
        seconds, _ = matrix.arc(stop.node_id, target, indices)
        if seconds is None:
            raise RoutingValidationError("UNREACHABLE_ARC")
        finish = _latest_action(limit, seconds, vehicle.breaks)
        start = min(stop.service_start_window.end, finish - stop.service_seconds)
        if stop.kind == StopKind.DISCHARGE:
            point = facilities[stop.receiving_point_id]  # type: ignore[index]
            start = min(start, lots[stop.lot_id].discharge_deadline - stop.service_seconds)
            feasible = []
            for w in reversed(point.opening_windows):
                s = _latest_action(
                    min(start + stop.service_seconds, w.end), stop.service_seconds, vehicle.breaks
                )
                if w.start <= s and s + stop.service_seconds <= w.end:
                    feasible.append(s)
            start = max(feasible, default=-1)
        else:
            start = _latest_action(
                start + stop.service_seconds, stop.service_seconds, vehicle.breaks
            )
        if start < stop.service_start_window.start:
            raise RoutingValidationError("SERVICE_WINDOW_MISSED")
        target, limit = stop.node_id, start
    seconds, _ = matrix.arc(vehicle.start_node_id, target, indices)
    if seconds is None:
        raise RoutingValidationError("UNREACHABLE_ARC")
    return _latest_action(limit, seconds, vehicle.breaks)


@dataclass
class RouteIndex:
    vehicles: dict[UUID, VehicleSnapshot]
    stops: dict[UUID, RoutingStop]
    lots: dict[UUID, CargoLot]
    lot_stops: dict[UUID, list[RoutingStop]]
    points: dict[UUID, ReceivingPointSnapshot]
    matrices: dict[UUID, RoadMatrix]
    indices: dict[UUID, dict[UUID, int]]

    @classmethod
    def compile(cls, problem: RoutingProblem) -> RouteIndex:
        lot_stops: dict[UUID, list[RoutingStop]] = {}
        for stop in problem.stops:
            lot_stops.setdefault(stop.lot_id, []).append(stop)
        return cls(
            {v.vehicle_id: v for v in problem.vehicles},
            {s.stop_id: s for s in problem.stops},
            {lot.lot_id: lot for lot in problem.lots},
            lot_stops,
            {p.receiving_point_id: p for p in problem.receiving_points},
            {m.matrix_id: m for m in problem.matrices},
            {m.matrix_id: {n: i for i, n in enumerate(m.node_ids)} for m in problem.matrices},
        )


def route_problem(
    problem: RoutingProblem,
    vehicle_id: UUID,
    order: tuple[UUID, ...],
    index: RouteIndex | None = None,
) -> RoutingProblem:
    index = index or RouteIndex.compile(problem)
    vehicle, by_stop = index.vehicles[vehicle_id], index.stops
    lot_ids = {by_stop[s].lot_id for s in order} | {c.lot_id for c in vehicle.initial_cargo}
    lots = tuple(index.lots[key] for key in sorted(lot_ids, key=str))
    stops = tuple(
        replace(
            s, eligible_vehicle_ids=(vehicle_id,) if vehicle_id in s.eligible_vehicle_ids else ()
        )
        for key in sorted(lot_ids, key=str)
        for s in index.lot_stops[key]
    )
    point_ids = {p for lot in lots for p in lot.authorized_receiving_point_ids}
    points = tuple(index.points[key] for key in sorted(point_ids, key=str))
    nodes = {vehicle.start_node_id, vehicle.end_node_id} | {s.node_id for s in stops}
    nodes.update(p.node_id for p in points)
    matrix = index.matrices[vehicle.matrix_id]
    indices = index.indices[vehicle.matrix_id]
    path = (
        (vehicle.start_node_id,) + tuple(by_stop[s].node_id for s in order) + (vehicle.end_node_id,)
    )
    pairs = set(zip(path, path[1:], strict=False))
    arcs = tuple(
        RoadArc(a, b, *matrix.arc(a, b, indices))
        for a, b in sorted(pairs, key=lambda p: (p[0].int, p[1].int))
    )
    local = RoadMatrix(
        matrix.matrix_id,
        matrix.graph_version,
        matrix.profile_version,
        tuple(sorted(nodes, key=str)),
        (),
        (),
        arcs,
    )
    return replace(
        problem,
        contract_version=ROUTING_CONTRACT_V3,
        vehicles=(vehicle,),
        lots=lots,
        stops=stops,
        receiving_points=points,
        matrices=(local,),
        limits=RoutingLimits(max(1, len(nodes)), max(1, len(stops)), 1, 1),
    )


@dataclass(frozen=True, slots=True)
class RouteProof:
    __pydantic_config__ = ConfigDict(ser_json_bytes="hex", val_json_bytes="hex")
    fingerprint: bytes
    order: tuple[UUID, ...]
    latest_entry: int
    assessment: RouteAssessment


PROBLEM_CODEC = TypeAdapter(RoutingProblem)


def route_fingerprint(problem: RoutingProblem, order: tuple[UUID, ...]) -> bytes:
    vehicle = problem.vehicles[0]
    canonical = replace(
        problem,
        problem_id=UUID(int=0),
        snapshot_id=UUID(int=0),
        vehicles=(replace(vehicle, available_at=vehicle.shift.start),),
        matrices=tuple(replace(m, matrix_id=UUID(int=0)) for m in problem.matrices),
    )
    canonical = replace(
        canonical, vehicles=(replace(canonical.vehicles[0], matrix_id=UUID(int=0)),)
    )
    data = PROBLEM_CODEC.dump_json(canonical)
    return hashlib.sha256(data + b"".join(s.bytes for s in order)).digest()


def certify_route(
    problem: RoutingProblem,
    proposal: RouteProposal,
    vehicle_id: UUID,
    local: RoutingProblem | None = None,
) -> RouteProof:
    route = next(r for r in proposal.routes if r.vehicle_id == vehicle_id)
    local = local or route_problem(problem, vehicle_id, route.stop_ids)
    complete = replace(
        proposal, contract_version=local.contract_version, routes=(route,), unserved_stop_ids=()
    )
    assessment = validate_proposal(local, complete).routes[0]
    return RouteProof(
        route_fingerprint(local, route.stop_ids),
        route.stop_ids,
        latest_route_entry(local, local.vehicles[0], route.stop_ids),
        assessment,
    )


def compose(
    problem: RoutingProblem, proposal: RouteProposal, previous: dict[UUID, RouteProof]
) -> dict[UUID, RouteProof]:
    """Global coverage is checked even when unchanged route scheduling is reused."""
    validate_problem(problem)
    if (
        proposal.problem_id != problem.problem_id
        or proposal.snapshot_id != problem.snapshot_id
        or proposal.contract_version != problem.contract_version
        or proposal.policy_version != problem.policy_version
        or len(set(proposal.unserved_stop_ids)) != len(proposal.unserved_stop_ids)
        or type(proposal.routes) is not tuple
        or type(proposal.unserved_stop_ids) is not tuple
        or not isinstance(proposal.engine, (OptimizerEngine, SelectionEngine))
        or not proposal.engine_version
    ):
        raise RoutingValidationError("PROPOSAL_PROVENANCE_MISMATCH")
    index = RouteIndex.compile(problem)
    stops, vehicles = index.stops, index.vehicles
    served: set[UUID] = set()
    proofs: dict[UUID, RouteProof] = {}
    for route in proposal.routes:
        if route.vehicle_id not in vehicles or route.vehicle_id in proofs:
            raise RoutingValidationError("DUPLICATE_ROUTE_VEHICLE")
        for s in route.stop_ids:
            if s not in stops or s in served:
                raise RoutingValidationError("DUPLICATE_ROUTE_STOP")
            served.add(s)
        local = route_problem(problem, route.vehicle_id, route.stop_ids, index)
        old = previous.get(route.vehicle_id)
        fingerprint = route_fingerprint(local, route.stop_ids)
        vehicle = vehicles[route.vehicle_id]
        if old and old.fingerprint == fingerprint and vehicle.available_at <= old.latest_entry:
            proofs[route.vehicle_id] = old
        else:
            proofs[route.vehicle_id] = certify_route(problem, proposal, route.vehicle_id, local)
    missing = set(stops) - served
    if missing != set(proposal.unserved_stop_ids) or any(stops[s].required for s in missing):
        raise RoutingValidationError("REQUIRED_STOP_UNSERVED")
    if any(
        v.vehicle_id not in proofs and (v.initial_cargo or v.protected_prefix)
        for v in problem.vehicles
    ):
        raise RoutingValidationError("COMMITTED_ROUTE_OMITTED")
    return proofs
