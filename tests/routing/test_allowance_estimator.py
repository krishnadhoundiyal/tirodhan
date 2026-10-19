from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tirodhan.modules.collection_requests.scheduling import SlotWindow
from tirodhan.modules.routing_operations.allowance_estimator import (
    AllowancePolicy,
    TravelBand,
    Workload,
    footprint,
    free_intervals,
    pickup_supply,
    remaining_allowance,
)


def parameters(**updates):
    values = dict(
        version=1,
        local_travel_seconds=120,
        clustered_travel_seconds=60,
        facility_travel_seconds=1500,
        stem_seconds=0,
        cluster_threshold=2,
        pickup_start_offset_seconds=900,
        limited_bookings=1,
    )
    return AllowancePolicy(**dict(values, **updates))


EPOCH = datetime(2026, 1, 1, 9, tzinfo=timezone.utc)


@given(st.integers(1, 86400), st.integers(0, 86400), st.integers(0, 7200), st.integers(0, 7200))
def test_integer_footprint_preserves_every_second(pickup, discharge, travel, facility):
    policy = parameters(
        local_travel_seconds=travel, clustered_travel_seconds=0, facility_travel_seconds=facility
    )
    work = Workload(EPOCH, EPOCH + timedelta(minutes=30), pickup, discharge)
    values = footprint(work, policy)
    assert sum(values.values()) == pickup + discharge + travel + facility
    assert all(0 < value <= 1800 and key.minute in (0, 30) for key, value in values.items())


def test_spillover_consumes_shared_neighbouring_slot():
    policy = parameters()
    work = Workload(EPOCH, EPOCH + timedelta(minutes=30), 180, 60)
    assert footprint(work, policy) == {EPOCH: 1020, EPOCH + timedelta(minutes=30): 840}
    remaining = {
        (EPOCH, "PICKUP"): 1800,
        (EPOCH, "DUTY"): 1800,
        (EPOCH + timedelta(minutes=30), "DUTY"): 1800,
    }
    assert remaining_allowance(work, policy, remaining) == 1
    remaining[(EPOCH + timedelta(minutes=30), "DUTY")] = 839
    assert remaining_allowance(work, policy, remaining) == 0


def test_break_fragments_cannot_be_combined_for_service():
    policy = parameters(
        local_travel_seconds=0, clustered_travel_seconds=0, facility_travel_seconds=0
    )
    end = EPOCH + timedelta(minutes=30)
    free = free_intervals(
        EPOCH, end, [(EPOCH + timedelta(minutes=5), EPOCH + timedelta(minutes=25))]
    )
    assert sum(int((b - a).total_seconds()) for a, b in free) == 600
    assert (
        pickup_supply(
            [SlotWindow(EPOCH, end)],
            free,
            [(EPOCH, end)],
            pickup_seconds=420,
            discharge_seconds=0,
            policy=policy,
        )[EPOCH]
        == 0
    )


def test_pickup_may_finish_after_service_start_slot():
    policy = parameters(
        local_travel_seconds=0, clustered_travel_seconds=0, facility_travel_seconds=0
    )
    end = EPOCH + timedelta(minutes=30)
    free = [(EPOCH + timedelta(minutes=25), EPOCH + timedelta(hours=1))]
    assert (
        pickup_supply(
            [SlotWindow(EPOCH, end)],
            free,
            [(EPOCH, EPOCH + timedelta(hours=1))],
            pickup_seconds=420,
            discharge_seconds=0,
            policy=policy,
        )[EPOCH]
        > 0
    )


def test_facility_tail_excludes_late_pickup():
    policy = parameters(facility_travel_seconds=1200)
    end = EPOCH + timedelta(minutes=30)
    assert (
        pickup_supply(
            [SlotWindow(EPOCH, end)],
            [(EPOCH, end)],
            [(EPOCH, EPOCH + timedelta(minutes=20))],
            pickup_seconds=300,
            discharge_seconds=300,
            policy=policy,
        )[EPOCH]
        == 0
    )


def test_time_bands_change_forecast_without_learning():
    band = TravelBand(
        start_minute=14 * 60,
        end_minute=15 * 60,
        local_travel_seconds=600,
        clustered_travel_seconds=300,
        facility_travel_seconds=1800,
    )
    policy = parameters(travel_bands=(band,))
    # 09:00 UTC is 14:30 Asia/Kolkata.
    assert (
        sum(footprint(Workload(EPOCH, EPOCH + timedelta(minutes=30), 180, 60), policy).values())
        == 2640
    )
    with pytest.raises(ValueError):
        parameters(travel_bands=(band, band))


@given(st.lists(st.tuples(st.integers(0, 100), st.integers(0, 100)), max_size=12))
def test_free_interval_union_matches_independent_second_oracle(blocks):
    excluded = [
        (EPOCH + timedelta(seconds=min(a, b)), EPOCH + timedelta(seconds=max(a, b)))
        for a, b in blocks
    ]
    actual = free_intervals(EPOCH, EPOCH + timedelta(seconds=100), excluded)
    for second in range(100):
        at = EPOCH + timedelta(seconds=second)
        assert any(a <= at < b for a, b in actual) == (not any(a <= at < b for a, b in excluded))
