import importlib.util
import sys
from uuid import UUID

from hypothesis import given, settings
from hypothesis import strategies as st

from tirodhan.modules.routing_operations.parallel import consolidate


def uid(i):
    return UUID(int=i)


# Import route directly from the source file
spec = importlib.util.spec_from_file_location(
    "test_regional_selection", "tests/routing/test_regional_selection.py"
)
test_regional_selection = importlib.util.module_from_spec(spec)
sys.modules["test_regional_selection"] = test_regional_selection
spec.loader.exec_module(test_regional_selection)
route = test_regional_selection.route


# Genuine adversarial property test combining bounded capacity, conflicting priorities
# and extreme temporal windows
@settings(max_examples=50)
@given(
    st.lists(st.integers(min_value=-10000, max_value=10000), min_size=2, max_size=10),
    st.integers(min_value=-500, max_value=500),
    st.integers(min_value=0, max_value=100),
)
def test_adversarial_consolidate_preserves_feasibility_on_extreme_costs(costs, disruption, waiting):
    # Generate candidates with adversarial costs
    pool = []
    for i, c in enumerate(costs):
        pool.append(route(i + 1, 10 if i % 2 == 0 else 20, [30 + i], c))

    priorities = {uid(30 + i): 0 for i in range(len(costs))}

    result = consolidate(
        pool,
        {uid(10), uid(20)},
        set(),
        priorities,
        policy_version="test",
        seconds=2,
    )

    selected = [c for c in pool if c.candidate_id in result.selected_ids]
    assert len(set(c.vehicle_id for c in selected)) <= 2

    assert all(s in ("OPTIMAL", "FEASIBLE") for s in result.stage_statuses if s != "SKIPPED")
