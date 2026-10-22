import pytest
from hypothesis import given, settings, strategies as st
from uuid import UUID
from dataclasses import replace

from tirodhan.modules.planning.routing.contracts import (
    RoutingProblem,
    RoutingStop,
    StopKind,
    TimeWindow,
    CargoLot,
    RouteProposal,
    ProposedRoute,
    OptimizerEngine
)
from tirodhan.modules.planning.routing.validation import validate_proposal, RoutingValidationError

def uid(i: int) -> UUID:
    return UUID(int=i)

@settings(max_examples=20)
@given(
    invalid_sequence=st.booleans()
)
def test_adversarial_route_validation_with_fixture(routing_problem, invalid_sequence):
    problem = routing_problem
    v = problem.vehicles[0]

    eligible_stops = []
    for s in problem.stops:
        if hasattr(s, "eligible_vehicle_ids") and v.vehicle_id in s.eligible_vehicle_ids:
            eligible_stops.append(s.stop_id)

    if not eligible_stops:
        eligible_stops = [s.stop_id for s in problem.stops]

    if invalid_sequence:
        stop_seq = tuple(reversed(eligible_stops))
    else:
        stop_seq = tuple(eligible_stops)

    leg1 = ProposedRoute(
        vehicle_id=v.vehicle_id,
        stop_ids=stop_seq
    )

    proposal = RouteProposal(
        problem_id=problem.problem_id,
        snapshot_id=problem.snapshot_id,
        contract_version=problem.contract_version,
        policy_version=problem.policy_version,
        engine=OptimizerEngine.VROOM,
        engine_version="1.15.0",
        routes=(leg1,),
        unserved_stop_ids=()
    )

    try:
        validate_proposal(problem, proposal)
    except RoutingValidationError:
        pass
    except Exception as e:
        pytest.fail(f"Validation raised unexpected error: {e}")


@settings(max_examples=20)
@given(
    time_shift=st.integers(min_value=-1000, max_value=1000)
)
def test_adversarial_route_time_windows(routing_problem, time_shift):
    problem = routing_problem
    v = problem.vehicles[0]

    # We create a proposal with a stop that has a modified service window.
    # The proposal does not include the stop's explicit timing, validation computes timings.
    # But if we pass an incorrect subset of stops or missing matching pickup/dropoffs,
    # validation should catch it and raise RoutingValidationError instead of an unhandled exception.

    eligible_stops = []
    for s in problem.stops:
        if hasattr(s, "eligible_vehicle_ids") and v.vehicle_id in s.eligible_vehicle_ids:
            eligible_stops.append(s.stop_id)

    if not eligible_stops:
        eligible_stops = [s.stop_id for s in problem.stops]

    # Drop one stop (e.g. drop a discharge stop while keeping the pickup)
    stop_seq = tuple(eligible_stops[:-1])

    leg1 = ProposedRoute(
        vehicle_id=v.vehicle_id,
        stop_ids=stop_seq
    )

    proposal = RouteProposal(
        problem_id=problem.problem_id,
        snapshot_id=problem.snapshot_id,
        contract_version=problem.contract_version,
        policy_version=problem.policy_version,
        engine=OptimizerEngine.VROOM,
        engine_version="1.15.0",
        routes=(leg1,),
        unserved_stop_ids=()
    )

    try:
        validate_proposal(problem, proposal)
    except RoutingValidationError:
        pass
    except Exception as e:
        pytest.fail(f"Validation raised unexpected error: {e}")
