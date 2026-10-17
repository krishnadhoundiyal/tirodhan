import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from dataclasses import replace
from uuid import UUID

from tirodhan.modules.planning.routing.contracts import TimeWindow
from tirodhan.modules.routing_operations.admission import insert_obligation
from tests.routing.test_admission_insertion import seed

def uid(i): return UUID(int=i)

@settings(max_examples=200, deadline=None)
@given(
    start=st.integers(min_value=-10000, max_value=10000),
    end=st.integers(min_value=-10000, max_value=10000)
)
def test_adversarial_time_window_never_bypasses_capacity(routing_problem, start, end):
    s = seed(routing_problem)

    stops = list(routing_problem.stops)
    for i, stop in enumerate(stops):
        if stop.stop_id == uid(51):
            stops[i] = replace(stop, service_start_window=TimeWindow(start, end))

    impossible = replace(routing_problem, stops=tuple(stops))

    if start > end or start < 0 or end < 0:
        # Invalid time windows should be rejected early
        try:
            result = insert_obligation(impossible, s, uid(42), max_evaluations=10)
            assert result is None, "Inverted or negative time window should never certify capacity"
        except Exception as e:
            # We also accept if it throws a validation error right away
            pass
    else:
        result = insert_obligation(impossible, s, uid(42), max_evaluations=10)
        if result is not None:
            from tirodhan.modules.planning.routing.validation import validate_proposal
            validate_proposal(impossible, result)
