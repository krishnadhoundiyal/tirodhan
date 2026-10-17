"""Independent regional selection oracle and real contract/gate regressions."""

import itertools
from dataclasses import replace
from uuid import UUID

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tirodhan.modules.planning.routing.contracts import ProposedRoute
from tirodhan.modules.planning.routing.osrm import RoadNode
from tirodhan.modules.planning.routing.validation import RoutingValidationError
from tirodhan.modules.routing_operations.economics import EconomicPolicy
from tirodhan.modules.routing_operations.parallel import (
    VERSION,
    Candidate,
    FacilityLimit,
    candidate,
    conflict_alternatives,
    consolidate,
    selected_routes,
)
from tirodhan.modules.routing_operations.regional_matrix import RegionalMatrixCache

pytest.importorskip("ortools", reason="regional selection requires the optional routing extra")


def uid(i):
    return UUID(int=i)


def route(i, vehicle, lots=(), cost=0):
    return Candidate(
        candidate_id=uid(i),
        vehicle_id=uid(vehicle),
        rider_id=uid(100 + vehicle),
        stop_ids=tuple(uid(1000 + x) for x in lots),
        lot_ids=tuple(map(uid, lots)),
        pickup_ids=tuple(map(uid, lots)),
        construction="TEST",
        engine_version=VERSION,
        cost=cost,
    )


def test_consolidation_solves_non_greedy_cost_choice():
    pool = [
        route(1, 10, [30], 8),
        route(2, 20, [30], 10),
        route(3, 10, [40], 9),
        route(4, 20, [40], 100),
        route(5, 10),
        route(6, 20),
    ]
    result = consolidate(
        pool, {uid(10), uid(20)}, set(), {uid(30): 0, uid(40): 0}, policy_version="test", seconds=2
    )
    assert set(result.selected_ids) == {uid(2), uid(3)}
    assert result.stage_values[-2:] == (2, -19)
    assert result.stage_statuses == ("OPTIMAL",) * 4


def test_hybrid_urgent_reattempt_precedes_general_coverage_and_mandatory_is_hard():
    pool = [route(1, 10, [30]), route(2, 10, [40, 41]), route(3, 10)]
    selected = consolidate(
        pool,
        {uid(10)},
        set(),
        {uid(30): 2, uid(40): 0, uid(41): 0},
        policy_version="hybrid",
        seconds=2,
    )
    assert selected.selected_ids == (uid(1),)
    required = consolidate(
        pool,
        {uid(10)},
        {uid(40)},
        {uid(30): 2, uid(40): 0, uid(41): 0},
        policy_version="hybrid",
        seconds=2,
    )
    assert required.selected_ids == (uid(2),)


def test_fixed_facility_limits_and_pool_infeasibility_do_not_retime_routes():
    a = route(1, 10, [30]).model_copy(update={"facility_visits": ((uid(90), 10, 20),)})
    b = route(2, 20, [40]).model_copy(update={"facility_visits": ((uid(90), 15, 25),)})
    result = consolidate(
        [a, b, route(3, 10), route(4, 20)],
        {uid(10), uid(20)},
        set(),
        {uid(30): 0, uid(40): 0},
        policy_version="test",
        seconds=2,
        facility_limits=(FacilityLimit(receiving_point_id=uid(90), concurrent_unloads=1),),
    )
    assert result.stage_values[-2] == 1
    impossible = consolidate(
        [a, b],
        {uid(10), uid(20)},
        {uid(30), uid(40)},
        {},
        policy_version="test",
        seconds=2,
        facility_limits=(FacilityLimit(receiving_point_id=uid(90), concurrent_unloads=1),),
    )
    with pytest.raises(ValueError, match="NO_FEASIBLE_SELECTION"):
        selected_routes(impossible)


