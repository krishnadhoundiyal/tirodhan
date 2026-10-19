"""Real native identity/precedence checks without per-lot load dimensions."""

import os
from dataclasses import replace
from uuid import UUID

import pytest

from tirodhan.modules.planning.routing.adapters import ORToolsOptimizer, VroomOptimizer
from tirodhan.modules.planning.routing.contracts import CargoLot, RoutingStop, StopKind, TimeWindow
from tirodhan.modules.planning.routing.validation import validate_proposal

pytestmark = [
    pytest.mark.routing_engine,
    pytest.mark.skipif(
        os.getenv("TIRODHAN_ROUTING_ENGINE_TESTS") != "1",
        reason="explicit native solver opt-in",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("optimizer", [VroomOptimizer, ORToolsOptimizer])
async def test_many_identical_load_lots_with_zero_time_still_preserve_identity(
    routing_problem,
    optimizer,
):
    lots, stops = list(routing_problem.lots), list(routing_problem.stops)
    for i in range(30):
        lot, pickup, discharge = (UUID(int=10000 + i * 3 + j) for j in range(3))
        lots.append(CargoLot(lot, lot, 1, "household", (1, 1), (UUID(int=61),), 900, pickup))
        stops.extend(
            (
                RoutingStop(
                    pickup,
                    UUID(int=2),
                    StopKind.PICKUP,
                    lot,
                    1,
                    0,
                    TimeWindow(0, 800),
                    True,
                    (UUID(int=30),),
                    None,
                    None,
                ),
                RoutingStop(
                    discharge,
                    UUID(int=3),
                    StopKind.DISCHARGE,
                    lot,
                    1,
                    0,
                    TimeWindow(0, 800),
                    True,
                    (UUID(int=30),),
                    None,
                    UUID(int=61),
                ),
            )
        )
    matrix = routing_problem.matrices[0]
    zero = tuple(tuple(0 for _ in row) for row in matrix.travel_seconds)
    problem = replace(
        routing_problem,
        lots=tuple(lots),
        stops=tuple(stops),
        matrices=(replace(matrix, travel_seconds=zero, distance_m=zero),),
        limits=replace(routing_problem.limits, max_stops=len(stops)),
    )
    proposal = await optimizer().propose(problem, search_seconds=2)
    assessment = validate_proposal(problem, proposal)
    assert not proposal.unserved_stop_ids
    order = proposal.routes[0].stop_ids
    assert order[:1] == routing_problem.vehicles[0].protected_prefix
    for lot in lots:
        if lot.pickup_stop_id:
            assert all(
                order.index(lot.pickup_stop_id) < order.index(s.stop_id)
                for s in stops
                if s.lot_id == lot.lot_id and s.kind == StopKind.DISCHARGE
            )
    assert assessment.routes[0].stops[-1].load_after == (0, 0)
