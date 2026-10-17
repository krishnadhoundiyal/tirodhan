import pytest
from hypothesis import given, settings, strategies as st
from uuid import UUID

from tirodhan.modules.planning.routing.contracts import (
    VehicleSnapshot, ResourceKind, TimeWindow, CargoBalance, RoutingProblem, RoutingLimits, ROUTING_CONTRACT_V1,
    RoadMatrix, ReceivingPointSnapshot, RoutingStop, StopKind, CargoLot, SelectionEngine
)
from tirodhan.modules.planning.routing.validation import validate_problem, validate_proposal, RoutingValidationError
from tirodhan.modules.planning.routing.contracts import ProposedRoute, RouteProposal
from datetime import datetime, timezone

def uid(value: int) -> UUID:
    return UUID(int=value)

@settings(max_examples=20, deadline=None)
@given(
    quantity=st.integers(min_value=1, max_value=100),
    protected_pickup=st.booleans()
)
def test_vehicle_retains_custody_across_shift_boundaries(quantity, protected_pickup):
    cargo = CargoBalance(lot_id=uid(10), quantity=quantity)

    protected = ()
    if protected_pickup:
        protected = (uid(15),) # The discharge stop is protected, ensuring we execute it

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
        protected_prefix=protected
    )

    matrix = RoadMatrix(
        matrix_id=uid(4),
        graph_version="g",
        profile_version="p",
        node_ids=(uid(5), uid(6), uid(11)),
        travel_seconds=((0, 10, 10), (10, 0, 10), (10, 10, 0)),
        distance_m=((0, 10, 10), (10, 0, 10), (10, 10, 0))
    )

    receiving_point = ReceivingPointSnapshot(
        receiving_point_id=uid(14),
        node_id=uid(11),
        state_version=1,
        available=True,
        opening_windows=(TimeWindow(0, 86400),)
    )

    # Note that a protected prefix must point to a *required* and *vehicle-bound* stop
    stops = [
        RoutingStop(uid(15), uid(11), StopKind.DISCHARGE, uid(10), quantity, 10, TimeWindow(0, 86400), True, (uid(1),), uid(1) if protected_pickup else None, uid(14))
    ]

    lots = (
        CargoLot(uid(10), uid(13), quantity, "kg", (1,), (uid(14),), 86400, None),
    )

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
        stops=tuple(stops)
    )

    validate_problem(problem)

    if protected_pickup:
        # Protected means we MUST execute it
        routes = (ProposedRoute(v.vehicle_id, ()),)

        proposal = RouteProposal(
            problem_id=problem.problem_id,
            snapshot_id=problem.snapshot_id,
            contract_version=problem.contract_version,
            policy_version=problem.policy_version,
            engine=SelectionEngine.COMPOSITE,
            engine_version="1.0",
            routes=routes,
            unserved_stop_ids=()
        )

        with pytest.raises(RoutingValidationError, match="PROTECTED_PREFIX_CHANGED|CARGO_NOT_DISCHARGED"):
            validate_proposal(problem, proposal)

        routes_valid = (ProposedRoute(v.vehicle_id, (uid(15),)),)
        proposal_valid = RouteProposal(
            problem_id=problem.problem_id,
            snapshot_id=problem.snapshot_id,
            contract_version=problem.contract_version,
            policy_version=problem.policy_version,
            engine=SelectionEngine.COMPOSITE,
            engine_version="1.0",
            routes=routes_valid,
            unserved_stop_ids=()
        )
        validate_proposal(problem, proposal_valid)
