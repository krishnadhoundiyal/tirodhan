import pytest
from hypothesis import given, settings, strategies as st
from uuid import UUID
from datetime import datetime, timezone
from tirodhan.modules.planning.routing.contracts import (
    RoutingProblem,
    TimeWindow,
    RoutingStop,
    StopKind,
    CargoLot,
    RoutingLimits
)
from tirodhan.modules.planning.routing.validation import RoutingValidationError, validate_proposal

def uid(i: int) -> UUID:
    return UUID(int=i)

# Hypothesis strategy for valid time windows
time_windows = st.builds(
    TimeWindow,
    st.integers(min_value=-100, max_value=86400),
    st.integers(min_value=-100, max_value=86400)
)

# More comprehensive adversarial test with hypothesis
@given(
    time_windows,
    st.integers(min_value=-1000, max_value=1000),
    st.integers(min_value=-10, max_value=10),
)
@settings(max_examples=100)
def test_routing_stop_validation_bounds(window, duration, load):
    stop = RoutingStop(
        stop_id=uid(1),
        node_id=uid(2),
        kind=StopKind.PICKUP,
        lot_id=uid(3),
        quantity=load,
        service_seconds=duration,
        service_start_window=window,
        required=True,
        eligible_vehicle_ids=(uid(4),),
        bound_vehicle_id=None,
        receiving_point_id=None
    )
    assert isinstance(stop.stop_id, UUID)

@given(st.lists(st.integers(min_value=0, max_value=100), max_size=5))
@settings(max_examples=100)
def test_routing_problem_with_lots(lots_quantities):
    lots = []
    stops = []
    for i, q in enumerate(lots_quantities):
        lots.append(CargoLot(
            lot_id=uid(100+i),
            collection_request_id=uid(200+i),
            quantity=q,
            quantity_unit="kg",
            unit_load=(q,),
            authorized_receiving_point_ids=(uid(4),),
            discharge_deadline=900,
            pickup_stop_id=uid(300+i*2)
        ))
        stops.append(RoutingStop(
            stop_id=uid(300+i*2),
            node_id=uid(1+i),
            kind=StopKind.PICKUP,
            lot_id=uid(100+i),
            quantity=q,
            service_seconds=30,
            service_start_window=TimeWindow(0, 3600),
            required=True,
            eligible_vehicle_ids=(uid(4),),
            bound_vehicle_id=None,
            receiving_point_id=None
        ))

    problem = RoutingProblem(
        problem_id=uid(1),
        snapshot_id=uid(2),
        contract_version="v2",
        policy_version="v1",
        epoch=datetime.now(timezone.utc),
        load_dimensions=("kg",),
        limits=RoutingLimits(max_nodes=100, max_stops=100, max_vehicles=10, max_matrices=2),
        matrices=tuple(),
        vehicles=tuple(),
        receiving_points=tuple(),
        lots=tuple(lots),
        stops=tuple(stops)
    )
    assert len(problem.lots) == len(lots_quantities)
    assert len(problem.stops) == len(lots_quantities)