@settings(max_examples=20, deadline=None)
@given(st.lists(st.integers(0, 20), min_size=6, max_size=6))
def test_small_candidate_pools_match_exhaustive_oracle(costs):
    pool = [
        route(1, 10, [30], costs[0]),
        route(2, 10, [40], costs[1]),
        route(3, 10, [], costs[2]),
        route(4, 20, [30], costs[3]),
        route(5, 20, [40], costs[4]),
        route(6, 20, [], costs[5]),
    ]
    feasible = [
        (a, b)
        for a, b in itertools.product(pool[:3], pool[3:])
        if not set(a.lot_ids) & set(b.lot_ids)
    ]
    optimum = min((-len(set(a.lot_ids + b.lot_ids)), a.cost + b.cost) for a, b in feasible)
    result = consolidate(
        pool, {uid(10), uid(20)}, set(), {uid(30): 0, uid(40): 0}, policy_version="test", seconds=2
    )
    selected = [c for c in pool if c.candidate_id in result.selected_ids]
    assert (
        -len({lot for c in selected for lot in c.lot_ids}),
        sum(c.cost for c in selected),
    ) == optimum


def test_complete_candidate_cannot_forget_existing_cargo_or_protected_work(routing_problem):
    economics = EconomicPolicy(
        version="test",
        additional_vehicle_seconds=0,
        disruption_seconds=0,
        waiting_weight=0,
        window_risk_buffer_seconds=0,
        window_risk_weight=0,
        max_candidate_evaluations=10,
    )
    valid = candidate(
        routing_problem,
        ProposedRoute(uid(30), (uid(50), uid(51), uid(52), uid(53))),
        "INCUMBENT",
        VERSION,
        economics,
        {},
    )
    assert valid.lot_ids == (uid(40), uid(42))
    with pytest.raises(RoutingValidationError):
        candidate(
            routing_problem,
            ProposedRoute(uid(30), (uid(51), uid(52), uid(53))),
            "REMOVED",
            VERSION,
            economics,
            {},
        )


@pytest.mark.asyncio
async def test_directional_cache_reuses_submatrices_without_merging_households(routing_problem):
    class Source:
        calls = 0

        async def acquire(self, nodes):
            self.calls += 1
            values = tuple(
                tuple(
                    0 if a.longitude == b.longitude else int(a.longitude * 10 + b.longitude)
                    for b in nodes
                )
                for a in nodes
            )
            return replace(
                routing_problem.matrices[0],
                graph_version="g",
                profile_version="p",
                node_ids=tuple(n.node_id for n in nodes),
                travel_seconds=values,
                distance_m=values,
            )

    source = Source()
    cache = RegionalMatrixCache(source, profile="p", graph="g", block_nodes=4, max_pairs=16)
    nodes = (RoadNode(uid(1), 1, 1), RoadNode(uid(2), 2, 2), RoadNode(uid(3), 1, 1))
    matrix = await cache.acquire(nodes)
    count = source.calls
    sub = await cache.acquire((nodes[1], nodes[0]))
    assert source.calls == count
    assert matrix.node_ids == (uid(1), uid(2), uid(3))
    assert sub.travel_seconds == ((0, 21), (12, 0))
    assert matrix.travel_seconds[0] == matrix.travel_seconds[2]


