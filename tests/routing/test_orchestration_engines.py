"""Pinned-engine breaks and committed priority, checked by the independent gate."""

import os
from dataclasses import replace
from uuid import UUID

import pytest

from tirodhan.modules.planning.routing.adapters import ORToolsOptimizer, VroomOptimizer
from tirodhan.modules.planning.routing.contracts import ROUTING_CONTRACT_V2, TimeWindow
from tirodhan.modules.planning.routing.validation import validate_proposal

pytestmark = [
    pytest.mark.routing_engine,
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("TIRODHAN_ROUTING_ENGINE_TESTS") != "1",
        reason="pinned native environment required",
    ),
]


@pytest.mark.parametrize("engine", ["VROOM", "OR_TOOLS"])
async def test_break_with_cargo_preserves_window_and_physical_balance(routing_problem, engine):
    vehicle = replace(routing_problem.vehicles[0], breaks=(TimeWindow(80, 90),))
    problem = replace(routing_problem, contract_version=ROUTING_CONTRACT_V2, vehicles=(vehicle,))
    optimizer = VroomOptimizer() if engine == "VROOM" else ORToolsOptimizer()
    result = await optimizer.propose(problem, search_seconds=1)
    route = validate_proposal(problem, result).routes[0]
    pickup = next(s for s in route.stops if s.stop_id == UUID(int=51))
    assert 100 <= pickup.service_start_at <= 110
    assert route.stops[-1].load_after == (0, 0)
