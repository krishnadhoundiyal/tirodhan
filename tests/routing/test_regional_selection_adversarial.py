import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.routing.test_regional_selection import route, uid
from tirodhan.modules.routing_operations.parallel import consolidate

pytest.importorskip("ortools", reason="regional selection requires the optional routing extra")


@settings(max_examples=50, deadline=None)
@given(
    st.lists(
        st.tuples(
            st.integers(1, 100),  # candidate id
            st.integers(1, 10),  # vehicle id
            st.lists(st.integers(1, 20), min_size=1, max_size=5, unique=True),  # lots
            st.integers(0, 1000),  # cost
        ),
        min_size=2,
        max_size=15,
    )
)
def test_adversarial_overlapping_booking_removal_and_decision_quality(candidate_data):
    pool = []
    priorities = {}
    for cid, vid, lots, cost in candidate_data:
        pool.append(route(cid, vid, lots, cost))
        for lot in lots:
            if uid(lot) not in priorities:
                priorities[uid(lot)] = 0

    # ensure candidate_ids are unique
    seen_cids = set()
    unique_pool = []
    for c in pool:
        if c.candidate_id not in seen_cids:
            seen_cids.add(c.candidate_id)
            unique_pool.append(c)

    pool = unique_pool

    vehicles = {c.vehicle_id for c in pool}

    result = consolidate(pool, vehicles, set(), priorities, policy_version="test", seconds=2)

    selected_candidates = [c for c in pool if c.candidate_id in result.selected_ids]

    # Check 1: No overlapping lot_ids across selected routes
    covered_lots = set()
    for c in selected_candidates:
        for lot in c.lot_ids:
            assert lot not in covered_lots, f"Overlapping lot found: {lot}"
            covered_lots.add(lot)

    # Check 2: At most one candidate selected per vehicle
    selected_vehicles = set()
    for c in selected_candidates:
        assert c.vehicle_id not in selected_vehicles, (
            f"Multiple candidates for vehicle: {c.vehicle_id}"
        )
        selected_vehicles.add(c.vehicle_id)
