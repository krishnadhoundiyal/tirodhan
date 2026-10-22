import pytest
from hypothesis import given, strategies as st, settings, HealthCheck
from uuid import UUID
from datetime import datetime, timezone
import math

from tirodhan.modules.planning.routing.contracts import (
    RoutingProblem,
    VehicleSnapshot,
    RoutingStop,
    TimeWindow,
    StopKind,
    CargoLot,
    RouteProposal,
    ProposedRoute,
    ResourceKind,
    RoutingLimits,
    ReceivingPointSnapshot
)
from tirodhan.modules.planning.routing.validation import validate_proposal, RoutingValidationError

def uid(i: int) -> UUID:
    return UUID(int=i)

# Strategies for property-based adversarial testing
time_windows = st.builds(
    TimeWindow,
    start=st.integers(min_value=0, max_value=86400),
    end=st.integers(min_value=0, max_value=86400)
).filter(lambda tw: tw.start <= tw.end)

capacities = st.tuples(
    st.integers(min_value=0, max_value=1000),
    st.integers(min_value=0, max_value=1000)
)

@st.composite
def adversarial_routing_problem(draw):
    num_vehicles = draw(st.integers(min_value=1, max_value=3))
    vehicles = tuple(
        VehicleSnapshot(
            vehicle_id=uid(i),
            rider_id=uid(100+i),
            state_version=1,
            resource_kind=ResourceKind.COMMITTED,
            operating_shift_id=None,
            matrix_id=uid(100),
            start_node_id=uid(200+i),
            end_node_id=uid(200+i),
            available_at=draw(st.integers(min_value=0, max_value=1000)),
            shift=draw(time_windows),
            capacity=draw(capacities),
            initial_cargo=(),
            protected_prefix=(),
            breaks=()
        )
        for i in range(1, num_vehicles + 1)
    )

    num_lots = draw(st.integers(min_value=1, max_value=5))
    lots = tuple(
        CargoLot(
            lot_id=uid(1000 + i),
            collection_request_id=uid(2000+i),
            quantity=draw(st.integers(min_value=1, max_value=50)),
            quantity_unit="kg",
            unit_load=draw(capacities),
            authorized_receiving_point_ids=(uid(5000),),
            discharge_deadline=draw(st.integers(min_value=0, max_value=100000)),
            pickup_stop_id=uid(3000 + i)
        )
        for i in range(1, num_lots + 1)
    )

    receiving_points = (
        ReceivingPointSnapshot(
            receiving_point_id=uid(5000),
            node_id=uid(6000),
            state_version=1,
            available=True,
            opening_windows=(TimeWindow(0, 100000),)
        ),
    )

    stops = []
    for lot in lots:
        stops.append(
            RoutingStop(
                stop_id=lot.pickup_stop_id,
                node_id=uid(7000),
                kind=StopKind.PICKUP,
                lot_id=lot.lot_id,
                quantity=lot.quantity,
                service_seconds=draw(st.integers(min_value=0, max_value=600)),
                service_start_window=draw(time_windows),
                required=draw(st.booleans()),
                eligible_vehicle_ids=tuple(v.vehicle_id for v in vehicles),
                bound_vehicle_id=None,
                receiving_point_id=None
            )
        )

        # Add corresponding discharge stop
        stops.append(
            RoutingStop(
                stop_id=uid(lot.pickup_stop_id.int + 10000),
                node_id=uid(6000),
                kind=StopKind.DISCHARGE,
                lot_id=lot.lot_id,
                quantity=lot.quantity,
                service_seconds=draw(st.integers(min_value=0, max_value=600)),
                service_start_window=draw(time_windows),
                required=True,
                eligible_vehicle_ids=tuple(v.vehicle_id for v in vehicles),
                bound_vehicle_id=None,
                receiving_point_id=uid(5000)
            )
        )

    return RoutingProblem(
        problem_id=uid(9999),
        snapshot_id=uid(8888),
        contract_version="v1",
        policy_version="v1",
        epoch=datetime.now(timezone.utc),
        load_dimensions=("kg", "L"),
        limits=RoutingLimits(100, 100, 10, 1),
        matrices=(), # We won't test solving here, only validation logic resilience
        vehicles=vehicles,
        lots=lots,
        stops=tuple(stops),
        receiving_points=receiving_points
    )


@given(problem=adversarial_routing_problem())
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_validation_resilience_to_adversarial_inputs(problem):
    # Just creating a dummy empty proposal
    proposal = RouteProposal(
        problem_id=problem.problem_id,
        snapshot_id=problem.snapshot_id,
        contract_version=problem.contract_version,
        policy_version=problem.policy_version,
        engine="VROOM",
        engine_version="1.0",
        routes=(),
        unserved_stop_ids=tuple(s.stop_id for s in problem.stops)
    )

    # The validation function should either pass (if proposal somehow valid, though unlikely as empty)
    # or raise RoutingValidationError, but NEVER crash with other exceptions like KeyError, ValueError, etc.
    try:
        validate_proposal(problem, proposal)
    except RoutingValidationError:
        pass
    except Exception as e:
        pytest.fail(f"Validation crashed with unexpected exception: {type(e)} {e}")
