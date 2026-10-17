"""Independent fixed-schedule shared receiving-point capacity validation."""

from collections import defaultdict
from typing import Any, cast
from uuid import UUID

from .contracts import RoutingProblem
from .validation import ProposalAssessment, RoutingValidationError


def validate_sources(
    problem: RoutingProblem, assessment: ProposalAssessment, sources: dict[str, Any]
) -> None:
    limits = {
        point["receiving_point_id"]: point["unloading_capacity"]
        for point in sources["points"]
        if point.get("unloading_capacity") is not None
    }
    stops = {stop.stop_id: stop for stop in problem.stops}
    visits = tuple(
        (
            cast(UUID, stops[stop.stop_id].receiving_point_id),
            stop.service_start_at,
            stop.service_end_at,
        )
        for route in assessment.routes
        for stop in route.stops
        if stops[stop.stop_id].receiving_point_id is not None
    )
    external = tuple(
        (
            visit["receiving_point_id"],
            int((visit["planned_start"] - problem.epoch).total_seconds()),
            int((visit["planned_end"] - problem.epoch).total_seconds()),
        )
        for visit in sources.get("external_facility_visits", [])
    )
    validate_capacity(limits, visits + external)


def validate_capacity(limits: dict[UUID, int], visits: tuple[tuple[UUID, int, int], ...]) -> None:
    events: dict[UUID, list[tuple[int, int]]] = defaultdict(list)
    for point, start, end in visits:
        if point in limits and end > start:
            events[point].extend(((start, 1), (end, -1)))
    for point, changes in events.items():
        active = 0
        for _, delta in sorted(changes):  # Half-open intervals: endings precede starts.
            active += delta
            if active > limits[point]:
                raise RoutingValidationError("FACILITY_UNLOADING_CAPACITY")
