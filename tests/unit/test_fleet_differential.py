import os
from dataclasses import asdict, fields, is_dataclass, replace
from uuid import UUID

import pytest
from fleet_verification.contracts import policy, proposal
from fleet_verification.oracle import Infeasible, enumerate_feasible, evaluate
from fleet_verification.strategies import fleet_problems
from fleet_verification.witness import single_sweep_witness
from hypothesis import event, given, target
from hypothesis import strategies as st

from tirodhan.modules.planning.routing.validation import RoutingValidationError, validate_proposal
from tirodhan.modules.routing_operations.economics import insertion_search, measure


@given(
    fleet_problems(
        max_lots=3 if os.getenv("TIRODHAN_HYPOTHESIS_PROFILE") == "fleet-extended" else 2
    )
)
def test_exhaustive_reference_checks_accounting_feasibility_and_search(case):
    problem, weights, incumbent = case
    feasible = list(enumerate_feasible(problem, weights, incumbent))
    event(f"feasible={bool(feasible)}, loaded={bool(problem.vehicles[0].initial_cargo)}")
    if not feasible:
        with pytest.raises(RoutingValidationError):
            validate_proposal(problem, proposal(problem, {}))
        return
    optimum_orders, optimum = min(feasible, key=lambda candidate: candidate[1].rank)
    # Independently enumerated examples, including expensive/waiting/extra-vehicle alternatives.
    for orders, expected in (feasible[0], feasible[-1], (optimum_orders, optimum)):
        candidate = proposal(problem, orders)
        actual = measure(problem, candidate, policy(weights), incumbent)
        assert asdict(actual) == asdict(expected)
        validate_proposal(problem, candidate)
        if any(orders.values()):
            vehicle = next(v for v in orders if orders[v])
            invalid = dict(orders)
            invalid[vehicle] = orders[vehicle] + (orders[vehicle][0],)
            with pytest.raises(Infeasible):
                evaluate(problem, invalid, weights, incumbent)
            with pytest.raises(RoutingValidationError):
                validate_proposal(problem, proposal(problem, invalid))
    # A poor but independently feasible seed must improve monotonically. Global optimality
    # is deliberately NOT asserted for bounded heuristic search.
    seed_orders, seed_cost = max(feasible, key=lambda candidate: candidate[1].rank)
    improved, cost, _ = insertion_search(
        problem, proposal(problem, seed_orders), policy(weights), incumbent
    )
    independent = evaluate(
        problem, {r.vehicle_id: r.stop_ids for r in improved.routes}, weights, incumbent
    )
    assert asdict(cost) == asdict(independent)
    assert cost.rank <= seed_cost.rank and optimum.rank <= cost.rank
    gap = cost.generalized_seconds - optimum.generalized_seconds
    target(float(max(0, gap)), label="heuristic economic gap seconds")
    event(f"quality_gap={max(0, gap)}, coverage_gap={cost.unserved_lots - optimum.unserved_lots}")
    # Input collection order cannot change a fixed proposal's feasibility or cost.
    permuted = replace(
        problem, vehicles=problem.vehicles[::-1], stops=problem.stops[::-1], lots=problem.lots[::-1]
    )
    assert measure(permuted, improved, policy(weights), incumbent) == cost

    def rename(value):
        if isinstance(value, UUID):
            return UUID(int=value.int + 10000)
        if is_dataclass(value):
            return replace(value, **{f.name: rename(getattr(value, f.name)) for f in fields(value)})
        if isinstance(value, tuple):
            return tuple(rename(v) for v in value)
        return value

    renamed_incumbent = {rename(v): rename(order) for v, order in incumbent.items()}
    assert measure(rename(problem), rename(improved), policy(weights), renamed_incumbent) == cost


