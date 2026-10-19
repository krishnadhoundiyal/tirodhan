"""Independently measured fleet costs and bounded, feasibility-gated insertion search."""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from tirodhan.modules.planning.routing.contracts import (
    ProposedRoute,
    ResourceKind,
    RouteProposal,
    RoutingProblem,
    StopKind,
)
from tirodhan.modules.planning.routing.validation import (
    FeasibilityGate,
    RoutingValidationError,
    validate_proposal,
)


class EconomicPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: str = Field(min_length=1, max_length=100)
    additional_vehicle_seconds: int = Field(strict=True, ge=0, le=86400)
    disruption_seconds: int = Field(strict=True, ge=0, le=86400)
    waiting_weight: int = Field(strict=True, ge=0, le=100)
    window_risk_buffer_seconds: int = Field(strict=True, ge=0, le=86400)
    window_risk_weight: int = Field(strict=True, ge=0, le=100)
    max_candidate_evaluations: int = Field(strict=True, ge=1, le=20000)


@dataclass(frozen=True)
class FleetCost:
    unserved_lots: int
    deadline_risk: tuple[int, ...]
    travel_seconds: int
    distance_m: int
    waiting_seconds: int
    empty_travel_seconds: int
    additional_vehicles: int
    disruptions: int
    minimum_window_slack_seconds: int | None
    window_risk_seconds: int
    supplementary_lots: int
    generalized_seconds: int

    @property
    def rank(self) -> tuple[object, ...]:
        # Coverage first; among equal coverage protect the earliest outstanding promises.
        return (
            self.unserved_lots,
            self.deadline_risk,
            self.supplementary_lots,
            self.generalized_seconds,
            self.distance_m,
            self.disruptions,
        )


def measure(
    problem: RoutingProblem,
    proposal: RouteProposal,
    policy: EconomicPolicy,
    incumbent: dict[UUID, tuple[UUID, ...]],
    gate: FeasibilityGate | None = None,
) -> FleetCost:
    if gate is not None and gate.problem is not problem:
        raise RoutingValidationError("PROPOSAL_SNAPSHOT_MISMATCH")
    assessment = gate.assess(proposal) if gate is not None else validate_proposal(problem, proposal)
    stops = {s.stop_id: s for s in problem.stops}
    vehicles = {v.vehicle_id: v for v in problem.vehicles}
    matrices = {m.matrix_id: m for m in problem.matrices}
    unserved = {stops[s].lot_id for s in proposal.unserved_stop_ids}
    # A served earlier deadline wins: prefer leaving later deadlines outstanding.
    deadlines = sorted(
        (
            s.service_start_window.end
            for s in problem.stops
            if s.lot_id in unserved and s.kind == StopKind.PICKUP
        ),
        reverse=True,
    )
    risk = tuple(-d for d in deadlines)
    waiting = empty = travel = distance = activated = 0
    slack: list[int] = []
    orders = {r.vehicle_id: r.stop_ids for r in proposal.routes}
    for route in assessment.routes:
        if not route.stops:
            continue  # No operational dispatch/return journey for an unused vehicle.
        vehicle = vehicles[route.vehicle_id]
        matrix = matrices[vehicle.matrix_id]
        indexes = {node: i for i, node in enumerate(matrix.node_ids)}
        node = vehicle.start_node_id
        balances = {c.lot_id: c.quantity for c in vehicle.initial_cargo}
        travel += route.travel_seconds
        distance += route.distance_m
        activated += not bool(incumbent.get(route.vehicle_id)) and not bool(vehicle.initial_cargo)
        for scheduled in route.stops:
            stop = stops[scheduled.stop_id]
            seconds, _ = matrix.arc(node, stop.node_id, indexes)
            assert seconds is not None
            if not any(balances.values()):
                empty += seconds
            waiting += scheduled.service_start_at - scheduled.arrival_at
            slack.append(stop.service_start_window.end - scheduled.service_start_at)
            if stop.kind == StopKind.PICKUP:
                balances[stop.lot_id] = stop.quantity
            else:
                balances[stop.lot_id] -= stop.quantity
            node = stop.node_id
        # Explicitly include the mandatory end journey in empty travel too.
        seconds, _ = matrix.arc(node, vehicle.end_node_id, indexes)
        assert seconds is not None
        empty += seconds
    # Compare incumbent relative order/owner, ignoring pure additions and completed history.
    disruptions = 0
    for vehicle_id, old in incumbent.items():
        remaining = tuple(s for s in old if s in stops)
        new = tuple(s for s in orders.get(vehicle_id, ()) if s in remaining)
        disruptions += (
            sum(a != b for a, b in zip(remaining, new, strict=False)) + len(remaining) - len(new)
        )
    risk_seconds = sum(max(0, policy.window_risk_buffer_seconds - seconds) for seconds in slack)
    return FleetCost(
        len(unserved),
        risk,
        travel,
        distance,
        waiting,
        empty,
        activated,
        disruptions,
        min(slack) if slack else None,
        risk_seconds,
        len(
            {
                stops[s].lot_id
                for route in proposal.routes
                if vehicles[route.vehicle_id].resource_kind == ResourceKind.FLEXIBLE
                for s in route.stop_ids
                if stops[s].kind == StopKind.PICKUP and stops[s].bound_vehicle_id is None
            }
        ),
        travel
        + waiting * policy.waiting_weight
        + activated * policy.additional_vehicle_seconds
        + disruptions * policy.disruption_seconds
        + risk_seconds * policy.window_risk_weight,
    )


