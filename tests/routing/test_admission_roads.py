from dataclasses import replace
from uuid import UUID

import pytest

from tirodhan.modules.planning.routing.contracts import RoadMatrix
from tirodhan.modules.planning.routing.execution import RoutingAdapterError
from tirodhan.modules.planning.routing.osrm import RoadNode
from tirodhan.modules.routing_operations.admission_roads import DirectedRoadCache


class Roads:
    def __init__(self):
        self.calls = 0
        self.bad = False
        self.graph = "graph"

    @property
    def cache_namespace(self):
        return self.graph, "profile"

    async def acquire(self, nodes):
        self.calls += 1
        times = tuple(
            tuple(
                0 if a.longitude == b.longitude else int(10 + a.longitude * 2 + b.longitude)
                for b in nodes
            )
            for a in nodes
        )
        if self.bad:
            times = ((0, -1), (1, 0))
        return RoadMatrix(
            UUID(int=self.calls),
            self.graph,
            "profile",
            tuple(n.node_id for n in nodes),
            times,
            times,
            snap_tokens=tuple((str(n.longitude), str(n.longitude)) for n in nodes),
        )


@pytest.mark.asyncio
async def test_directed_incremental_acquisition_reuses_old_arcs_and_bounds_bytes():
    source = Roads()
    roads = DirectedRoadCache(source, max_bytes=100_000, block_nodes=4)
    nodes = tuple(RoadNode(UUID(int=i + 1), float(i), 28) for i in range(12))
    first = await roads.acquire(nodes[:3])
    old_calls = source.calls
    second = await roads.acquire(nodes[:4])
    # A star expansion fits in one square block; both directions are retained.
    assert source.calls - old_calls == 1
    assert second.travel_seconds[0][1] == first.travel_seconds[0][1]
    assert second.travel_seconds[0][1] != second.travel_seconds[1][0]
    calls = source.calls
    await roads.acquire(nodes[:4])
    assert source.calls == calls
    assert roads.bytes <= roads.max_bytes // 2


@pytest.mark.asyncio
async def test_invalid_provider_response_is_not_cached_and_can_recover():
    source = Roads()
    roads = DirectedRoadCache(source)
    nodes = (RoadNode(UUID(int=1), 1, 28), RoadNode(UUID(int=2), 2, 28))
    source.bad = True
    with pytest.raises(RoutingAdapterError, match="INVALID_ROAD_MATRIX"):
        await roads.acquire(nodes)
    assert not roads.entries
    source.bad = False
    matrix = await roads.acquire(nodes)
    assert matrix.travel_seconds[0][1] > 0
    source.graph = "new"
    with pytest.raises(RoutingAdapterError, match="MATRIX_PROVENANCE_CHANGED"):
        await roads.acquire(nodes + (replace(nodes[0], node_id=UUID(int=3), longitude=3),))