def test_one_contested_household_does_not_discard_other_serviceable_work(routing_problem):
    from tirodhan.modules.planning.routing.contracts import (
        CargoLot,
        RoutingStop,
        StopKind,
        TimeWindow,
    )

    economics = EconomicPolicy(
        version="test",
        additional_vehicle_seconds=0,
        disruption_seconds=0,
        waiting_weight=0,
        window_risk_buffer_seconds=0,
        window_risk_weight=0,
        max_candidate_evaluations=10,
    )
    v = replace(
        routing_problem.vehicles[0], initial_cargo=(), protected_prefix=(), capacity=(100, 100)
    )
    w = replace(v, vehicle_id=uid(70), rider_id=uid(71), operating_shift_id=uid(72))
    lots = []
    stops = []
    for i in range(5):
        lots.append(
            CargoLot(
                uid(100 + i), uid(200 + i), 1, "lot", (1, 1), (uid(61),), 900, uid(300 + i * 2)
            )
        )
        stops.extend(
            (
                RoutingStop(
                    uid(300 + i * 2),
                    uid(2),
                    StopKind.PICKUP,
                    uid(100 + i),
                    1,
                    1,
                    TimeWindow(0, 800),
                    False,
                    (v.vehicle_id, w.vehicle_id),
                    None,
                    None,
                ),
                RoutingStop(
                    uid(301 + i * 2),
                    uid(3),
                    StopKind.DISCHARGE,
                    uid(100 + i),
                    1,
                    1,
                    TimeWindow(0, 900),
                    False,
                    (v.vehicle_id, w.vehicle_id),
                    None,
                    uid(61),
                ),
            )
        )
    problem = replace(routing_problem, vehicles=(v, w), lots=tuple(lots), stops=tuple(stops))
    a = ProposedRoute(v.vehicle_id, tuple(uid(300 + i * 2 + j) for i in (0, 1, 2) for j in (0, 1)))
    b = ProposedRoute(w.vehicle_id, tuple(uid(300 + i * 2 + j) for i in (2, 3, 4) for j in (0, 1)))
    pool = [
        candidate(problem, r, "VROOM", "1.15.0", economics, {})
        for r in (a, b, ProposedRoute(v.vehicle_id, ()), ProposedRoute(w.vehicle_id, ()))
    ]
    priorities = {lot.lot_id: 0 for lot in lots}
    before = consolidate(
        pool, {v.vehicle_id, w.vehicle_id}, set(), priorities, policy_version="test", seconds=2
    )
    adjusted = conflict_alternatives(problem, pool, economics, {}, 10)
    after = consolidate(
        adjusted, {v.vehicle_id, w.vehicle_id}, set(), priorities, policy_version="test", seconds=2
    )
    assert before.stage_values[-2] == 3
    assert after.stage_values[-2] == 5
    assert all(not ({uid(304), uid(305)} & set(c.stop_ids)) for c in adjusted if c.parent_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["vroom", "ortools"])
async def test_real_native_collect_unload_and_resume(routing_problem, engine):
    import os

    if os.getenv("TIRODHAN_ROUTING_ENGINE_TESTS") != "1":
        pytest.skip("explicit real-engine opt-in")
    from tirodhan.modules.planning.routing.adapters import ORToolsOptimizer, VroomOptimizer
    from tirodhan.modules.planning.routing.contracts import (
        CargoLot,
        RoutingStop,
        StopKind,
        TimeWindow,
    )
    from tirodhan.modules.planning.routing.validation import validate_proposal

    v = replace(routing_problem.vehicles[0], initial_cargo=(), protected_prefix=(), capacity=(1, 1))
    lots = []
    stops = []
    for i in range(2):
        lots.append(
            CargoLot(
                uid(100 + i), uid(200 + i), 1, "lot", (1, 1), (uid(61),), 900, uid(300 + i * 2)
            )
        )
        stops.extend(
            (
                RoutingStop(
                    uid(300 + i * 2),
                    uid(1 + i),
                    StopKind.PICKUP,
                    uid(100 + i),
                    1,
                    30,
                    TimeWindow(i * 200, i * 200 + 100),
                    True,
                    (v.vehicle_id,),
                    None,
                    None,
                ),
                RoutingStop(
                    uid(301 + i * 2),
                    uid(3),
                    StopKind.DISCHARGE,
                    uid(100 + i),
                    1,
                    20,
                    TimeWindow(0, 900),
                    True,
                    (v.vehicle_id,),
                    None,
                    uid(61),
                ),
            )
        )
    problem = replace(routing_problem, vehicles=(v,), lots=tuple(lots), stops=tuple(stops))
    solver = VroomOptimizer() if engine == "vroom" else ORToolsOptimizer()
    proposal = await solver.propose(problem, search_seconds=2)
    assessment = validate_proposal(problem, proposal)
    assert proposal.routes[0].stop_ids == (uid(300), uid(301), uid(302), uid(303))
    assert [stop.load_after for stop in assessment.routes[0].stops] == [
        (1, 1),
        (0, 0),
        (1, 1),
        (0, 0),
    ]
