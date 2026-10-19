from dataclasses import replace
from uuid import UUID

import pytest

from tirodhan.modules.planning.routing.adapters import VROOM_VERSION
from tirodhan.modules.planning.routing.contracts import (
    OptimizerEngine,
    ProposedRoute,
    RouteProposal,
    TimeWindow,
)
from tirodhan.modules.planning.routing.validation import RoutingValidationError, validate_proposal
from tirodhan.modules.routing_operations.admission import insert_obligation


def seed(problem):
    # Existing cargo has two immutable discharge obligations, one protected.
    return RouteProposal(
        problem.problem_id,
        problem.snapshot_id,
        problem.contract_version,
        problem.policy_version,
        OptimizerEngine.VROOM,
        VROOM_VERSION,
        (ProposedRoute(UUID(int=30), (UUID(int=50), UUID(int=52))),),
        (UUID(int=51), UUID(int=53)),
    )


def test_inserts_whole_new_household_preserving_cargo_and_protected_order(routing_problem):
    result = insert_obligation(
        routing_problem, seed(routing_problem), UUID(int=42), max_evaluations=100
    )
    assert result is not None
    assessment = validate_proposal(routing_problem, result)
    order = result.routes[0].stop_ids
    assert order[0] == UUID(int=50)
    assert tuple(s for s in order if s in (UUID(int=50), UUID(int=52))) == (
        UUID(int=50),
        UUID(int=52),
    )
    assert order.index(UUID(int=51)) < order.index(UUID(int=53))
    assert assessment.routes[0].stops[-1].load_after == (0, 0)
    assert result.unserved_stop_ids == ()


def test_missing_incumbent_cannot_be_hidden_as_incremental_certificate(routing_problem):
    broken = replace(seed(routing_problem), routes=(ProposedRoute(UUID(int=30), (UUID(int=50),)),))
    with pytest.raises(RoutingValidationError):
        insert_obligation(routing_problem, broken, UUID(int=42), max_evaluations=100)


def test_unused_vehicle_without_time_to_return_does_not_block_healthy_vehicle(routing_problem):
    first = replace(
        routing_problem.vehicles[0],
        initial_cargo=(),
        protected_prefix=(),
        end_node_id=UUID(int=3),
        shift=TimeWindow(0, 1),
    )
    healthy = replace(
        first,
        vehicle_id=UUID(int=33),
        rider_id=UUID(int=34),
        operating_shift_id=UUID(int=35),
        shift=TimeWindow(0, 1000),
    )
    problem = replace(
        routing_problem,
        vehicles=(first, healthy),
        lots=tuple(lot for lot in routing_problem.lots if lot.lot_id == UUID(int=42)),
        stops=tuple(
            replace(
                s,
                eligible_vehicle_ids=(first.vehicle_id, healthy.vehicle_id),
                bound_vehicle_id=None,
            )
            for s in routing_problem.stops
            if s.lot_id == UUID(int=42)
        ),
    )
    empty = replace(
        seed(problem), routes=(), unserved_stop_ids=tuple(s.stop_id for s in problem.stops)
    )
    result = insert_obligation(problem, empty, UUID(int=42), max_evaluations=100)
    assert result is not None
    assert {r.vehicle_id for r in result.routes if r.stop_ids} == {healthy.vehicle_id}
    validate_proposal(problem, result)


def test_exhaustion_or_infeasible_new_window_never_certifies_capacity(routing_problem):
    assert (
        insert_obligation(routing_problem, seed(routing_problem), UUID(int=42), max_evaluations=0)
        is None
    )
    impossible = replace(
        routing_problem,
        stops=tuple(
            replace(s, service_start_window=TimeWindow(0, 0)) if s.stop_id == UUID(int=51) else s
            for s in routing_problem.stops
        ),
    )
    assert (
        insert_obligation(impossible, seed(impossible), UUID(int=42), max_evaluations=100) is None
    )
