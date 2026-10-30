import pytest
from hypothesis import given, settings, strategies as st
from uuid import UUID

from tirodhan.modules.planning.routing.contracts import TimeWindow
from tirodhan.modules.planning.routing.validation import avoid_breaks

@settings(max_examples=100)
@given(
    start=st.integers(min_value=0, max_value=86400),
    duration=st.integers(min_value=1, max_value=3600),
    break_start=st.integers(min_value=0, max_value=86400),
    break_duration=st.integers(min_value=1, max_value=3600)
)
def test_avoid_breaks_never_overlaps_break_window(start, duration, break_start, break_duration):
    """Property: A task scheduled around a break must not overlap the break window."""
    break_end = break_start + break_duration
    breaks = (TimeWindow(break_start, break_end),)

    actual_start = avoid_breaks(start, duration, breaks)
    actual_end = actual_start + duration

    # The actual task must not strictly overlap the break.
    # It can be entirely before, or entirely after (or starting exactly at the end).
    overlaps = (actual_start < break_end) and (actual_end > break_start)
    assert not overlaps, f"Task [{actual_start}, {actual_end}) overlaps break [{break_start}, {break_end})"

    # Furthermore, if it was shifted, it must be shifted to exactly the end of the break.
    if actual_start != start:
        assert actual_start == break_end, "Task shifted to somewhere other than the end of the break"

def test_avoid_breaks_multiple_adjacent_breaks():
    breaks = (
        TimeWindow(100, 200),
        TimeWindow(200, 300),
        TimeWindow(400, 500)
    )
    # A 50 second task starting at 180 should be pushed to 300 because it overlaps the first,
    # then starting at 200 overlaps the second, so it's pushed to 300.
    assert avoid_breaks(180, 50, breaks) == 300

    # A 150 second task starting at 250 should be pushed to 300,
    # but starting at 300 makes it end at 450, overlapping the third break!
    # The current implementation of `avoid_breaks` only does a single pass over the breaks,
    # so we'll see if it handles cascading overlaps. (This might be a bug we uncover!)
    # Actually wait, `avoid_breaks` loops through breaks in order.
    # start=250, duration=150.
    # check 100-200: no overlap.
    # check 200-300: overlaps! start pushed to 300.
    # check 400-500: start=300, duration=150, ends at 450. Overlaps! start pushed to 500.
    assert avoid_breaks(250, 150, breaks) == 500
