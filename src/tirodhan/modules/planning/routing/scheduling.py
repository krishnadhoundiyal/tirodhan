"""Fixed-order PDPTW insertion by forward propagation and backward suffix bounds.

Savelsbergh (1992), Campbell/Savelsbergh (2004). Calendars are represented as
integer interval sets, not a linear-delay approximation across breaks/openings.
The independent validator never consumes these search summaries as evidence.
"""

from __future__ import annotations

import time
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from uuid import UUID

from .contracts import RoutingProblem, RoutingStop, StopKind, TimeWindow, VehicleSnapshot
from .validation import RoutingValidationError, ValidationContext


@dataclass(frozen=True, slots=True)
class Intervals:
    ranges: tuple[tuple[int, int], ...]
    starts: tuple[int, ...]
    ends: tuple[int, ...]

    @classmethod
    def make(cls, ranges: list[tuple[int, int]]) -> Intervals:
        merged: list[tuple[int, int]] = []
        for a, b in sorted(ranges):
            if a > b:
                continue
            if merged and a <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
            else:
                merged.append((a, b))
        return cls(tuple(merged), tuple(a for a, _ in merged), tuple(b for _, b in merged))

    def next(self, value: int) -> int | None:
        i = bisect_left(self.ends, value)
        return max(value, self.starts[i]) if i < len(self.ranges) else None

    def previous(self, value: int) -> int | None:
        i = bisect_right(self.starts, value) - 1
        return min(value, self.ends[i]) if i >= 0 else None


def exclude_breaks(
    ranges: list[tuple[int, int]], duration: int, breaks: tuple[TimeWindow, ...]
) -> Intervals:
    # Match avoid_breaks exactly, including its existing zero-duration semantics.
    for pause in breaks:
        low, high = pause.start - duration + 1, pause.end - 1
        remaining = []
        for a, b in ranges:
            if high < a or low > b:
                remaining.append((a, b))
            else:
                remaining.extend(((a, min(b, low - 1)), (max(a, high + 1), b)))
        ranges = [(a, b) for a, b in remaining if a <= b]
    return Intervals.make(ranges)


@dataclass(frozen=True, slots=True)
class InsertionCursor:
    pickup_gap: int = 0
    discharge_gap: int = 0


@dataclass(frozen=True, slots=True)
class InsertionResult:
    order: tuple[UUID, ...] | None
    cursor: InsertionCursor | None
    exhausted: bool
    evaluations: int
    prefix_prunes: int


