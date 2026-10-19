from dataclasses import replace
from uuid import UUID

import pytest
from test_admission_insertion import seed

from tirodhan.modules.planning.routing.certificates import compose
from tirodhan.modules.planning.routing.contracts import ProposedRoute
from tirodhan.modules.planning.routing.validation import RoutingValidationError, validate_proposal
from tirodhan.modules.routing_operations.admission import insert_obligation


def test_composition_reuses_only_unchanged_valid_route_proofs(routing_problem):
    proposal = insert_obligation(
        routing_problem, seed(routing_problem), UUID(int=42), max_evaluations=100
    )
    proofs = compose(routing_problem, proposal, {})
    assert compose(routing_problem, proposal, proofs)[UUID(int=30)] is proofs[UUID(int=30)]
    latest = proofs[UUID(int=30)].latest_entry
    for available in range(latest + 2):
        problem = replace(
            routing_problem,
            vehicles=(replace(routing_problem.vehicles[0], available_at=available),),
        )
        try:
            validate_proposal(problem, proposal)
            expected = True
        except RoutingValidationError:
            expected = False
        assert (available <= latest) == expected
        if expected:
            compose(problem, proposal, proofs)
        else:
            with pytest.raises(RoutingValidationError):
                compose(problem, proposal, proofs)


def test_composition_rejects_missing_duplicate_and_changed_obligations(routing_problem):
    proposal = insert_obligation(
        routing_problem, seed(routing_problem), UUID(int=42), max_evaluations=100
    )
    proofs = compose(routing_problem, proposal, {})
    broken = replace(
        proposal, routes=(ProposedRoute(UUID(int=30), proposal.routes[0].stop_ids[:-1]),)
    )
    with pytest.raises(RoutingValidationError):
        compose(routing_problem, broken, proofs)
    broken = replace(proposal, routes=proposal.routes * 2)
    with pytest.raises(RoutingValidationError):
        compose(routing_problem, broken, proofs)
    changed = replace(
        routing_problem,
        lots=tuple(replace(lot, unit_load=(100, 100)) for lot in routing_problem.lots),
    )
    with pytest.raises(RoutingValidationError):
        compose(changed, proposal, proofs)
