from datetime import timedelta

import pytest

from tirodhan.db.values import utc_now
from tirodhan.modules.dispatch.service import _require_intent, _require_offer_window


def test_dispatch_input_vocabulary_is_bounded() -> None:
    _require_intent("AVAILABLE")
    _require_intent("OFFLINE")
    with pytest.raises(ValueError):
        _require_intent("BUSY")


def test_offer_window_requires_positive_round_and_future_expiry() -> None:
    now = utc_now()
    _require_offer_window(1, now + timedelta(seconds=1), now)
    with pytest.raises(ValueError):
        _require_offer_window(0, now + timedelta(seconds=1), now)
    with pytest.raises(ValueError):
        _require_offer_window(1, now, now)
