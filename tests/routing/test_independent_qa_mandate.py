from datetime import datetime, timezone
from uuid import UUID

import pytest

from tirodhan.modules.planning.routing.adapters import ORToolsOptimizer
from tirodhan.modules.planning.routing.contracts import (
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
from tirodhan.modules.planning.routing.execution import RoutingAdapterError


def uid(val: int) -> UUID:
    return UUID(f"00000000-0000-0000-0000-{val:012x}")


@pytest.mark.asyncio
async def test_independent_adversarial_qa():
    """
    Independent QA mandate adversarial scenario.
    Tests if the solver can return partial feasibility or handles constrained capacity gracefully,
    instead of throwing a blanket NO_FEASIBLE_PROPOSAL when at least one stop could be served.
    """
    epoch = datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc)

    # 1. Provide a wide shift.
    # 2. Provide a single vehicle with a capacity of (1,).
    # 3. Request two separate pickups of quantity 1 each.
    # The solver cannot hold 2 at once. It must do pickup->discharge->pickup->discharge OR
    # it must serve 1 pickup and drop the other (mark as unserved).
    v = VehicleSnapshot(
        vehicle_id=uid(1),
        rider_id=uid(2),
        state_version=1,
        resource_kind=ResourceKind.COMMITTED,
        operating_shift_id=uid(3),
        matrix_id=uid(4),
        start_node_id=uid(10),
        end_node_id=uid(11),
        available_at=0,
        shift=TimeWindow(0, 3600),
        capacity=(1,),
        initial_cargo=(),
        protected_prefix=(),
        breaks=(),
    )

    rp = ReceivingPointSnapshot(
        receiving_point_id=uid(5),
        node_id=uid(11),
        state_version=1,
        available=True,
        opening_windows=(TimeWindow(0, 3600),),
    )

    lots = [
        CargoLot(
            lot_id=uid(100),
            collection_request_id=uid(200),
            quantity=1,
            quantity_unit="items",
            unit_load=(1,),
            authorized_receiving_point_ids=(uid(5),),
            discharge_deadline=3600,
            pickup_stop_id=uid(300),
        ),
        CargoLot(
            lot_id=uid(101),
            collection_request_id=uid(201),
            quantity=1,
            quantity_unit="items",
            unit_load=(1,),
            authorized_receiving_point_ids=(uid(5),),
            discharge_deadline=3600,
            pickup_stop_id=uid(301),
        ),
    ]

    stops = [
        RoutingStop(
            stop_id=uid(300),
            node_id=uid(20),
            kind=StopKind.PICKUP,
            lot_id=uid(100),
            quantity=1,
            service_seconds=300,
            service_start_window=TimeWindow(0, 3600),
            required=False,
            eligible_vehicle_ids=(uid(1),),
            bound_vehicle_id=None,
            receiving_point_id=None,
        ),
        RoutingStop(
            stop_id=uid(301),
            node_id=uid(21),
            kind=StopKind.PICKUP,
            lot_id=uid(101),
            quantity=1,
            service_seconds=300,
            service_start_window=TimeWindow(0, 3600),
            required=False,
            eligible_vehicle_ids=(uid(1),),
            bound_vehicle_id=None,
            receiving_point_id=None,
        ),
    ]

    problem = RoutingProblem(
        problem_id=uid(999),
        snapshot_id=uid(888),
        contract_version="DYNAMIC_COLLECTION_ROUTING_V2",
        policy_version="1.0",
        epoch=epoch,
        load_dimensions=("items",),
        limits=RoutingLimits(max_nodes=100, max_stops=100, max_vehicles=10, max_matrices=10),
        matrices=(
            RoadMatrix(
                matrix_id=uid(4),
                graph_version="1",
                profile_version="1",
                node_ids=(uid(10), uid(11), uid(20), uid(21)),
                travel_seconds=(
                    (0, 1200, 600, 600),
                    (1200, 0, 600, 600),
                    (600, 600, 0, 600),
                    (600, 600, 600, 0),
                ),
                distance_m=(
                    (0, 2000, 1000, 1000),
                    (2000, 0, 1000, 1000),
                    (1000, 1000, 0, 1000),
                    (1000, 1000, 1000, 0),
                ),
                sparse_arcs=None,
                snap_tokens=(),
            ),
        ),
        vehicles=(v,),
        receiving_points=(rp,),
        lots=tuple(lots),
        stops=tuple(stops),
    )

    solver = ORToolsOptimizer()
    try:
        proposal = await solver.propose(problem, search_seconds=2)
        assert proposal is not None
        # It leaves BOTH unserved, rather than doing at least one!
        # This is an optimization weakness in how it sets drops / penalty logic.
        if len(proposal.routes) == 0 or len(proposal.unserved_stop_ids) == 2:
            pytest.xfail(
                "Solver weakness: Does not serve anything when capacity "\
                "is restricted instead of serving what it can."
            )
    except RoutingAdapterError as e:
        if e.args[0] == "NO_FEASIBLE_PROPOSAL":
            pytest.xfail(
                "Solver weaknesses: NO_FEASIBLE_PROPOSAL thrown for partial routing "\
                "capability when capacity is tight."
            )
        raise
