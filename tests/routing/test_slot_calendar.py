from datetime import datetime, timedelta, timezone
from uuid import UUID

from hypothesis import given
from hypothesis import strategies as st

from tirodhan.modules.collection_requests.scheduling import SlotWindow
from tirodhan.modules.routing_operations.slot_projection import calendar_supply


@given(
    st.lists(
        st.tuples(
            st.integers(-20, 200),
            st.integers(-20, 200),
            st.lists(st.tuples(st.integers(-20, 200), st.integers(-20, 200)), max_size=4),
        ),
        max_size=10,
    ),
    st.booleans(),
)
def test_calendar_sweep_matches_independent_per_second_occupancy(resources, gap):
    epoch = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def instant(t):
        return epoch + timedelta(seconds=t)

    ranges = [(0, 60), (120, 180)] if gap else [(0, 60), (60, 120), (120, 180)]
    slots = [SlotWindow(instant(a), instant(b)) for a, b in ranges]
    shifts, breaks = [], {}
    for i, (a, b, excluded) in enumerate(resources):
        a, b = sorted((a, b))
        identity = UUID(int=i + 1)
        shifts.append(dict(shift_id=identity, starts_at=instant(a), ends_at=instant(b)))
        breaks[identity] = [(instant(min(x, y)), instant(max(x, y))) for x, y in excluded]
    actual = calendar_supply(slots, shifts, breaks)
    expected = {
        instant(a): sum(
            sum(
                min(x, y) <= second < max(x, y)
                and not any(min(p, q) <= second < max(p, q) for p, q in excluded)
                for x, y, excluded in resources
            )
            for second in range(a, b)
        )
        for a, b in ranges
    }
    assert actual == expected