def make_candidate(
    template: RouteProposal, problem: RoutingProblem, orders: dict[UUID, tuple[UUID, ...]]
) -> RouteProposal:
    served = {s for route in orders.values() for s in route}
    return replace(
        template,
        problem_id=problem.problem_id,
        snapshot_id=problem.snapshot_id,
        contract_version=problem.contract_version,
        policy_version=problem.policy_version,
        routes=tuple(
            ProposedRoute(v, order)
            for v, order in sorted(orders.items(), key=lambda r: str(r[0]))
            if order
        ),
        unserved_stop_ids=tuple(
            sorted((s.stop_id for s in problem.stops if s.stop_id not in served), key=str)
        ),
    )


def insertion_search(
    problem: RoutingProblem,
    seed: RouteProposal,
    policy: EconomicPolicy,
    incumbent: dict[UUID, tuple[UUID, ...]],
    *,
    max_wall_seconds: float | None = None,
) -> tuple[RouteProposal, FleetCost, int]:
    """Whole-household relocate/insertion; every fleet candidate passes the independent gate.

    Repeated sweeps consider lots in deadline order across vehicles and suffix positions.
    Consolidation also runs after newly inserted work; the same total work budget applies.
    Bounds are technical work limits, never permission to drop required commitments.
    """
    best = seed
    gate = FeasibilityGate(problem)
    cost = measure(problem, best, policy, incumbent, gate)
    deadline = time.monotonic() + max_wall_seconds if max_wall_seconds is not None else float("inf")
    evaluations = 0
    by_lot: dict[UUID, list[UUID]] = {}
    stops = {s.stop_id: s for s in problem.stops}
    protected = {s for v in problem.vehicles for s in v.protected_prefix}
    for stop in problem.stops:
        by_lot.setdefault(stop.lot_id, []).append(stop.stop_id)
    while evaluations < policy.max_candidate_evaluations and time.monotonic() < deadline:
        before = cost.rank
        # A one-lot move may cost more while moving several future lots together saves a
        # complete vehicle journey. Evaluate blocks before individual insertions to escape
        # that fleet-wide local minimum, without moving a protected/custodied household.
        vehicle_map = {v.vehicle_id: v for v in problem.vehicles}
        for donor in problem.vehicles:
            orders = {r.vehicle_id: r.stop_ids for r in best.routes}
            old = orders.get(donor.vehicle_id, ())
            movable_lots = {
                lot
                for lot, pair in by_lot.items()
                if len(pair) == 2
                and all(s in old for s in pair)
                and not protected.intersection(pair)
                and any(stops[s].kind == StopKind.PICKUP for s in pair)
            }
            block = tuple(s for s in old if stops[s].lot_id in movable_lots)
            if not block:
                continue
            for recipient in problem.vehicles:
                if recipient.vehicle_id == donor.vehicle_id or any(
                    recipient.vehicle_id not in stops[s].eligible_vehicle_ids for s in block
                ):
                    continue
                target = orders.get(recipient.vehicle_id, ())
                for i in range(
                    len(vehicle_map[recipient.vehicle_id].protected_prefix), len(target) + 1
                ):
                    if (
                        evaluations >= policy.max_candidate_evaluations
                        or time.monotonic() >= deadline
                    ):
                        return best, cost, evaluations
                    evaluations += 1
                    changed = dict(orders)
                    changed[donor.vehicle_id] = tuple(s for s in old if s not in block)
                    changed[recipient.vehicle_id] = target[:i] + block + target[i:]
                    candidate = make_candidate(best, problem, changed)
                    try:
                        measured = measure(problem, candidate, policy, incumbent, gate)
                    except RoutingValidationError:
                        continue
                    if measured.rank < cost.rank:
                        best, cost = candidate, measured
        lots = sorted(
            by_lot,
            key=lambda lot: (min(stops[s].service_start_window.end for s in by_lot[lot]), str(lot)),
        )
        for lot in lots:
            pair = by_lot[lot]
            pickup = next((s for s in pair if stops[s].kind == StopKind.PICKUP), None)
            if pickup is None or len(pair) != 2 or protected.intersection(pair):
                continue
            discharge = next(s for s in pair if s != pickup)
            orders = {r.vehicle_id: r.stop_ids for r in best.routes}
            residual = {
                v.vehicle_id: tuple(s for s in orders.get(v.vehicle_id, ()) if s not in pair)
                for v in problem.vehicles
            }
            for vehicle in problem.vehicles:
                if vehicle.vehicle_id not in stops[pickup].eligible_vehicle_ids:
                    continue
                suffix = residual[vehicle.vehicle_id]
                for i in range(len(vehicle.protected_prefix), len(suffix) + 1):
                    with_pickup = suffix[:i] + (pickup,) + suffix[i:]
                    for j in range(i + 1, len(with_pickup) + 1):
                        if (
                            evaluations >= policy.max_candidate_evaluations
                            or time.monotonic() >= deadline
                        ):
                            return best, cost, evaluations
                        evaluations += 1
                        changed = dict(residual)
                        changed[vehicle.vehicle_id] = (
                            with_pickup[:j] + (discharge,) + with_pickup[j:]
                        )
                        candidate = make_candidate(best, problem, changed)
                        try:
                            measured = measure(problem, candidate, policy, incumbent, gate)
                        except RoutingValidationError:
                            continue
                        if measured.rank < cost.rank:
                            best, cost = candidate, measured
        if cost.rank == before:
            break
    return best, cost, evaluations


