from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from .contracts import (
    CargoLot,
    OptimizerEngine,
    ProposedRoute,
    ReceivingPointSnapshot,
    RouteProposal,
    RoutingProblem,
    RoutingStop,
    StopKind,
)
from .execution import RoutingAdapterError
from .validation import RoutingValidationError, validate_problem, validate_proposal

INT32_MAX = 2**31 - 1
UINT32_MAX = 2**32 - 1


def stop_windows(
    problem: RoutingProblem,
    stop: RoutingStop,
    lots: Mapping[UUID, CargoLot] | None = None,
    facilities: Mapping[UUID, ReceivingPointSnapshot] | None = None,
) -> tuple[tuple[int, int], ...]:
    low, high = stop.service_start_window.start, stop.service_start_window.end
    if stop.kind == StopKind.PICKUP:
        return ((low, high),)
    if stop.receiving_point_id is None:
        raise RoutingAdapterError("UNAUTHORIZED_RECEIVING_POINT")
    lot = (
        lots[stop.lot_id]
        if lots is not None
        else next(item for item in problem.lots if item.lot_id == stop.lot_id)
    )
    facility = (
        facilities[stop.receiving_point_id]
        if facilities is not None
        else next(
            item
            for item in problem.receiving_points
            if item.receiving_point_id == stop.receiving_point_id
        )
    )
    if not facility.available:
        return ()
    windows = tuple(
        (
            max(low, window.start),
            min(
                high,
                window.end - stop.service_seconds,
                lot.discharge_deadline - stop.service_seconds,
            ),
        )
        for window in facility.opening_windows
    )
    return tuple((a, b) for a, b in windows if a <= b)


@dataclass(frozen=True)
class PreparedProblem:
    payload: dict[str, Any]
    stop_ids: tuple[UUID, ...]
    vehicle_ids: tuple[UUID, ...]
    prefixes: tuple[tuple[UUID, ...], ...]