def test_minimized_single_sweep_deploys_an_unnecessary_vehicle():
    problem, weights, incumbent, seed, historical = single_sweep_witness()
    assert historical["actual"]["generalized_seconds"] == 6
    best = min(enumerate_feasible(problem, weights, incumbent), key=lambda c: c[1].rank)[1]
    improved, actual, count = insertion_search(
        problem, proposal(problem, seed), policy(weights), incumbent
    )
    independent = evaluate(
        problem, {r.vehicle_id: r.stop_ids for r in improved.routes}, weights, incumbent
    )
    assert asdict(actual) == asdict(independent) == asdict(best)
    assert actual.additional_vehicles == 1 and actual.generalized_seconds == 5
    assert count <= policy(weights).max_candidate_evaluations


def test_empty_list_and_tuple_orders_have_the_same_reference_meaning():
    problem, weights, incumbent, _, _ = single_sweep_witness()
    lists = {v.vehicle_id: [] for v in problem.vehicles}
    tuples = {v.vehicle_id: () for v in problem.vehicles}
    assert evaluate(problem, lists, weights, incumbent) == evaluate(
        problem, tuples, weights, incumbent
    )
    validate_proposal(problem, proposal(problem, lists))


def test_minimized_strict_local_search_gap_is_explicit_and_bounded():
    problem, weights, incumbent, seed, data = single_sweep_witness("strict_local_minimum.json")
    improved, actual, count = insertion_search(
        problem, proposal(problem, seed), policy(weights), incumbent
    )
    independent = evaluate(
        problem, {r.vehicle_id: r.stop_ids for r in improved.routes}, weights, incumbent
    )
    optimal = min(enumerate_feasible(problem, weights, incumbent), key=lambda c: c[1].rank)[1]
    assert asdict(actual) == asdict(independent)
    assert optimal.generalized_seconds == data["optimum"]["generalized_seconds"] == 5
    assert optimal.rank <= actual.rank
    assert actual.generalized_seconds <= data["actual"]["generalized_seconds"] == 6
    assert actual.unserved_lots == 0 and count <= policy(weights).max_candidate_evaluations
    orders = {r.vehicle_id: r.stop_ids for r in improved.routes}
    # Independently enumerate every single whole-lot relocate around the final route.
    # This witness has one vehicle: no transfer/block move to a second vehicle exists.
    assert len(problem.vehicles) == 1
    vehicle = problem.vehicles[0]
    for lot in problem.lots:
        pair = tuple(s for s in problem.stops if s.lot_id == lot.lot_id)
        pickup = next(s.stop_id for s in pair if s.kind == "PICKUP")
        discharge = next(s.stop_id for s in pair if s.kind == "DISCHARGE")
        rest = tuple(s for s in orders[vehicle.vehicle_id] if s not in (pickup, discharge))
        for i in range(len(rest) + 1):
            placed = rest[:i] + (pickup,) + rest[i:]
            for j in range(i + 1, len(placed) + 1):
                moved = {vehicle.vehicle_id: placed[:j] + (discharge,) + placed[j:]}
                try:
                    neighbor = evaluate(problem, moved, weights, incumbent)
                except Infeasible:
                    continue
                assert neighbor.rank >= actual.rank
    # The bounded current neighborhood may retain the documented local minimum.
    # Improvements are welcome; accepting unsafe work or worsening beyond this baseline is not.


@given(case=fleet_problems(max_lots=2), data=st.data())
def test_arbitrary_allocations_match_independent_hard_constraints(case, data):
    problem, weights, incumbent = case
    vehicles = tuple(v.vehicle_id for v in problem.vehicles)
    assigned = data.draw(
        st.lists(
            st.integers(-1, len(vehicles) - 1),
            min_size=len(problem.stops),
            max_size=len(problem.stops),
        )
    )
    orders = {}
    for index, vehicle in enumerate(vehicles):
        members = tuple(
            s.stop_id for s, owner in zip(problem.stops, assigned, strict=True) if owner == index
        )
        orders[vehicle] = data.draw(st.permutations(members))
    try:
        expected = evaluate(problem, orders, weights, incumbent)
    except Infeasible:
        expected = None
    try:
        validate_proposal(problem, proposal(problem, orders))
        accepted = True
    except RoutingValidationError:
        accepted = False
    assert accepted == (expected is not None)
