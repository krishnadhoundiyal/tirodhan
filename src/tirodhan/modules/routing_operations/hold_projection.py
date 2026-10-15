"""Capacity holds constrain forecasts but cannot appear in executable routes or ETAs."""

from dataclasses import replace
from uuid import UUID

from tirodhan.modules.planning.routing.contracts import ProposedRoute, RouteProposal, RoutingProblem


def executable_projection(
    problem: RoutingProblem,
    proposal: RouteProposal,
    held_lots: set[UUID],
) -> tuple[RoutingProblem, RouteProposal]:
    held_stops = {s.stop_id for s in problem.stops if s.lot_id in held_lots}
    actual = replace(
        problem,
        lots=tuple(lot for lot in problem.lots if lot.lot_id not in held_lots),
        stops=tuple(s for s in problem.stops if s.stop_id not in held_stops),
    )
    routes = tuple(
        ProposedRoute(r.vehicle_id, tuple(s for s in r.stop_ids if s not in held_stops))
        for r in proposal.routes
        if any(s not in held_stops for s in r.stop_ids)
    )
    return actual, replace(
        proposal,
        routes=routes,
        unserved_stop_ids=tuple(s for s in proposal.unserved_stop_ids if s not in held_stops),
    )
