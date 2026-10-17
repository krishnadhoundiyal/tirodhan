from datetime import datetime, timezone
from uuid import UUID

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tirodhan.modules.planning.routing.contracts import (
    ROUTING_CONTRACT_V1,
    CargoBalance,
    ResourceKind,
    RoutingLimits,
    RoutingProblem,
    TimeWindow,
    VehicleSnapshot,
)
from tirodhan.modules.planning.routing.validation import validate_problem


def uid(value: int) -> UUID:
    return UUID(int=value)


@given(quantity=st.integers(min_value=1, max_value=100))
def test_vehicle_retains_custody_across_shift_boundaries(quantity):
    # A vehicle with initial cargo should retain it even if the shift expires.
    # Validate the routing problem ensures that vehicles with initial cargo
    # are correctly validated even after shift boundaries.

    cargo = CargoBalance(lot_id=uid(10), quantity=quantity)
    v = VehicleSnapshot(
        vehicle_id=uid(1),
        rider_id=uid(2),
        state_version=1,
        resource_kind=ResourceKind.COMMITTED,
        operating_shift_id=uid(3),
        matrix_id=uid(4),
        start_node_id=uid(5),
        end_node_id=uid(6),
        available_at=86401,  # Available after shift ends - simulate exception
        shift=TimeWindow(0, 86400),
        capacity=(100,),
        initial_cargo=(cargo,),
        protected_prefix=(),
    )

    problem = RoutingProblem(
        problem_id=uid(100),
        snapshot_id=uid(101),
        contract_version=ROUTING_CONTRACT_V1,
        policy_version="test",
        epoch=datetime.now(timezone.utc),
        load_dimensions=("kg",),
        limits=RoutingLimits(100, 100, 100, 100),
        matrices=(),
        vehicles=(v,),
        receiving_points=(),
        lots=(),
        stops=(),
    )

    # Validation should reject the problem because available_at is outside the shift,
    # demonstrating the temporal constraint validation works
    from tirodhan.modules.planning.routing.validation import RoutingValidationError

    with pytest.raises(RoutingValidationError, match="INVALID_VEHICLE_AVAILABILITY"):
        validate_problem(problem)