def prepare(problem: RoutingProblem) -> PreparedProblem:
    validate_problem(problem)
    if any(m.sparse_arcs is not None for m in problem.matrices):
        raise RoutingAdapterError("NATIVE_REQUIRES_DENSE_MATRIX")
    horizon = max((item.shift.end for item in problem.vehicles), default=0)
    if horizon >= INT32_MAX:
        raise RoutingAdapterError("NATIVE_RANGE_EXCEEDED")
    stops = {item.stop_id: item for item in problem.stops}
    lots = {item.lot_id: item for item in problem.lots}
    facilities = {p.receiving_point_id: p for p in problem.receiving_points}
    lot_ids = tuple(sorted(lots, key=lambda item: item.int))
    lot_indexes = {key: i for i, key in enumerate(lot_ids)}
    stop_ids = tuple(sorted(stops, key=lambda item: item.int))
    stop_indexes = {item: i + 1 for i, item in enumerate(stop_ids)}
    vehicles = tuple(sorted(problem.vehicles, key=lambda item: item.vehicle_id.int))
    vehicle_ids = tuple(item.vehicle_id for item in vehicles)
    vehicle_indexes = {item: i + 1 for i, item in enumerate(vehicle_ids)}
    nodes = tuple(
        sorted(
            {node for matrix in problem.matrices for node in matrix.node_ids},
            key=lambda item: item.int,
        )
    )
    if len(nodes) > problem.limits.max_nodes:
        raise RoutingAdapterError("ADAPTER_NODE_LIMIT")
    node_indexes = {item: i for i, item in enumerate(nodes)}
    matrices = {item.matrix_id: item for item in problem.matrices}
    consumed: set[UUID] = set()
    native_vehicles: list[dict[str, Any]] = []
    owners: dict[UUID, int] = {}
    for vi, vehicle in enumerate(vehicles, 1):
        matrix = matrices[vehicle.matrix_id]
        indexes = {item: i for i, item in enumerate(matrix.node_ids)}
        time, node = vehicle.available_at, vehicle.start_node_id
        cargo = {item.lot_id: item.quantity for item in vehicle.initial_cargo}
        load = [
            sum(q * lots[key].unit_load[d] for key, q in cargo.items())
            for d in range(len(problem.load_dimensions))
        ]
        for stop_id in vehicle.protected_prefix:
            stop = stops[stop_id]
            if (
                vehicle.vehicle_id not in stop.eligible_vehicle_ids
                or stop.bound_vehicle_id != vehicle.vehicle_id
            ):
                raise RoutingAdapterError("INVALID_PROTECTED_PREFIX")
            leg = matrix.travel_seconds[indexes[node]][indexes[stop.node_id]]
            if leg is None:
                raise RoutingAdapterError("UNREACHABLE_PREFIX")
            from .validation import avoid_breaks

            arrival = avoid_breaks(time, leg, vehicle.breaks) + leg
            start = next(
                (
                    max(arrival, a)
                    for a, b in stop_windows(problem, stop, lots, facilities)
                    if max(arrival, a) <= b
                ),
                None,
            )
            if start is not None:
                start = avoid_breaks(start, stop.service_seconds, vehicle.breaks)
            if start is None:
                raise RoutingAdapterError("INFEASIBLE_PREFIX")
            time, node = start + stop.service_seconds, stop.node_id
            delta = stop.quantity * (1 if stop.kind == StopKind.PICKUP else -1)
            cargo[stop.lot_id] = cargo.get(stop.lot_id, 0) + delta
            load = [
                amount + delta * unit
                for amount, unit in zip(load, lots[stop.lot_id].unit_load, strict=True)
            ]
            if (
                cargo[stop.lot_id] < 0
                or any(q > cap for q, cap in zip(load, vehicle.capacity, strict=True))
                or time > vehicle.shift.end
            ):
                raise RoutingAdapterError("INFEASIBLE_PREFIX")
            consumed.add(stop_id)
        for key, quantity in cargo.items():
            if quantity:
                owners[key] = vi
        initial = load
        native_vehicles.append(
            dict(
                id=vi,
                profile=str(vehicle.matrix_id),
                start=node_indexes[node],
                end=node_indexes[vehicle.end_node_id],
                available=time,
                end_at=vehicle.shift.end,
                breaks=[[b.start, b.end] for b in vehicle.breaks if b.end > time],
                capacity=list(vehicle.capacity),
                initial=initial,
                initial_lots={str(lot_indexes[key]): q for key, q in cargo.items() if q},
                mandatory=bool(vehicle.protected_prefix or vehicle.initial_cargo),
            )
        )
    native_stops: list[dict[str, Any]] = []
    for stop_id in stop_ids:
        if stop_id in consumed:
            continue
        stop = stops[stop_id]
        lot = lots[stop.lot_id]
        sign = 1 if stop.kind == StopKind.PICKUP else -1
        load_delta = [sign * stop.quantity * amount for amount in lot.unit_load]
        allowed = [
            vehicle_indexes[key]
            for key in stop.eligible_vehicle_ids
            if stop.bound_vehicle_id is None or key == stop.bound_vehicle_id
        ]
        if stop.lot_id in owners:
            allowed = [item for item in allowed if item == owners[stop.lot_id]]
        native_stops.append(
            dict(
                id=stop_indexes[stop_id],
                node=node_indexes.get(stop.node_id, -1),
                kind=stop.kind.value,
                lot=lot_indexes[stop.lot_id],
                quantity=stop.quantity,
                delta=load_delta,
                service=stop.service_seconds,
                windows=stop_windows(problem, stop, lots, facilities),
                allowed=allowed,
                required=stop.required,
            )
        )
    native_matrices: dict[str, Any] = {}
    max_arc = 0
    for matrix in problem.matrices:
        indexes = {item: i for i, item in enumerate(matrix.node_ids)}
        times: list[list[int]] = []
        distances: list[list[int]] = []
        for origin in nodes:
            tr: list[int] = []
            dr: list[int] = []
            for dest in nodes:
                seconds = (
                    matrix.travel_seconds[indexes[origin]][indexes[dest]]
                    if origin in indexes and dest in indexes
                    else None
                )
                meters = (
                    matrix.distance_m[indexes[origin]][indexes[dest]]
                    if origin in indexes and dest in indexes
                    else None
                )
                tr.append(horizon + 1 if seconds is None else seconds)
                dr.append(0 if meters is None else meters)
            times.append(tr)
            distances.append(dr)
            max_arc = max(max_arc, *tr, *dr)
        native_matrices[str(matrix.matrix_id)] = dict(durations=times, distances=distances)
    bound = max_arc * (len(native_stops) + len(vehicles))
    if bound > UINT32_MAX:
        raise RoutingAdapterError("NATIVE_RANGE_EXCEEDED")
    for item in native_stops:
        values = [abs(value) for value in item["delta"]] + [item["service"]]
        if any(value > INT32_MAX for value in values):
            raise RoutingAdapterError("NATIVE_RANGE_EXCEEDED")
    if any(
        value > INT32_MAX
        for item in native_vehicles
        for value in item["capacity"] + item["initial"]
    ):
        raise RoutingAdapterError("NATIVE_RANGE_EXCEEDED")
    payload = dict(
        stops=native_stops,
        vehicles=native_vehicles,
        matrices=native_matrices,
        physical_dimensions=len(problem.load_dimensions),
        horizon=horizon,
        penalty=bound + 1,
    )
    return PreparedProblem(
        payload, stop_ids, vehicle_ids, tuple(item.protected_prefix for item in vehicles)
    )


