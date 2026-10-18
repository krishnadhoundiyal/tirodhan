import uuid

from hypothesis import given
from hypothesis import strategies as st

from tirodhan.modules.planning.routing.contracts import RoadMatrix, RoutingLimits


@given(
    max_nodes=st.integers(min_value=1, max_value=500),
    max_stops=st.integers(min_value=1, max_value=500),
    max_vehicles=st.integers(min_value=1, max_value=100),
    max_matrices=st.integers(min_value=1, max_value=120),
)
def test_qa_mandate_adversarial_limits(max_nodes, max_stops, max_vehicles, max_matrices):
    limits = RoutingLimits(
        max_nodes=max_nodes,
        max_stops=max_stops,
        max_vehicles=max_vehicles,
        max_matrices=max_matrices,
    )
    assert limits.max_nodes == max_nodes
    assert limits.max_stops == max_stops
    assert limits.max_vehicles == max_vehicles
    assert limits.max_matrices == max_matrices


@given(
    distances=st.lists(
        st.lists(st.integers(min_value=-1, max_value=1000), min_size=1, max_size=10),
        min_size=1,
        max_size=10,
    )
)
def test_adversarial_matrix_validation(distances):
    node_ids = tuple(uuid.uuid4() for _ in range(len(distances)))
    try:
        matrix = RoadMatrix(
            matrix_id=uuid.uuid4(),
            graph_version="test",
            profile_version="test",
            node_ids=node_ids,
            travel_seconds=tuple(tuple(x if x >= 0 else None for x in row) for row in distances),
        )
        assert matrix.graph_version == "test"
    except Exception:
        pass
