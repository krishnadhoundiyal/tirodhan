"""Actual pinned solvers versus independent exhaustive feasible allocations."""

import os
from dataclasses import asdict

import pytest
from fleet_verification.contracts import policy
from fleet_verification.oracle import enumerate_feasible, evaluate
from fleet_verification.strategies import fleet_problems
from fleet_verification.witness import single_sweep_witness
from hypothesis import event, given, settings

from tirodhan.modules.planning.routing.adapters import ORToolsOptimizer, VroomOptimizer
from tirodhan.modules.planning.routing.execution import RoutingAdapterError
from tirodhan.modules.routing_operations.economics import insertion_search, measure

pytestmark = [
    pytest.mark.routing_engine,
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("TIRODHAN_ROUTING_ENGINE_TESTS") != "1",
        reason="native generated verification is opt-in",
    ),
]


@pytest.mark.parametrize("engine", ["VROOM", "OR_TOOLS"])
@settings(max_examples=5 if os.getenv("TIRODHAN_HYPOTHESIS_PROFILE") != "fleet-extended" else 20)
@given(case=fleet_problems(max_lots=2))
async def test_native_proposal_and_refinement_against_exhaustive_oracle(engine, case):
    problem, weights, incumbent = case
    feasible = list(enumerate_feasible(problem, weights, incumbent))
    optimizer = VroomOptimizer() if engine == "VROOM" else ORToolsOptimizer()
    try:
        candidate = await optimizer.propose(problem, search_seconds=1)
    except RoutingAdapterError:
        assert not feasible, "Engine failed although the independent oracle found feasible work"
        event("independently infeasible")
        return
    native = evaluate(
        problem, {r.vehicle_id: r.stop_ids for r in candidate.routes}, weights, incumbent
    )
    assert asdict(measure(problem, candidate, policy(weights), incumbent)) == asdict(native)
    improved, cost, evaluations = insertion_search(problem, candidate, policy(weights), incumbent)
    independent = evaluate(
        problem, {r.vehicle_id: r.stop_ids for r in improved.routes}, weights, incumbent
    )
    assert asdict(cost) == asdict(independent)
    assert cost.rank <= native.rank
    optimum = min(feasible, key=lambda value: value[1].rank)[1]
    assert optimum.rank <= cost.rank
    event(f"coverage_gap={cost.unserved_lots - optimum.unserved_lots}")
    print(
        "GENERATED_NATIVE",
        {
            "engine": engine,
            "native": asdict(native),
            "refined": asdict(independent),
            "optimum": asdict(optimum),
            "evaluations": evaluations,
        },
    )


@pytest.mark.parametrize("engine", ["VROOM", "OR_TOOLS"])
async def test_real_engines_minimized_extra_vehicle_regression(engine):
    problem, weights, incumbent, _, _ = single_sweep_witness()
    optimizer = VroomOptimizer() if engine == "VROOM" else ORToolsOptimizer()
    native = await optimizer.propose(problem, search_seconds=1)
    evaluate(problem, {r.vehicle_id: r.stop_ids for r in native.routes}, weights, incumbent)
    improved, cost, _ = insertion_search(problem, native, policy(weights), incumbent)
    expected = evaluate(
        problem, {r.vehicle_id: r.stop_ids for r in improved.routes}, weights, incumbent
    )
    assert cost.generalized_seconds == expected.generalized_seconds == 5
    assert cost.additional_vehicles == 1 and cost.unserved_lots == 0


@pytest.mark.parametrize("engine", ["VROOM", "OR_TOOLS"])
async def test_real_engines_on_documented_local_search_limit(engine):
    problem, weights, incumbent, _, _ = single_sweep_witness("strict_local_minimum.json")
    optimizer = VroomOptimizer() if engine == "VROOM" else ORToolsOptimizer()
    native = await optimizer.propose(problem, search_seconds=1)
    improved, cost, _ = insertion_search(problem, native, policy(weights), incumbent)
    expected = evaluate(
        problem, {r.vehicle_id: r.stop_ids for r in improved.routes}, weights, incumbent
    )
    assert asdict(cost) == asdict(expected) and cost.unserved_lots == 0
    print("RESIDUAL_NATIVE", engine, cost.generalized_seconds)
