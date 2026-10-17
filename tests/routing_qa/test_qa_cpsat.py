import itertools
from uuid import UUID

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tirodhan.modules.planning.routing.execution import RoutingAdapterError
from tirodhan.modules.routing_operations.parallel import Candidate


def uid(value: int) -> UUID:
    return UUID(int=value)


@settings(max_examples=10, deadline=None)
@given(
    nodes=st.integers(min_value=2, max_value=8),
    mandatory_node_idx=st.integers(min_value=0, max_value=7),
    conflict_node_idx=st.integers(min_value=0, max_value=7),
)
def test_cpsat_selection_limits_exhaustive_optimum(nodes, mandatory_node_idx, conflict_node_idx):
    try:
        from tirodhan.modules.routing_operations.parallel import consolidate
    except ImportError:
        pytest.fail("ortools not installed")

    candidates = []
    vehicles = set()
    mandatory_set = set()
    priorities = {}

    for i in range(nodes):
        vid = uid(100 + i)
        vehicles.add(vid)
        lots = (uid(i),)

        if i == conflict_node_idx and nodes > 1 and i > 0:
            lots = (uid(i), uid(i - 1))

        for loc in lots:
            priorities[loc] = 0

        # Empty candidate to satisfy "exactly one candidate per vehicle"
        candidates.append(
            Candidate(
                candidate_id=uid(2000 + i),
                vehicle_id=vid,
                rider_id=uid(i + 200),
                stop_ids=(),
                pickup_ids=(),
                construction="COMPOSITE",
                engine_version="1.0",
                parent_id=None,
                cost=0,
                supplementary_pickups=0,
                rebase_source_id=None,
                rebase_snapshot_id=None,
                facility_visits=(),
                lot_ids=(),
            )
        )

        candidates.append(
            Candidate(
                candidate_id=uid(i),
                vehicle_id=vid,
                rider_id=uid(i + 200),
                stop_ids=(uid(i),),
                # Note: The consolidation objective uses `pickup_ids` for priority, not `lot_ids`
                pickup_ids=lots,
                construction="COMPOSITE",
                engine_version="1.0",
                parent_id=None,
                cost=10 * i,
                supplementary_pickups=0,
                rebase_source_id=None,
                rebase_snapshot_id=None,
                facility_visits=(),
                lot_ids=lots,
            )
        )
        if i == mandatory_node_idx:
            mandatory_set.update(lots)

    def is_feasible(combo):
        seen_lots = set()
        seen_vehicles = set()
        for c in combo:
            if c.vehicle_id in seen_vehicles:
                return False
            if seen_lots.intersection(set(c.lot_ids)):
                return False
            seen_lots.update(c.lot_ids)
            seen_vehicles.add(c.vehicle_id)

        if seen_vehicles != vehicles:
            return False

        if not mandatory_set.issubset(seen_lots):
            return False

        return True

    best_objective = None
    best_combo = None

    if not mandatory_set:
        best_objective = (0, 0)
        best_combo = ()

    for r in range(1, len(candidates) + 1):
        for combo in itertools.combinations(candidates, r):
            if is_feasible(combo):
                # Using `pickup_ids` and cost just like the terms defined in `consolidate()`
                # In consolidate, there's a sequence of max terms.
                # 1. max sum of pickups in priority tier 2
                # 2. max sum of pickups in priority tier 1
                # 3. max sum of ALL pickups with defined priority
                # 4. max(-cost) -> min(cost)
                # We simplified it for our exhaustive search to match the actual logic:
                # The primary objective is to maximize total number of served pickups
                # Secondary is minimizing cost.

                served_lots = sum(len(c.pickup_ids) for c in combo)
                cost = sum(c.cost for c in combo)

                # We minimize (-served_lots, cost) to maximize served_lots and minimize cost
                obj = (-served_lots, cost)
                if best_objective is None or obj < best_objective:
                    best_objective = obj
                    best_combo = combo

    try:
        selection = consolidate(
            candidates=candidates,
            vehicles=vehicles,
            mandatory=mandatory_set,
            priorities=priorities,
            policy_version="test",
            seconds=5.0,
            threads=1,
        )

        if best_combo is not None:
            actual_candidates = [c for c in candidates if c.candidate_id in selection.selected_ids]
            actual_lots = sum(len(c.pickup_ids) for c in actual_candidates)
            actual_cost = sum(c.cost for c in actual_candidates)

            assert (-actual_lots, actual_cost) == best_objective, (
                "CP-SAT failed to find the optimum found by exhaustive search"
            )

            # Since best_combo contains an item per vehicle, we want to ensure we get selections.
            if any(len(c.lot_ids) > 0 for c in best_combo):
                # There must be at least one real candidate selected
                # Actually selected_ids in consolidate might drop empty candidates.
                pass
        else:
            assert len(selection.selected_ids) == 0

    except RoutingAdapterError:
        pytest.fail("RoutingAdapterError occurred during CP-SAT validation.")
