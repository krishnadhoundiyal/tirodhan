from hypothesis import given, settings
from hypothesis import strategies as st

from tirodhan.modules.planning.routing.contracts import TimeWindow
from tirodhan.modules.planning.routing.validation import avoid_breaks


@settings(max_examples=100)
@given(
    start=st.integers(min_value=0, max_value=86400),
    duration=st.integers(min_value=1, max_value=3600),
    break_start=st.integers(min_value=0, max_value=86400),
    break_duration=st.integers(min_value=1, max_value=3600),
)
def test_avoid_breaks_never_overlaps_break_window(start, duration, break_start, break_duration):
    """Property: A task scheduled around a break must not overlap the break window."""
    break_end = break_start + break_duration
    breaks = (TimeWindow(break_start, break_end),)

    actual_start = avoid_breaks(start, duration, breaks)
    actual_end = actual_start + duration

    overlaps = (actual_start < break_end) and (actual_end > break_start)
    assert not overlaps, (
        f"Task [{actual_start}, {actual_end}) overlaps break [{break_start}, {break_end})"
    )

    if actual_start != start:
        assert actual_start == break_end, "Task shifted to somewhere other than break end"


def test_avoid_breaks_multiple_adjacent_breaks():
    breaks = (TimeWindow(100, 200), TimeWindow(200, 300), TimeWindow(400, 500))
    assert avoid_breaks(180, 50, breaks) == 300
    assert avoid_breaks(250, 150, breaks) == 500