def finish(
    problem: RoutingProblem,
    prepared: PreparedProblem,
    native_routes: list[dict[str, Any]],
    engine: OptimizerEngine,
    version: str,
) -> RouteProposal:
    try:
        routes: dict[int, tuple[UUID, ...]] = {}
        for route in native_routes:
            vi, jobs = route["vehicle"], route["jobs"]
            if (
                type(vi) is not int
                or vi < 1
                or vi > len(prepared.vehicle_ids)
                or vi in routes
                or not isinstance(jobs, list)
                or any(
                    type(job) is not int or job < 1 or job > len(prepared.stop_ids) for job in jobs
                )
            ):
                raise ValueError("invalid route")
            routes[vi] = tuple(prepared.stop_ids[job - 1] for job in jobs)
        proposal_routes = tuple(
            ProposedRoute(key, prepared.prefixes[i - 1] + routes.get(i, ()))
            for i, key in enumerate(prepared.vehicle_ids, 1)
            if i in routes or prepared.prefixes[i - 1]
        )
        served = {key for route in proposal_routes for key in route.stop_ids}
        candidate = RouteProposal(
            problem.problem_id,
            problem.snapshot_id,
            problem.contract_version,
            problem.policy_version,
            engine,
            version,
            proposal_routes,
            tuple(key for key in prepared.stop_ids if key not in served),
        )
        validate_proposal(problem, candidate)
    except (KeyError, TypeError, ValueError, IndexError) as error:
        code = "SOLVER_INVALID_PROPOSAL"
        if isinstance(error, RoutingValidationError) and error.code == "REQUIRED_STOP_UNSERVED":
            code = "NO_FEASIBLE_PROPOSAL"
        raise RoutingAdapterError(code) from None
    return candidate


def residual_cost(prepared: PreparedProblem, routes: list[dict[str, Any]]) -> int:
    stops = {item["id"]: item for item in prepared.payload["stops"]}
    vehicles = {item["id"]: item for item in prepared.payload["vehicles"]}
    result = 0
    try:
        for route in routes:
            vehicle = vehicles[route["vehicle"]]
            matrix = prepared.payload["matrices"][vehicle["profile"]]["durations"]
            path = (
                [vehicle["start"]]
                + [stops[key]["node"] for key in route["jobs"]]
                + [vehicle["end"]]
            )
            result += sum(matrix[a][b] for a, b in zip(path, path[1:], strict=False))
    except (KeyError, TypeError, IndexError):
        raise RoutingAdapterError("SOLVER_INVALID_PROPOSAL") from None
    return result
