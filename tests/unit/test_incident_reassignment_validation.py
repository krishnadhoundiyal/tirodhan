from uuid import UUID

import pytest

from tirodhan.modules.customers.service import command_fingerprint
from tirodhan.modules.operations.service import (
    INCIDENT_REASON_CODES,
    InvalidIncidentReasonError,
    _require_incident_reason,
)


def test_pickup_incident_reason_vocabulary_is_bounded() -> None:
    for reason_code in INCIDENT_REASON_CODES:
        _require_incident_reason(reason_code)
    with pytest.raises(InvalidIncidentReasonError):
        _require_incident_reason("RETRY_LATER")


def test_reassignment_fingerprint_is_deterministic_and_includes_optional_incident() -> None:
    facts = {
        "predecessor_assignment_id": UUID("00000000-0000-7000-8000-000000000001"),
        "replacement_rider_id": UUID("00000000-0000-7000-8000-000000000002"),
        "manager_user_id": UUID("00000000-0000-7000-8000-000000000003"),
        "incident_id": None,
    }
    assert command_fingerprint(facts) == command_fingerprint(dict(reversed(facts.items())))
    assert command_fingerprint(facts) != command_fingerprint(
        {
            **facts,
            "incident_id": UUID("00000000-0000-7000-8000-000000000004"),
        }
    )
