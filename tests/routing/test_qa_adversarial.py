import os
from dataclasses import replace
from uuid import UUID

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tirodhan.modules.planning.routing.adapters import ORToolsOptimizer, VroomOptimizer
from tirodhan.modules.planning.routing.contracts import (
    CargoLot,
    RoutingStop,
    StopKind,
    TimeWindow,
)
from tirodhan.modules.planning.routing.execution import RoutingAdapterError
from tirodhan.modules.planning.routing.validation import validate_proposal

pytestmark = [
    pytest.mark.routing_engine,
    pytest.mark.skipif(
        os.getenv("TIRODHAN_ROUTING_ENGINE_TESTS") != "1",
        reason="explicit native solver opt-in",
    ),
]


def create_uuid(seed: int) -> UUID:
    return UUID(int=seed)


@pytest.mark.asyncio
@pytest.mark.parametrize("optimizer_cls", [VroomOptimizer, ORToolsOptimizer])
@settings(max_examples=20, deadline=None)
@given(
    v1_cap=st.integers(min_value=1, max_value=5),
    v2_cap=st.integers(min_value=1, max_value=5),
    lots_data=st.lists(
        st.tuples(
            st.integers(min_value=1, max_value=5),
            st.integers(min_value=0, max_value=1000),
            st.integers(min_value=10, max_value=1000),
            st.booleans(),
        ),
        min_size=2,
        max_size=10,
    ),
)
async def test_adversarial_capacity_and_windows_do_not_crash(
    routing_problem, optimizer_cls, v1_cap, v2_cap, lots_data
):
    v1 = replace(
        routing_problem.vehicles[0],
        vehicle_id=create_uuid(10),
        capacity=(v1_cap, v1_cap),
        initial_cargo=(),
        protected_prefix=(),
        rider_id=create_uuid(11),
        operating_shift_id=create_uuid(12),
    )
    v2 = replace(
        routing_problem.vehicles[0],
        vehicle_id=create_uuid(20),
        capacity=(v2_cap, v2_cap),
        initial_cargo=(),
        protected_prefix=(),
        rider_id=create_uuid(21),
        operating_shift_id=create_uuid(22),
    )

    lots = []
    stops = []
    matrix = routing_problem.matrices[0]

    nodes = matrix.node_ids

    facility = routing_problem.receiving_points[0]
    facility_node = facility.node_id

    for i, (quantity, start, length, req) in enumerate(lots_data):
        lot_id = create_uuid(10000 + i)
        pickup_id = create_uuid(20000 + i)
        drop_id = create_uuid(30000 + i)

        lots.append(
            CargoLot(
                lot_id=lot_id,
                collection_request_id=lot_id,
                quantity=quantity,
                quantity_unit="adversarial",
                unit_load=(quantity, quantity),
                authorized_receiving_point_ids=(facility.receiving_point_id,),
                discharge_deadline=900,
                pickup_stop_id=pickup_id,
            )
        )

        stops.append(
            RoutingStop(
                stop_id=pickup_id,
                node_id=nodes[i % len(nodes)],
                kind=StopKind.PICKUP,
                lot_id=lot_id,
                quantity=quantity,
                service_seconds=5,
                service_start_window=TimeWindow(start, start + length),
                required=req,
                eligible_vehicle_ids=(v1.vehicle_id, v2.vehicle_id),
                bound_vehicle_id=None,
                receiving_point_id=None,
            )
        )

        stops.append(
            RoutingStop(
                stop_id=drop_id,
                node_id=facility_node,
                kind=StopKind.DISCHARGE,
                lot_id=lot_id,
                quantity=quantity,
                service_seconds=5,
                service_start_window=TimeWindow(0, 5000),
                required=req,
                eligible_vehicle_ids=(v1.vehicle_id, v2.vehicle_id),
                bound_vehicle_id=None,
                receiving_point_id=facility.receiving_point_id,
            )
        )

    zero = tuple(tuple(0 for _ in row) for row in matrix.travel_seconds)
    problem = replace(
        routing_problem,
        vehicles=(v1, v2),
        lots=tuple(lots),
        stops=tuple(stops),
        matrices=(replace(matrix, travel_seconds=zero, distance_m=zero),),
        limits=replace(routing_problem.limits, max_stops=len(stops) * 2),
    )

    optimizer = optimizer_cls()

    try:
        proposal = await optimizer.propose(problem, search_seconds=2)
        assessment = validate_proposal(problem, proposal)
        assert assessment is not None
    except RoutingAdapterError as e:
        if e.code == "NO_FEASIBLE_PROPOSAL":
            pass
        else:
            raise