def nearest_feasible_baseline(
    problem: RoutingProblem,
    seed: RouteProposal,
    policy: EconomicPolicy,
    incumbent: dict[UUID, tuple[UUID, ...]],
) -> tuple[RouteProposal, FleetCost]:
    """Deadline-order, nearest-start vehicle, append-only pairs; comparison baseline only."""
    candidate = seed
    gate = FeasibilityGate(problem)
    matrices = {m.matrix_id: m for m in problem.matrices}
    optional = sorted(
        (s for s in problem.stops if s.kind == StopKind.PICKUP and not s.required),
        key=lambda s: (s.service_start_window.end, str(s.stop_id)),
    )
    for pickup in optional:
        discharge = next(
            s for s in problem.stops if s.lot_id == pickup.lot_id and s.kind == StopKind.DISCHARGE
        )
        ranked = []
        for vehicle in problem.vehicles:
            if vehicle.vehicle_id not in pickup.eligible_vehicle_ids:
                continue
            matrix = matrices[vehicle.matrix_id]
            distance = matrix.distance_m[matrix.node_ids.index(vehicle.start_node_id)][
                matrix.node_ids.index(pickup.node_id)
            ]
            if distance is not None:
                ranked.append((distance, str(vehicle.vehicle_id), vehicle.vehicle_id))
        for _, _, vehicle_id in sorted(ranked):
            orders = {r.vehicle_id: r.stop_ids for r in candidate.routes}
            orders[vehicle_id] = orders.get(vehicle_id, ()) + (pickup.stop_id, discharge.stop_id)
            proposed = make_candidate(candidate, problem, orders)
            try:
                gate.assess(proposed)
            except RoutingValidationError:
                continue
            candidate = proposed
            break
    return candidate, measure(problem, candidate, policy, incumbent, gate)
