import os
from datetime import datetime, timezone
from uuid import UUID

import pytest

from tirodhan.modules.planning.routing.adapters import ORToolsOptimizer
from tirodhan.modules.planning.routing.contracts import (
    ROUTING_CONTRACT_V1,
    CargoBalance,
    CargoLot,
    ReceivingPointSnapshot,
    ResourceKind,
    RoadMatrix,
    RoutingLimits,
    RoutingProblem,
    RoutingStop,
    StopKind,
    TimeWindow,
    VehicleSnapshot,
)


def uid(value: int) -> UUID:
    return UUID(int=value)


@pytest.mark.asyncio
async def test_full_routing_pipeline_integration():
    if os.getenv("TIRODHAN_ROUTING_ENGINE_TESTS") != "1":
        pytest.skip("Explicit real-engine opt-in required for E2E integration test")

    cargo = CargoBalance(lot_id=uid(10), quantity=1)

    v = VehicleSnapshot(
        vehicle_id=uid(1),
        rider_id=uid(2),
        state_version=1,
        resource_kind=ResourceKind.COMMITTED,
        operating_shift_id=uid(3),
        matrix_id=uid(4),
        start_node_id=uid(5),
        end_node_id=uid(6),
        available_at=0,
        shift=TimeWindow(0, 86400),
        capacity=(100,),
        initial_cargo=(cargo,),
        protected_prefix=(),
    )

    # Needs valid coords to work against osrm in the container
    matrix = RoadMatrix(
        matrix_id=uid(4),
        graph_version="g",
        profile_version="p",
        node_ids=(uid(5), uid(6), uid(11)),
        travel_seconds=((0, 10, 10), (10, 0, 10), (10, 10, 0)),
        distance_m=((0, 10, 10), (10, 0, 10), (10, 10, 0)),
    )

    receiving_point = ReceivingPointSnapshot(
        receiving_point_id=uid(14),
        node_id=uid(11),
        state_version=1,
        available=True,
        opening_windows=(TimeWindow(0, 86400),),
    )

    stops = [
        RoutingStop(
            uid(15),
            uid(11),
            StopKind.DISCHARGE,
            uid(10),
            1,
            10,
            TimeWindow(0, 86400),
            True,
            (uid(1),),
            None,
            uid(14),
        )
    ]

    lots = (CargoLot(uid(10), uid(13), 1, "kg", (1,), (uid(14),), 86400, None),)

    problem = RoutingProblem(
        problem_id=uid(100),
        snapshot_id=uid(101),
        contract_version=ROUTING_CONTRACT_V1,
        policy_version="test",
        epoch=datetime.now(timezone.utc),
        load_dimensions=("kg",),
        limits=RoutingLimits(100, 100, 100, 100),
        matrices=(matrix,),
        vehicles=(v,),
        receiving_points=(receiving_point,),
        lots=lots,
        stops=tuple(stops),
    )

    # Validate OR Tools optimizer adapter integration
    solver = ORToolsOptimizer()

    # E2E - Native Engine call
    try:
        proposal = await solver.propose(problem, search_seconds=2)
        assert len(proposal.routes) > 0
        assert proposal.routes[0].vehicle_id == uid(1)
        assert uid(15) in proposal.routes[0].stop_ids
    except Exception as e:
        # Just fail natively
        pytest.fail(f"OR Tools failed: {e}")
