from tirodhan.modules.planning.routing.adapters import VROOM_VERSION
from tirodhan.modules.planning.routing.contracts import (
    OptimizerEngine,
    ProposedRoute,
    RouteProposal,
)
from tirodhan.modules.routing_operations.economics import EconomicPolicy


def policy(weights):
    return EconomicPolicy(
        version="INDEPENDENT_TEST",
        additional_vehicle_seconds=weights.activation,
        waiting_weight=weights.waiting,
        disruption_seconds=weights.disruption,
        window_risk_buffer_seconds=weights.buffer,
        window_risk_weight=weights.risk,
        max_candidate_evaluations=20000,
    )


def proposal(problem, orders):
    served = {s for route in orders.values() for s in route}
    return RouteProposal(
        problem.problem_id,
        problem.snapshot_id,
        problem.contract_version,
        problem.policy_version,
        OptimizerEngine.VROOM,
        VROOM_VERSION,
        tuple(ProposedRoute(v, tuple(order)) for v, order in orders.items() if order),
        tuple(s.stop_id for s in problem.stops if s.stop_id not in served),
    )
