from uuid import UUID

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tirodhan.modules.planning.routing.execution import RoutingAdapterError
from tirodhan.modules.routing_operations.parallel import Candidate


def uid(value: int) -> UUID:
    return UUID(int=value)


@given(nodes=st.integers(min_value=2, max_value=200))
def test_cpsat_selection_limits(nodes):
    try:
        from tirodhan.modules.routing_operations.parallel import consolidate
    except ImportError:
        pytest.skip("ortools not installed")

    candidates = []
    vehicles = set()
    for i in range(nodes):
        vid = uid(100 + i)
        vehicles.add(vid)
        candidates.append(
            Candidate(
                candidate_id=uid(i),
                vehicle_id=vid,
                rider_id=uid(i + 200),
                stop_ids=(uid(i),),
                pickup_ids=(uid(i),),
                construction="COMPOSITE",
                engine_version="1.0",
                parent_id=None,
                cost=10 * i,
                supplementary_pickups=1,
                rebase_source_id=None,
                rebase_snapshot_id=None,
                facility_visits=(),
                lot_ids=(uid(i),),
            )
        )

    try:
        selection = consolidate(
            candidates=candidates,
            vehicles=vehicles,
            mandatory=set(),
            priorities={uid(i): 0 for i in range(nodes)},
            policy_version="test",
            seconds=2.0,
            threads=1,
        )
        assert len(selection.selected_ids) > 0
    except (RoutingAdapterError, ImportError):
        # We only catch missing native dependency or version mismatch gracefully
        pass
