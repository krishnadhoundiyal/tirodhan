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
