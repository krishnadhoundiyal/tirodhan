from uuid import UUID

import pytest

from tirodhan.modules.customers.service import GeoPoint, command_fingerprint
from tirodhan.modules.handovers.service import (
    InvalidHandoverCommandError,
    _validate_command_inputs,
)


def test_handover_command_requires_unique_nonempty_pickups_and_location() -> None:
    pickup_id = UUID("00000000-0000-7000-8000-000000000001")
    _validate_command_inputs(
        pickup_execution_ids=[pickup_id],
        observed_location=GeoPoint(latitude=28.6139, longitude=77.2090),
    )
    with pytest.raises(InvalidHandoverCommandError):
        _validate_command_inputs(pickup_execution_ids=[], observed_location=GeoPoint(1, 1))
    with pytest.raises(InvalidHandoverCommandError):
        _validate_command_inputs(
            pickup_execution_ids=[pickup_id, pickup_id],
            observed_location=GeoPoint(1, 1),
        )
    with pytest.raises(InvalidHandoverCommandError):
        _validate_command_inputs(pickup_execution_ids=[pickup_id], observed_location=None)


def test_handover_fingerprint_facts_are_order_independent_but_location_sensitive() -> None:
    first = UUID("00000000-0000-7000-8000-000000000001")
    second = UUID("00000000-0000-7000-8000-000000000002")
    rider = UUID("00000000-0000-7000-8000-000000000003")
    point = UUID("00000000-0000-7000-8000-000000000004")

    def fingerprint(pickups: list[UUID], latitude: float) -> bytes:
        canonical = sorted(pickups, key=lambda value: value.int)
        return command_fingerprint(
            {
                "rider_id": rider,
                "receiving_point_id": point,
                "pickup_execution_ids": [str(value) for value in canonical],
                "observed_location": {"latitude": latitude, "longitude": 77.2090},
            }
        )

    assert fingerprint([first, second], 28.6139) == fingerprint([second, first], 28.6139)
    assert fingerprint([first, second], 28.6139) != fingerprint([first, second], 28.6140)
