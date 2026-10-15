"""Evaluate committed capacity collectively before accepted supplementary capacity."""

from dataclasses import replace

from tirodhan.modules.planning.routing.contracts import (
    ResourceKind,
    RouteOptimizer,
    RouteProposal,
    RoutingProblem,
)
from tirodhan.modules.planning.routing.execution import RoutingAdapterError
from tirodhan.modules.planning.routing.validation import RoutingValidationError, validate_proposal

from .economics import EconomicPolicy, measure


async def propose_prioritized(
    optimizer: RouteOptimizer,
    problem: RoutingProblem,
    *,
    search_seconds: int,
    economics: EconomicPolicy,
) -> RouteProposal:
    supplementary = {
        v.vehicle_id for v in problem.vehicles if v.resource_kind == ResourceKind.FLEXIBLE
    }
    if not supplementary:
        return await optimizer.propose(problem, search_seconds=search_seconds)
    # Existing flexible commitments and cargo remain bound. Only new unbound work is tiered.
    primary = replace(
        problem,
        stops=tuple(
            replace(
                s,
                eligible_vehicle_ids=tuple(
                    v for v in s.eligible_vehicle_ids if v not in supplementary
                ),
            )
            if s.bound_vehicle_id is None
            else s
            for s in problem.stops
        ),
    )
    candidates: list[RouteProposal] = []
    try:
        if any(s.required and not s.eligible_vehicle_ids for s in primary.stops):
            raise RoutingAdapterError("NO_CONFIRMED_CAPACITY")
        proposal = await optimizer.propose(primary, search_seconds=search_seconds)
        validate_proposal(problem, proposal)
        candidates.append(proposal)
        if not proposal.unserved_stop_ids:
            return proposal
    except (RoutingAdapterError, RoutingValidationError):
        pass
    proposal = await optimizer.propose(problem, search_seconds=search_seconds)
    validate_proposal(problem, proposal)
    candidates.append(proposal)
    return min(candidates, key=lambda p: measure(problem, p, economics, {}).rank)