class RouteSchedule:
    """Linear route state; search is O(n²(D + log h)), excluding road acquisition."""

    def __init__(
        self,
        problem: RoutingProblem,
        vehicle: VehicleSnapshot,
        order: tuple[UUID, ...],
        context: ValidationContext | None = None,
    ):
        self.problem, self.vehicle, self.order = problem, vehicle, order
        context = context or ValidationContext.compile(problem)
        self.stops, self.lots, self.points = context.stops, context.lots, context.facilities
        self.matrix = context.matrices[vehicle.matrix_id]
        self.indices = context.indices[vehicle.matrix_id]
        self.departures: dict[int, Intervals] = {}
        self.windows: dict[UUID, Intervals] = {}
        self.nodes = [vehicle.start_node_id] + [self.stops[s].node_id for s in order]
        self.endings = [vehicle.available_at]
        initial = {c.lot_id: c.quantity for c in vehicle.initial_cargo}
        load = tuple(
            sum(q * self.lots[lot].unit_load[d] for lot, q in initial.items())
            for d in range(len(problem.load_dimensions))
        )
        self.loads = [load]
        for identity in order:
            stop = self.stops[identity]
            end = self.visit(self.nodes[len(self.endings) - 1], self.endings[-1], stop)
            if end is None:
                raise RoutingValidationError("INCUMBENT_SCHEDULE_INVALID")
            sign = 1 if stop.kind == StopKind.PICKUP else -1
            load = tuple(
                x + sign * stop.quantity * q
                for x, q in zip(load, self.lots[stop.lot_id].unit_load, strict=True)
            )
            self.endings.append(end)
            self.loads.append(load)
        self.latest = [0] * len(order)
        if not order and not vehicle.initial_cargo:
            # An unused vehicle has no return journey in the independent contract.
            # Check the actual pickup/discharge/end path when inserting work below;
            # an impossible empty return must not abort other available resources.
            self.latest_entry = vehicle.shift.end
            return
        node, limit = vehicle.end_node_id, vehicle.shift.end
        for i in range(len(order) - 1, -1, -1):
            stop = self.stops[order[i]]
            duration = self.duration(stop.node_id, node)
            departure = self.allowed_departures(duration).previous(limit - duration)
            latest = (
                self.allowed_starts(stop).previous(departure - stop.service_seconds)
                if departure is not None
                else None
            )
            if latest is None:
                raise RoutingValidationError("INCUMBENT_SCHEDULE_INVALID")
            self.latest[i] = latest
            node, limit = stop.node_id, latest
        duration = self.duration(vehicle.start_node_id, node)
        latest_entry = self.allowed_departures(duration).previous(limit - duration)
        if latest_entry is None:
            raise RoutingValidationError("INCUMBENT_SCHEDULE_INVALID")
        self.latest_entry = latest_entry

    def duration(self, a: UUID, b: UUID) -> int:
        try:
            value, _ = self.matrix.arc(a, b, self.indices)
        except KeyError:
            raise RoutingValidationError("ROAD_ARC_NOT_ACQUIRED") from None
        if value is None:
            raise RoutingValidationError("UNREACHABLE_ARC")
        return value

    def allowed_departures(self, duration: int) -> Intervals:
        if duration not in self.departures:
            self.departures[duration] = exclude_breaks(
                [(0, self.vehicle.shift.end)], duration, self.vehicle.breaks
            )
        return self.departures[duration]

    def allowed_starts(self, stop: RoutingStop) -> Intervals:
        if stop.stop_id not in self.windows:
            low, high = stop.service_start_window.start, stop.service_start_window.end
            if stop.kind == StopKind.DISCHARGE:
                point = (
                    self.points.get(stop.receiving_point_id) if stop.receiving_point_id else None
                )
                if point is None or not point.available:
                    ranges: list[tuple[int, int]] = []
                else:
                    high = min(
                        high, self.lots[stop.lot_id].discharge_deadline - stop.service_seconds
                    )
                    ranges = [
                        (max(low, w.start), min(high, w.end - stop.service_seconds))
                        for w in point.opening_windows
                    ]
            else:
                ranges = [(low, high)]
            self.windows[stop.stop_id] = exclude_breaks(
                ranges, stop.service_seconds, self.vehicle.breaks
            )
        return self.windows[stop.stop_id]

    def arrive(self, origin: UUID, end: int, target: UUID) -> int | None:
        try:
            duration = self.duration(origin, target)
        except RoutingValidationError:
            return None
        departure = self.allowed_departures(duration).next(end)
        return departure + duration if departure is not None else None

    def visit(self, origin: UUID, end: int, stop: RoutingStop) -> int | None:
        arrival = self.arrive(origin, end, stop.node_id)
        start = self.allowed_starts(stop).next(arrival) if arrival is not None else None
        return start + stop.service_seconds if start is not None else None

    def insert(
        self,
        pickup: RoutingStop,
        discharge: RoutingStop,
        *,
        max_evaluations: int,
        deadline: float,
        cursor: InsertionCursor | None = None,
    ) -> InsertionResult:
        vehicle, old = self.vehicle, self.order
        if any(
            vehicle.vehicle_id not in stop.eligible_vehicle_ids
            or stop.bound_vehicle_id not in (None, vehicle.vehicle_id)
            for stop in (pickup, discharge)
        ):
            return InsertionResult(None, None, True, 0, 0)
        quantity = tuple(pickup.quantity * q for q in self.lots[pickup.lot_id].unit_load)
        evaluations = prunes = 0
        first = max(len(vehicle.protected_prefix), cursor.pickup_gap if cursor else 0)
        for i in range(first, len(old) + 1):
            end = self.visit(self.nodes[i], self.endings[i], pickup)
            if end is None or any(
                a + b > cap
                for a, b, cap in zip(self.loads[i], quantity, vehicle.capacity, strict=True)
            ):
                prunes += 1
                continue
            node = pickup.node_id
            for j in range(i, len(old) + 1):
                # Reconstruct the carried prefix on resume; never repeat full validation.
                if not cursor or i != cursor.pickup_gap or j >= cursor.discharge_gap:
                    if evaluations >= max_evaluations or time.monotonic() >= deadline:
                        return InsertionResult(
                            None, InsertionCursor(i, j), False, evaluations, prunes
                        )
                    evaluations += 1
                    dropped = self.visit(node, end, discharge)
                    if dropped is not None:
                        target = self.stops[old[j]].node_id if j < len(old) else vehicle.end_node_id
                        arrival = self.arrive(discharge.node_id, dropped, target)
                        bound = self.latest[j] if j < len(old) else vehicle.shift.end
                        if arrival is not None and arrival <= bound:
                            order = (
                                old[:i]
                                + (pickup.stop_id,)
                                + old[i:j]
                                + (discharge.stop_id,)
                                + old[j:]
                            )
                            return InsertionResult(order, None, False, evaluations, prunes)
                if j == len(old):
                    break
                stop = self.stops[old[j]]
                carried = self.visit(node, end, stop)
                if carried is None or any(
                    a + b > cap
                    for a, b, cap in zip(self.loads[j + 1], quantity, vehicle.capacity, strict=True)
                ):
                    prunes += 1
                    break
                node, end = stop.node_id, carried
        return InsertionResult(None, None, True, evaluations, prunes)
