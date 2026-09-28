import pytest

from tirodhan.modules.pickups.service import InvalidPickupOutcomeError, _require_attempt_outcome


def test_pickup_attempt_outcome_vocabulary_is_bounded() -> None:
    _require_attempt_outcome("COLLECTED")
    _require_attempt_outcome("NOT_COLLECTED")
    with pytest.raises(InvalidPickupOutcomeError):
        _require_attempt_outcome("CUSTOMER_UNAVAILABLE")
