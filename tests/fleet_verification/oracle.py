"""Small whole-lot exhaustive oracle. Never imports production validation/economics.

Interprets the public immutable contract using an independent event/cargo ledger.
Enumeration includes omission of optional lots, vehicle assignment and every stop order.
"""

from dataclasses import dataclass
from itertools import permutations, product

from tirodhan.modules.planning.routing.contracts import RoutingProblem


class Infeasible(ValueError):
    pass


@dataclass(frozen=True)
class Weights:
    activation: int
    waiting: int
    disruption: int
    buffer: int
    risk: int


@dataclass(frozen=True)
class Score:
    unserved_lots: int
    deadline_risk: tuple[int, ...]
    travel_seconds: int
    distance_m: int
    waiting_seconds: int
    empty_travel_seconds: int
    additional_vehicles: int
    disruptions: int
    minimum_window_slack_seconds: int | None
    window_risk_seconds: int
    supplementary_lots: int
    generalized_seconds: int

    @property
    def rank(self):
        return (
            self.unserved_lots,
            self.deadline_risk,
            self.supplementary_lots,
            self.generalized_seconds,
            self.distance_m,
            self.disruptions,
        )


def evaluate(problem, orders, weights, incumbent):
    stops = {s.stop_id: s for s in problem.stops}
    lots = {lot.lot_id: lot for lot in problem.lots}
    vehicles = {v.vehicle_id: v for v in problem.vehicles}
    points = {f.receiving_point_id: f for f in problem.receiving_points}
    matrices = {m.matrix_id: m for m in problem.matrices}
    served = [s for order in orders.values() for s in order]
    if len(served) != len(set(served)) or set(served) - stops.keys():
        raise Infeasible("duplicate/unknown stop")
    if set(orders) - vehicles.keys():
        raise Infeasible("unknown vehicle")
    missing = set(stops) - set(served)
    if any(stops[s].required for s in missing):
        raise Infeasible("required work omitted")
    for lot in lots:
        members = {s.stop_id for s in problem.stops if s.lot_id == lot}
        if members & missing and members - missing:
            raise Infeasible("partial household")
    travel = distance = waiting = empty = deployed = 0
    slacks = []
    for vehicle_id, order in orders.items():
        vehicle = vehicles[vehicle_id]
        if tuple(order[: len(vehicle.protected_prefix)]) != vehicle.protected_prefix:
            raise Infeasible("protected destination")
        if not order:
            if vehicle.initial_cargo or vehicle.protected_prefix:
                raise Infeasible("cargo/protected work omitted")
            continue
        deployed += not vehicle.initial_cargo and not incumbent.get(vehicle_id)
        cargo = {b.lot_id: b.quantity for b in vehicle.initial_cargo}
        collected = set(cargo)
        clock = max(vehicle.available_at, vehicle.shift.start)
        node = vehicle.start_node_id
        matrix = matrices[vehicle.matrix_id]
        indexes = {n: i for i, n in enumerate(matrix.node_ids)}

        def load_fits(cargo=cargo, vehicle=vehicle):
            return all(
                sum(lots[lot].unit_load[d] * count for lot, count in cargo.items()) <= capacity
                for d, capacity in enumerate(vehicle.capacity)
            )

        if not load_fits():
            raise Infeasible("initial capacity")
        for stop_id in order:
            stop = stops[stop_id]
            if vehicle_id not in stop.eligible_vehicle_ids or (
                stop.bound_vehicle_id is not None and stop.bound_vehicle_id != vehicle_id
            ):
                raise Infeasible("ownership/eligibility")
            a, b = indexes[node], indexes[stop.node_id]
            seconds, metres = matrix.travel_seconds[a][b], matrix.distance_m[a][b]
            if seconds is None or metres is None:
                raise Infeasible("unreachable")
            travel += seconds
            distance += metres
            empty += seconds if not any(cargo.values()) else 0
            arrival = clock + seconds
            start = max(arrival, stop.service_start_window.start)
            if stop.kind == "DISCHARGE":
                point = points[stop.receiving_point_id]
                if (
                    not point.available
                    or stop.receiving_point_id
                    not in lots[stop.lot_id].authorized_receiving_point_ids
                ):
                    raise Infeasible("receiving authority")
                options = [
                    max(start, w.start)
                    for w in point.opening_windows
                    if max(start, w.start) + stop.service_seconds <= w.end
                ]
                if not options:
                    raise Infeasible("receiving hours")
                start = min(options)
            if start > stop.service_start_window.end:
                raise Infeasible("customer window")
            clock = start + stop.service_seconds
            if clock > vehicle.shift.end:
                raise Infeasible("shift")
            waiting += start - arrival
            slacks.append(stop.service_start_window.end - start)
            if stop.kind == "PICKUP":
                if stop.lot_id in collected:
                    raise Infeasible("duplicate pickup")
                cargo[stop.lot_id] = stop.quantity
                collected.add(stop.lot_id)
            else:
                if cargo.get(stop.lot_id, 0) < stop.quantity:
                    raise Infeasible("custody/discharge before collection")
                cargo[stop.lot_id] -= stop.quantity
                if clock > lots[stop.lot_id].discharge_deadline:
                    raise Infeasible("custody deadline")
            if not load_fits():
                raise Infeasible("capacity")
            node = stop.node_id
        if any(cargo.values()):
            raise Infeasible("undischarged cargo")
        a, b = indexes[node], indexes[vehicle.end_node_id]
        seconds, metres = matrix.travel_seconds[a][b], matrix.distance_m[a][b]
        if seconds is None or metres is None or clock + seconds > vehicle.shift.end:
            raise Infeasible("return journey")
        travel += seconds
        distance += metres
        empty += seconds
    if any(
        v.initial_cargo or v.protected_prefix
        for v in problem.vehicles
        if v.vehicle_id not in orders
    ):
        raise Infeasible("unused cargo vehicle")
    debt = {stops[s].lot_id for s in missing}
    deadlines = sorted(
        (
            s.service_start_window.end
            for s in problem.stops
            if s.kind == "PICKUP" and s.lot_id in debt
        ),
        reverse=True,
    )
    disruption = 0
    for vehicle_id, old in incumbent.items():
        previous = [s for s in old if s in stops]
        retained = [s for s in orders.get(vehicle_id, ()) if s in previous]
        disruption += len(previous) - len(retained)
        disruption += sum(previous[i] != s for i, s in enumerate(retained))
    risk = sum(max(0, weights.buffer - slack) for slack in slacks)
    return Score(
        len(debt),
        tuple(-d for d in deadlines),
        travel,
        distance,
        waiting,
        empty,
        deployed,
        disruption,
        min(slacks) if slacks else None,
        risk,
        len(
            {
                stops[stop_id].lot_id
                for vehicle_id, route in orders.items()
                if vehicles[vehicle_id].resource_kind == "FLEXIBLE"
                for stop_id in route
                if stops[stop_id].kind == "PICKUP" and stops[stop_id].bound_vehicle_id is None
            }
        ),
        travel
        + weights.activation * deployed
        + weights.waiting * waiting
        + weights.disruption * disruption
        + weights.risk * risk,
    )


def enumerate_feasible(problem: RoutingProblem, weights: Weights, incumbent):
    """At most three new lots plus one cargo lot, two vehicles: explicitly exponential."""
    vehicles = tuple(v.vehicle_id for v in problem.vehicles)
    members = {
        lot.lot_id: tuple(s.stop_id for s in problem.stops if s.lot_id == lot.lot_id)
        for lot in problem.lots
    }
    for assignment in product((-1, *range(len(vehicles))), repeat=len(members)):
        groups = [[] for _ in vehicles]
        for index, pair in zip(assignment, members.values(), strict=True):
            if index != -1:
                groups[index].extend(pair)
        for routes in product(*(permutations(g) for g in groups)):
            orders = {v: r for v, r in zip(vehicles, routes, strict=True) if r}
            try:
                score = evaluate(problem, orders, weights, incumbent)
            except Infeasible:
                continue
            yield orders, score
