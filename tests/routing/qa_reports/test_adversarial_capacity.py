from uuid import UUID

from hypothesis import given, settings
from hypothesis import strategies as st

from tirodhan.modules.routing_operations.parallel import (
    Candidate,
    consolidate,
)


def uid(value: int) -> UUID:
    return UUID(int=value)


def route(i: int, vehicle: int, lots: list[int], cost: int):
    return Candidate(
        candidate_id=uid(i),
        vehicle_id=uid(vehicle),
        rider_id=uid(100 + vehicle),
        stop_ids=tuple(uid(1000 + x) for x in lots),
        lot_ids=tuple(map(uid, lots)),
        pickup_ids=tuple(map(uid, lots)),
        construction="TEST",
        engine_version="1.0",
        cost=cost,
        facility_visits=(),
        parent_id=None,
    )


@settings(max_examples=10, deadline=None)
@given(
    cost1=st.integers(1, 20),
    cost2=st.integers(1, 20),
    cost3=st.integers(1, 20),
    cost4=st.integers(1, 20),
    p1=st.integers(0, 10),
    p2=st.integers(0, 10),
    p3=st.integers(0, 10),
    p4=st.integers(0, 10),
)
def test_adversarial_overlapping_demand_and_mandatory_work(
    cost1, cost2, cost3, cost4, p1, p2, p3, p4
):
    pool = [
        route(1, 10, [30, 31], cost1),
        route(2, 10, [31, 32], cost2),
        route(3, 20, [32, 33], cost3),
        route(4, 20, [30], cost4),
    ]
    result = consolidate(
        pool,
        {uid(10), uid(20)},
        {uid(31)},
        {uid(30): p1, uid(31): p2, uid(32): p3, uid(33): p4},
        policy_version="hybrid",
        seconds=2,
    )
    assert result.stage_statuses[0] in ("OPTIMAL", "FEASIBLE")
    selected = set(result.selected_ids)
    assert uid(1) in selected or uid(2) in selected
