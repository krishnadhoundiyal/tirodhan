from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from uuid import UUID

from .contracts import (
    ROUTING_CONTRACT_V1,
    ROUTING_CONTRACT_V2,
    ROUTING_CONTRACT_V3,
    CargoLot,
    OptimizerEngine,
    ReceivingPointSnapshot,
    ResourceKind,
    RoadMatrix,
    RouteProposal,
    RoutingProblem,
    RoutingStop,
    SelectionEngine,
    StopKind,
    TimeWindow,
    VehicleSnapshot,
)


class RoutingValidationError(ValueError):
    """Controlled failure codes only; no coordinates, raw solver output or request data."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise RoutingValidationError(code)


def _integer(value: int, *, positive: bool = False) -> bool:
    return type(value) is int and value >= (1 if positive else 0)


def _unique(values: tuple[UUID, ...]) -> bool:
    return type(values) is tuple and len(set(values)) == len(values)


def _window(window: TimeWindow) -> bool:
    return _integer(window.start) and _integer(window.end) and window.start <= window.end


def _vector(vector: tuple[int, ...], dimensions: int) -> bool:
    return (
        type(vector) is tuple
        and len(vector) == dimensions
        and all(_integer(value) for value in vector)
        and any(vector)
    )


def validate_problem(problem: RoutingProblem) -> None:
    """Validate complete immutable inputs before an adapter or validator consumes them."""
    _require(
        problem.contract_version in (ROUTING_CONTRACT_V1, ROUTING_CONTRACT_V2, ROUTING_CONTRACT_V3),
        "UNSUPPORTED_CONTRACT",
    )
    _require(bool(problem.policy_version), "MISSING_POLICY_VERSION")
    _require(problem.epoch.utcoffset() == timedelta(0), "EPOCH_NOT_UTC")
    _require(
        type(problem.load_dimensions) is tuple
        and bool(problem.load_dimensions)
        and all(problem.load_dimensions)
        and len(set(problem.load_dimensions)) == len(problem.load_dimensions),
        "INVALID_LOAD_DIMENSIONS",
    )
    limits = problem.limits
    _require(
        all(
            _integer(value, positive=True)
            for value in (
                limits.max_nodes,
                limits.max_stops,
                limits.max_vehicles,
                limits.max_matrices,
            )
        ),
        "INVALID_LIMITS",
    )
    for values in (
        problem.matrices,
        problem.vehicles,
        problem.receiving_points,
        problem.lots,
        problem.stops,
    ):
        _require(type(values) is tuple, "MUTABLE_INPUT")
    _require(
        len(problem.stops) <= limits.max_stops
        and len(problem.vehicles) <= limits.max_vehicles
        and len(problem.matrices) <= limits.max_matrices
        and len(problem.lots) <= limits.max_stops,
        "WORKING_SET_LIMIT",
    )
    for ids in (
        tuple(item.matrix_id for item in problem.matrices),
        tuple(item.vehicle_id for item in problem.vehicles),
        tuple(item.rider_id for item in problem.vehicles),
        tuple(item.receiving_point_id for item in problem.receiving_points),
        tuple(item.lot_id for item in problem.lots),
        tuple(item.collection_request_id for item in problem.lots),
        tuple(item.stop_id for item in problem.stops),
    ):
        _require(_unique(ids), "DUPLICATE_INPUT_ID")
    _require(len(problem.receiving_points) <= limits.max_nodes, "WORKING_SET_LIMIT")
    matrices = {item.matrix_id: item for item in problem.matrices}
    matrix_nodes = {key: set(m.node_ids) for key, m in matrices.items()}
    vehicles = {item.vehicle_id: item for item in problem.vehicles}
    facilities = {item.receiving_point_id: item for item in problem.receiving_points}
    lots = {item.lot_id: item for item in problem.lots}
    stops = {item.stop_id: item for item in problem.stops}
    dimensions = len(problem.load_dimensions)
    for matrix in problem.matrices:
        _require(
            matrix.sparse_arcs is None or problem.contract_version == ROUTING_CONTRACT_V3,
            "SPARSE_MATRIX_REQUIRES_CONTRACT_V3",
        )
        _validate_matrix(matrix, limits.max_nodes)
    for facility in problem.receiving_points:
        _require(_integer(facility.state_version), "INVALID_STATE_VERSION")
        _require(type(facility.available) is bool, "INVALID_FACILITY_AVAILABILITY")
        _require(type(facility.opening_windows) is tuple, "MUTABLE_INPUT")
        _require(bool(facility.opening_windows), "MISSING_FACILITY_HOURS")
        previous_end = -1
        for window in facility.opening_windows:
            _require(_window(window) and window.start > previous_end, "INVALID_FACILITY_HOURS")
            previous_end = window.end
    initial_owners: dict[UUID, UUID] = {}
    for vehicle in problem.vehicles:
        _require(_integer(vehicle.state_version), "INVALID_STATE_VERSION")
        _require(isinstance(vehicle.resource_kind, ResourceKind), "INVALID_RESOURCE_KIND")
        _require(
            vehicle.resource_kind != ResourceKind.COMMITTED
            or vehicle.operating_shift_id is not None,
            "MISSING_COMMITTED_SHIFT",
        )
        _require(
            _window(vehicle.shift)
            and _integer(vehicle.available_at)
            and vehicle.shift.start <= vehicle.available_at <= vehicle.shift.end,
            "INVALID_VEHICLE_AVAILABILITY",
        )
        _require(_vector(vehicle.capacity, dimensions), "INVALID_CAPACITY")
        _require(vehicle.matrix_id in matrices, "UNKNOWN_MATRIX")
        nodes = matrix_nodes[vehicle.matrix_id]
        _require(
            vehicle.start_node_id in nodes and vehicle.end_node_id in nodes,
            "UNKNOWN_VEHICLE_NODE",
        )
        _require(
            type(vehicle.initial_cargo) is tuple and type(vehicle.breaks) is tuple, "MUTABLE_INPUT"
        )
        _require(
            not vehicle.breaks
            or problem.contract_version in (ROUTING_CONTRACT_V2, ROUTING_CONTRACT_V3),
            "BREAKS_REQUIRE_CONTRACT_V2",
        )
        _require(
            all(_window(b) and b.start < b.end for b in vehicle.breaks)
            and all(
                a.end <= b.start for a, b in zip(vehicle.breaks, vehicle.breaks[1:], strict=False)
            ),
            "INVALID_BREAK_WINDOWS",
        )
        _require(_unique(vehicle.protected_prefix), "INVALID_PROTECTED_PREFIX")
        for stop_id in vehicle.protected_prefix:
            _require(
                stop_id in stops
                and stops[stop_id].required
                and stops[stop_id].bound_vehicle_id == vehicle.vehicle_id,
                "INVALID_PROTECTED_PREFIX",
            )
        for balance in vehicle.initial_cargo:
            _require(balance.lot_id in lots, "UNKNOWN_INITIAL_LOT")
            lot = lots[balance.lot_id]
            _require(
                lot.pickup_stop_id is None
                and _integer(balance.quantity, positive=True)
                and balance.quantity == lot.quantity
                and balance.lot_id not in initial_owners,
                "INVALID_INITIAL_CARGO",
            )
            initial_owners[balance.lot_id] = vehicle.vehicle_id
    for lot in problem.lots:
        _require(
            _integer(lot.quantity, positive=True)
            and bool(lot.quantity_unit)
            and _vector(lot.unit_load, dimensions)
            and _integer(lot.discharge_deadline),
            "INVALID_LOT",
        )
        _require(
            _unique(lot.authorized_receiving_point_ids)
            and bool(lot.authorized_receiving_point_ids)
            and all(item in facilities for item in lot.authorized_receiving_point_ids),
            "INVALID_LOT_DESTINATIONS",
        )
        if lot.pickup_stop_id is None:
            _require(lot.lot_id in initial_owners, "MISSING_INITIAL_CARGO_OWNER")
        else:
            _require(
                lot.pickup_stop_id in stops
                and stops[lot.pickup_stop_id].kind == StopKind.PICKUP
                and stops[lot.pickup_stop_id].lot_id == lot.lot_id,
                "INVALID_LOT_PICKUP",
            )
    for stop in problem.stops:
        _require(isinstance(stop.kind, StopKind), "INVALID_STOP_KIND")
        _require(stop.lot_id in lots, "UNKNOWN_STOP_LOT")
        _require(type(stop.required) is bool, "INVALID_REQUIRED_FLAG")
        _require(
            _integer(stop.quantity, positive=True)
            and _integer(stop.service_seconds)
            and _window(stop.service_start_window),
            "INVALID_STOP_TERMS",
        )
        _require(
            _unique(stop.eligible_vehicle_ids)
            and all(item in vehicles for item in stop.eligible_vehicle_ids),
            "INVALID_STOP_ELIGIBILITY",
        )
        _require(
            stop.bound_vehicle_id is None or stop.bound_vehicle_id in stop.eligible_vehicle_ids,
            "INVALID_STOP_OWNER",
        )
        for vehicle_id in stop.eligible_vehicle_ids:
            _require(
                stop.node_id in matrix_nodes[vehicles[vehicle_id].matrix_id],
                "UNKNOWN_STOP_NODE",
            )
        lot = lots[stop.lot_id]
        if stop.kind == StopKind.PICKUP:
            _require(
                lot.pickup_stop_id == stop.stop_id
                and lot.quantity == stop.quantity
                and stop.receiving_point_id is None,
                "INVALID_PICKUP",
            )
        else:
            if stop.receiving_point_id is None:
                raise RoutingValidationError("UNAUTHORIZED_RECEIVING_POINT")
            _require(
                stop.receiving_point_id in facilities
                and stop.receiving_point_id in lot.authorized_receiving_point_ids,
                "UNAUTHORIZED_RECEIVING_POINT",
            )
            facility = facilities[stop.receiving_point_id]
            _require(stop.node_id == facility.node_id, "WRONG_DISCHARGE_NODE")
    for vehicle in problem.vehicles:
        _check_capacity(
            tuple(
                sum(
                    item.quantity * lots[item.lot_id].unit_load[d] for item in vehicle.initial_cargo
                )
                for d in range(dimensions)
            ),
            vehicle,
        )


def _validate_matrix(matrix: RoadMatrix, max_nodes: int) -> None:
    n = len(matrix.node_ids)
    _require(0 < n <= max_nodes and _unique(matrix.node_ids), "INVALID_MATRIX_NODES")
    _require(bool(matrix.graph_version) and bool(matrix.profile_version), "MISSING_MATRIX_VERSION")
    if matrix.sparse_arcs is not None:
        _require(not matrix.travel_seconds and not matrix.distance_m, "INVALID_MATRIX_SHAPE")
        nodes = set(matrix.node_ids)
        previous = (-1, -1)
        for arc in matrix.sparse_arcs:
            key = (arc.origin.int, arc.destination.int)
            _require(key > previous, "DUPLICATE_MATRIX_ARC")
            previous = key
            _require(arc.origin in nodes and arc.destination in nodes, "INVALID_MATRIX_NODES")
            _require(
                (arc.seconds is None and arc.metres is None)
                or (
                    arc.seconds is not None
                    and arc.metres is not None
                    and _integer(arc.seconds)
                    and _integer(arc.metres)
                ),
                "INVALID_MATRIX_VALUE",
            )
            if arc.origin == arc.destination:
                _require(arc.seconds == arc.metres == 0, "INVALID_MATRIX_DIAGONAL")
        return
    for values in (matrix.travel_seconds, matrix.distance_m):
        _require(type(values) is tuple and len(values) == n, "INVALID_MATRIX_SHAPE")
        for i, row in enumerate(values):
            _require(type(row) is tuple and len(row) == n, "INVALID_MATRIX_SHAPE")
            _require(all(item is None or _integer(item) for item in row), "INVALID_MATRIX_VALUE")
            _require(row[i] == 0, "INVALID_MATRIX_DIAGONAL")
    _require(
        all(
            (matrix.travel_seconds[i][j] is None) == (matrix.distance_m[i][j] is None)
            for i in range(n)
            for j in range(n)
        ),
        "INCONSISTENT_MATRIX_REACHABILITY",
    )


@dataclass(frozen=True, slots=True)
class StopAssessment:
    stop_id: UUID
    arrival_at: int
    service_start_at: int
    service_end_at: int
    load_after: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class RouteAssessment:
    vehicle_id: UUID
    resource_kind: ResourceKind
    stops: tuple[StopAssessment, ...]
    end_at: int
    travel_seconds: int
    distance_m: int


@dataclass(frozen=True, slots=True)
class ProposalAssessment:
    """Feasibility evidence for this snapshot only; never dispatch authorization."""

    routes: tuple[RouteAssessment, ...]
    unserved_stop_ids: tuple[UUID, ...]


def validate_proposal(problem: RoutingProblem, proposal: RouteProposal) -> ProposalAssessment:
    validate_problem(problem)
    return _validate_routes(problem, proposal)


@dataclass(frozen=True, slots=True)
class ValidationContext:
    stops: dict[UUID, RoutingStop]
    lots: dict[UUID, CargoLot]
    facilities: dict[UUID, ReceivingPointSnapshot]
    matrices: dict[UUID, RoadMatrix]
    indices: dict[UUID, dict[UUID, int]]

    @classmethod
    def compile(cls, problem: RoutingProblem) -> ValidationContext:
        return cls(
            {s.stop_id: s for s in problem.stops},
            {lot.lot_id: lot for lot in problem.lots},
            {p.receiving_point_id: p for p in problem.receiving_points},
            {m.matrix_id: m for m in problem.matrices},
            {m.matrix_id: {node: i for i, node in enumerate(m.node_ids)} for m in problem.matrices},
        )


@dataclass(frozen=True, slots=True)
class FeasibilityGate:
    """Validate immutable matrix/problem once when assessing many local-search proposals."""

    problem: RoutingProblem
    context: ValidationContext = field(init=False, repr=False)

    def __post_init__(self) -> None:
        validate_problem(self.problem)
        object.__setattr__(self, "context", ValidationContext.compile(self.problem))

    def assess(self, proposal: RouteProposal) -> ProposalAssessment:
        return _validate_routes(self.problem, proposal, self.context)


def _validate_routes(
    problem: RoutingProblem, proposal: RouteProposal, context: ValidationContext | None = None
) -> ProposalAssessment:
    context = context or ValidationContext.compile(problem)
    _require(
        proposal.contract_version == problem.contract_version
        and proposal.policy_version == problem.policy_version
        and proposal.problem_id == problem.problem_id
        and proposal.snapshot_id == problem.snapshot_id,
        "PROPOSAL_SNAPSHOT_MISMATCH",
    )
    _require(
        isinstance(proposal.engine, (OptimizerEngine, SelectionEngine))
        and bool(proposal.engine_version),
        "INVALID_ENGINE_VERSION",
    )
    _require(type(proposal.routes) is tuple, "MUTABLE_PROPOSAL")
    _require(len(proposal.routes) <= problem.limits.max_vehicles, "WORKING_SET_LIMIT")
    vehicles = {item.vehicle_id: item for item in problem.vehicles}
    stops = {item.stop_id: item for item in problem.stops}
    route_vehicles = tuple(route.vehicle_id for route in proposal.routes)
    _require(_unique(route_vehicles), "DUPLICATE_ROUTE_VEHICLE")
    _require(all(item in vehicles for item in route_vehicles), "UNKNOWN_ROUTE_VEHICLE")
    served: set[UUID] = set()
    for route in proposal.routes:
        _require(_unique(route.stop_ids), "DUPLICATE_ROUTE_STOP")
        for stop_id in route.stop_ids:
            _require(stop_id in stops, "UNKNOWN_ROUTE_STOP")
            _require(stop_id not in served, "DUPLICATE_ROUTE_STOP")
            served.add(stop_id)
        prefix = vehicles[route.vehicle_id].protected_prefix
        _require(route.stop_ids[: len(prefix)] == prefix, "PROTECTED_PREFIX_CHANGED")
    _require(_unique(proposal.unserved_stop_ids), "INVALID_UNSERVED_STOPS")
    unserved = set(proposal.unserved_stop_ids)
    _require(not served.intersection(unserved), "SERVED_AND_UNSERVED")
    _require(served.union(unserved) == set(stops), "INCOMPLETE_COVERAGE")
    _require(all(not stops[item].required for item in unserved), "REQUIRED_STOP_UNSERVED")
    for vehicle in problem.vehicles:
        _require(
            vehicle.vehicle_id in route_vehicles
            or (not vehicle.initial_cargo and not vehicle.protected_prefix),
            "COMMITTED_ROUTE_OMITTED",
        )
    assessments = tuple(
        _assess_route(problem, vehicles[route.vehicle_id], route.stop_ids, context)
        for route in proposal.routes
    )
    return ProposalAssessment(assessments, proposal.unserved_stop_ids)


def _load(problem: RoutingProblem, balances: dict[UUID, int]) -> tuple[int, ...]:
    lots = {item.lot_id: item for item in problem.lots}
    return tuple(
        sum(quantity * lots[lot_id].unit_load[d] for lot_id, quantity in balances.items())
        for d in range(len(problem.load_dimensions))
    )


def _check_capacity(load: tuple[int, ...], vehicle: VehicleSnapshot) -> None:
    _require(
        all(
            0 <= amount <= capacity for amount, capacity in zip(load, vehicle.capacity, strict=True)
        ),
        "CAPACITY_EXCEEDED",
    )


def _assess_route(
    problem: RoutingProblem,
    vehicle: VehicleSnapshot,
    stop_ids: tuple[UUID, ...],
    context: ValidationContext | None = None,
) -> RouteAssessment:
    context = context or ValidationContext.compile(problem)
    stops, lots, facilities = context.stops, context.lots, context.facilities
    matrix = context.matrices[vehicle.matrix_id]
    node_indexes = context.indices[vehicle.matrix_id]
    balances = {item.lot_id: item.quantity for item in vehicle.initial_cargo}
    load = tuple(
        sum(q * lots[lot].unit_load[d] for lot, q in balances.items())
        for d in range(len(problem.load_dimensions))
    )
    _check_capacity(load, vehicle)
    current_node = vehicle.start_node_id
    current_time = vehicle.available_at
    travel_seconds = distance_m = 0
    assessments: list[StopAssessment] = []
    for stop_id in stop_ids:
        stop = stops[stop_id]
        _require(
            vehicle.vehicle_id in stop.eligible_vehicle_ids
            and (stop.bound_vehicle_id is None or stop.bound_vehicle_id == vehicle.vehicle_id),
            "STOP_VEHICLE_INELIGIBLE",
        )
        seconds, meters = _arc(matrix, node_indexes, current_node, stop.node_id)
        departure = avoid_breaks(current_time, seconds, vehicle.breaks)
        arrival = departure + seconds
        start = max(arrival, stop.service_start_window.start)
        start = avoid_breaks(start, stop.service_seconds, vehicle.breaks)
        if stop.kind == StopKind.DISCHARGE:
            if stop.receiving_point_id is None:
                raise RoutingValidationError("UNAUTHORIZED_RECEIVING_POINT")
            facility = facilities[stop.receiving_point_id]
            _require(facility.available, "RECEIVING_POINT_UNAVAILABLE")
            start = next(
                (
                    avoid_breaks(max(start, window.start), stop.service_seconds, vehicle.breaks)
                    for window in facility.opening_windows
                    if avoid_breaks(max(start, window.start), stop.service_seconds, vehicle.breaks)
                    + stop.service_seconds
                    <= window.end
                ),
                -1,
            )
            _require(start >= 0, "RECEIVING_POINT_CLOSED")
        _require(start <= stop.service_start_window.end, "SERVICE_WINDOW_MISSED")
        end = start + stop.service_seconds
        lot = lots[stop.lot_id]
        if stop.kind == StopKind.PICKUP:
            _require(stop.lot_id not in balances, "LOT_ALREADY_ONBOARD")
            balances[stop.lot_id] = stop.quantity
        else:
            _require(balances.get(stop.lot_id, 0) >= stop.quantity, "CARGO_NOT_ONBOARD")
            _require(end <= lot.discharge_deadline, "CUSTODY_DEADLINE_MISSED")
            balances[stop.lot_id] -= stop.quantity
        sign = 1 if stop.kind == StopKind.PICKUP else -1
        load = tuple(
            amount + sign * stop.quantity * unit
            for amount, unit in zip(load, lot.unit_load, strict=True)
        )
        _check_capacity(load, vehicle)
        assessments.append(StopAssessment(stop_id, arrival, start, end, load))
        current_node, current_time = stop.node_id, end
        travel_seconds += seconds
        distance_m += meters
    _require(not any(balances.values()), "CARGO_NOT_DISCHARGED")
    seconds, meters = _arc(matrix, node_indexes, current_node, vehicle.end_node_id)
    end_at = avoid_breaks(current_time, seconds, vehicle.breaks) + seconds
    _require(end_at <= vehicle.shift.end, "SHIFT_END_MISSED")
    return RouteAssessment(
        vehicle.vehicle_id,
        vehicle.resource_kind,
        tuple(assessments),
        end_at,
        travel_seconds + seconds,
        distance_m + meters,
    )


def _arc(
    matrix: RoadMatrix, indexes: dict[UUID, int], origin: UUID, destination: UUID
) -> tuple[int, int]:
    try:
        seconds, meters = matrix.arc(origin, destination, indexes)
    except KeyError:
        raise RoutingValidationError("ROAD_ARC_NOT_ACQUIRED") from None
    if seconds is None or meters is None:
        raise RoutingValidationError("UNREACHABLE_ARC")
    return seconds, meters


def avoid_breaks(start: int, duration: int, breaks: tuple[TimeWindow, ...]) -> int:
    """Half-open nonworking intervals; never pause a road arc or service in its middle."""
    for pause in breaks:
        if start < pause.end and start + duration > pause.start:
            start = pause.end
    return start
