"""Quick generated checks that the QA ledger rejects real categories of history loss."""

from copy import deepcopy

import pytest
from geographical_evidence import Ledger
from hypothesis import given
from hypothesis import strategies as st


def view(identities):
    return dict(
        routes=[],
        task_history=[],
        cargo=[],
        assignments=[dict(booking_id=str(i), rider_id=str(i), revision=1) for i in identities],
        bookings=[dict(booking_id=str(i), status="ACCEPTED") for i in identities],
    )


@given(st.lists(st.uuids(), min_size=1, max_size=20, unique=True))
def test_cumulative_ledger_rejects_loss_and_duplicate_ownership(identities):
    original = view(identities)
    ledger = Ledger()
    ledger.check(view([]), original)
    # Order changes alone cannot break a commitment.
    reordered = deepcopy(original)
    reordered["assignments"].reverse()
    ledger.check(original, reordered)
    missing = deepcopy(reordered)
    missing["assignments"].pop()
    with pytest.raises(AssertionError, match="silently lost"):
        ledger.check(reordered, missing)
    duplicate = deepcopy(original)
    duplicate["assignments"].append(duplicate["assignments"][0])
    with pytest.raises(AssertionError, match="Duplicate active"):
        ledger.check(original, duplicate)


@given(st.uuids(), st.uuids())
def test_cumulative_ledger_rejects_cargo_history_rewrite(booking, vehicle):
    original = view([booking])
    original["cargo"] = [dict(request_id=str(booking), vehicle_id=str(vehicle))]
    ledger = Ledger()
    ledger.check(view([]), original)
    changed = deepcopy(original)
    changed["cargo"][0]["vehicle_id"] += "-changed"
    with pytest.raises(AssertionError, match="Cargo observation rewritten"):
        ledger.check(original, changed)
