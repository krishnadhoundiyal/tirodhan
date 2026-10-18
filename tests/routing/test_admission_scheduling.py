"""Independent enumeration checks pruning, calendars, zero arcs and resumable work."""

import random
import time
from dataclasses import replace
from uuid import UUID

import pytest
from test_admission_insertion import seed

from tirodhan.modules.planning.routing.contracts import (
    ROUTING_CONTRACT_V2,
    ProposedRoute,
    TimeWindow,
)
from tirodhan.modules.planning.routing.scheduling import RouteSchedule, exclude_breaks
from tirodhan.modules.planning.routing.validation import (
    RoutingValidationError,
    avoid_breaks,
    validate_proposal,
)


@pytest.mark.parametrize("duration", range(12))
def test_calendar_forward_and_inverse_match_exhaustive_integer_starts(duration):
    breaks = (TimeWindow(10, 20), TimeWindow(25, 29))
    intervals = exclude_breaks([(0, 45)], duration, breaks)
    valid = [t for t in range(46) if avoid_breaks(t, duration, breaks) == t]
    for t in range(46):
        assert intervals.next(t) == next((x for x in valid if x >= t), None)
        assert intervals.previous(t) == next((x for x in reversed(valid) if x <= t), None)


def test_enumeration_matches_independent_complete_gate(routing_problem):
    rng = random.Random(181026)
    old = (UUID(int=50), UUID(int=52))
    comparisons = 0
    for _ in range(100):
        values = tuple(
            tuple(0 if i == j else rng.randrange(0, 25) for j in range(4)) for i in range(4)
        )
        problem = replace(
            routing_problem,
            contract_version=ROUTING_CONTRACT_V2,
            matrices=(replace(routing_problem.matrices[0], travel_seconds=values),),
            vehicles=(replace(routing_problem.vehicles[0], breaks=(TimeWindow(30, 50),)),),
            stops=tuple(
                replace(s, service_start_window=TimeWindow(50, rng.randrange(80, 200)))
                if s.stop_id == UUID(int=51)
                else s
                for s in routing_problem.stops
            ),
        )
        original = seed(problem)
        optional = replace(
            problem,
            stops=tuple(
                replace(s, required=False) if s.lot_id == UUID(int=42) else s for s in problem.stops
            ),
        )
        validate_proposal(optional, original)
        expected = None
        for i in range(1, len(old) + 1):
            for j in range(i, len(old) + 1):
                order = old[:i] + (UUID(int=51),) + old[i:j] + (UUID(int=53),) + old[j:]
                proposal = replace(
                    original, routes=(ProposedRoute(UUID(int=30), order),), unserved_stop_ids=()
                )
                try:
                    validate_proposal(problem, proposal)
                    expected = expected or order
                except RoutingValidationError:
                    pass
        schedule = RouteSchedule(problem, problem.vehicles[0], old)
        result = schedule.insert(
            problem.stops[1], problem.stops[3], max_evaluations=100, deadline=time.monotonic() + 10
        )
        assert result.order == expected
        cursor = None
        for _ in range(10):
            resumed = schedule.insert(
                problem.stops[1],
                problem.stops[3],
                max_evaluations=1,
                deadline=time.monotonic() + 10,
                cursor=cursor,
            )
            if resumed.order is not None or resumed.exhausted:
                assert resumed.order == expected
                break
            cursor = resumed.cursor
        else:
            pytest.fail("search cursor failed to make progress")
        comparisons += 1
    assert comparisons == 100
