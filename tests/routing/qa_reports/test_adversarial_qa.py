import pytest
from hypothesis import given, strategies as st
from hypothesis.stateful import RuleBasedStateMachine, rule, Bundle, initialize
from uuid import uuid4
from datetime import datetime

from tirodhan.modules.planning.routing.contracts import (
    RoutingProblem,
    VehicleSnapshot,
    CargoLot,
    RoutingStop,
    StopKind,
    TimeWindow,
    RoutingLimits,
    ResourceKind
)

def uid(x):
    from uuid import UUID
    return UUID(int=x)

class AdversarialRoutingStateMachine(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        self.vehicles = []
        self.lots = []
        self.stops = []

    @initialize(vehicle_id=st.integers(min_value=1, max_value=10),
                capacity_a=st.integers(min_value=1, max_value=100),
                capacity_b=st.integers(min_value=1, max_value=100))
    def init_vehicle(self, vehicle_id, capacity_a, capacity_b):
        v = VehicleSnapshot(
            vehicle_id=uid(vehicle_id),
            rider_id=uid(vehicle_id + 100),
            state_version=1,
            resource_kind=ResourceKind.COMMITTED,
            operating_shift_id=uid(vehicle_id + 200),
            matrix_id=uid(1),
            start_node_id=uid(100),
            end_node_id=uid(100),
            available_at=0,
            shift=TimeWindow(0, 86400),
            capacity=(capacity_a, capacity_b),
            initial_cargo=(),
            protected_prefix=(),
            breaks=()
        )
        self.vehicles.append(v)

    @rule(booking_idx=st.integers(min_value=200, max_value=300),
          pickup_amount_a=st.integers(min_value=1, max_value=5),
          pickup_amount_b=st.integers(min_value=1, max_value=5),
          service_time=st.integers(min_value=30, max_value=100),
          time_window_start=st.integers(min_value=0, max_value=1000),
          time_window_end=st.integers(min_value=1001, max_value=2000))
    def add_booking(self, booking_idx, pickup_amount_a, pickup_amount_b, service_time, time_window_start, time_window_end):
        lot_id = uid(booking_idx)
        pickup_loc = uid(booking_idx + 1000)
        discharge_loc = uid(booking_idx + 2000)

        lot = CargoLot(
            lot_id=lot_id,
            collection_request_id=uid(booking_idx + 3000),
            quantity=1,
            quantity_unit="pcs",
            unit_load=(pickup_amount_a, pickup_amount_b),
            authorized_receiving_point_ids=(uid(10000),),
            discharge_deadline=86400,
            pickup_stop_id=uid(booking_idx + 4000)
        )
        self.lots.append(lot)

        pickup_stop = RoutingStop(
            stop_id=uid(booking_idx + 4000),
            node_id=pickup_loc,
            kind=StopKind.PICKUP,
            lot_id=lot_id,
            quantity=1,
            service_seconds=service_time,
            service_start_window=TimeWindow(time_window_start, time_window_end),
            required=True,
            eligible_vehicle_ids=tuple(v.vehicle_id for v in self.vehicles),
            bound_vehicle_id=None,
            receiving_point_id=None
        )

        discharge_stop = RoutingStop(
            stop_id=uid(booking_idx + 5000),
            node_id=discharge_loc,
            kind=StopKind.DISCHARGE,
            lot_id=lot_id,
            quantity=1,
            service_seconds=service_time,
            service_start_window=TimeWindow(0, 86400),
            required=True,
            eligible_vehicle_ids=tuple(v.vehicle_id for v in self.vehicles),
            bound_vehicle_id=None,
            receiving_point_id=uid(10000)
        )

        self.stops.extend([pickup_stop, discharge_stop])

    @rule()
    def build_problem(self):
        if not self.vehicles or not self.lots:
            return

        problem = RoutingProblem(
            problem_id=uid(9999),
            snapshot_id=uid(8888),
            contract_version="v1",
            policy_version="v1",
            epoch=datetime.now(),
            load_dimensions=("a", "b"),
            limits=RoutingLimits(max_nodes=100, max_stops=100, max_vehicles=100, max_matrices=100),
            matrices=(),
            vehicles=tuple(self.vehicles),
            receiving_points=(),
            lots=tuple(self.lots),
            stops=tuple(self.stops)
        )
        assert len(problem.vehicles) == len(self.vehicles)
        assert len(problem.lots) == len(self.lots)
        assert len(problem.stops) == len(self.stops)

        # Verify capacities are logical and time windows are correct
        for v in problem.vehicles:
            assert v.capacity[0] > 0
            assert v.capacity[1] > 0

        for stop in problem.stops:
            assert stop.service_start_window.start <= stop.service_start_window.end
            if stop.kind == StopKind.PICKUP:
                assert stop.required == True

TestRoutingStateMachine = AdversarialRoutingStateMachine.TestCase
