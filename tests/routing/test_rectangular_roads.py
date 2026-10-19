"""Directed provider cells, validation and recoverability of sparse road acquisition."""

import asyncio
import json
import logging
import os
from pathlib import Path
from uuid import UUID

import httpx
import pytest

from tirodhan.modules.planning.routing.execution import RoutingAdapterError
from tirodhan.modules.planning.routing.osrm import OSRMMatrixProvider, RoadNode
from tirodhan.modules.routing_operations.admission_roads import DirectedRoadCache


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "negative", "timeout", "snap", "reachability"])
async def test_rectangular_star_strictly_validates_and_recovers(failure):
    logging.getLogger("httpx").setLevel(logging.WARNING)
    calls, cells = [], []
    bad = failure

    def handler(request):
        n = 5
        sources = list(map(int, request.url.params["sources"].split(";")))
        destinations = list(map(int, request.url.params["destinations"].split(";")))
        assert len(sources) == 1 or len(destinations) == 1
        calls.append((sources, destinations))
        cells.append(len(sources) * len(destinations))
        if bad == "timeout":
            raise httpx.ReadTimeout("restricted provider detail", request=request)
        waypoints = [dict(distance=0, hint=str(i), location=[77 + i / 1000, 28]) for i in range(n)]
        durations = [[0 if a == b else 10 * a + b for b in destinations] for a in sources]
        distances = [[0 if a == b else 100 * a + b for b in destinations] for a in sources]
        if bad == "negative":
            distances[0][-1] = -0.3
        if bad == "reachability":
            distances[0][-1] = None
        if bad == "snap":
            waypoints[0]["distance"] = 500
        return httpx.Response(
            200,
            json=dict(
                code="Ok",
                durations=durations,
                distances=distances,
                sources=[waypoints[i] for i in sources],
                destinations=[waypoints[i] for i in destinations],
            ),
        )

    nodes = tuple(RoadNode(UUID(int=i + 1), 77 + i / 1000, 28) for i in range(5))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OSRMMatrixProvider(
            client=client,
            endpoint="http://localhost:5000",
            graph_version="graph",
            profile_version="profile",
            max_nodes=10,
            max_snap_distance_m=50,
        )
        cache = DirectedRoadCache(provider)
        pairs = {(nodes[0].node_id, n.node_id) for n in nodes} | {
            (n.node_id, nodes[0].node_id) for n in nodes
        }
        if failure:
            with pytest.raises(RoutingAdapterError):
                await cache.acquire_arcs(nodes, pairs)
            assert not cache.entries
        bad = None
        before = len(calls)
        matrix = await cache.acquire_arcs(nodes, pairs)
        assert len(calls) - before == 2
        assert sum(cells[-2:]) == 10 < 25
        assert matrix.arc(nodes[0].node_id, nodes[4].node_id, {}) == (4, 4)
        assert matrix.arc(nodes[4].node_id, nodes[0].node_id, {}) == (40, 400)
        await cache.acquire_arcs(nodes, pairs)
        assert len(calls) - before == 2
        assert (
            len(matrix.snap_tokens) == 0
        )  # Cache publishes arcs, not provider-private snap tokens.


@pytest.mark.asyncio
async def test_cancellation_during_second_rectangle_does_not_publish_partial_cache():
    logging.getLogger("httpx").setLevel(logging.WARNING)
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        if count == 2:
            raise asyncio.CancelledError()
        return httpx.Response(
            200,
            json=dict(
                code="Ok",
                sources=[dict(distance=0)],
                destinations=[dict(distance=0) for _ in range(5)],
                durations=[[0, 1, 2, 3, 4]],
                distances=[[0, 1, 2, 3, 4]],
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OSRMMatrixProvider(
            client=client,
            endpoint="http://localhost:5000",
            graph_version="g",
            profile_version="p",
            max_nodes=5,
            max_snap_distance_m=50,
        )
        cache = DirectedRoadCache(provider)
        nodes = tuple(RoadNode(UUID(int=i + 1), 77 + i / 1000, 28) for i in range(5))
        pairs = {(nodes[0].node_id, n.node_id) for n in nodes} | {
            (n.node_id, nodes[0].node_id) for n in nodes
        }
        with pytest.raises(asyncio.CancelledError):
            await cache.acquire_arcs(nodes, pairs)
        assert not cache.entries


@pytest.mark.asyncio
@pytest.mark.routing_engine
async def test_native_rectangles_match_dense_directed_osrm_values():
    if os.getenv("TIRODHAN_ROUTING_ENGINE_TESTS") != "1":
        pytest.skip("explicit pinned OSRM execution required")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    public = json.loads((Path(__file__).parent / "data/inc8-road-nodes.json").read_text())[:32]
    nodes = tuple(
        RoadNode(UUID(int=i + 1), n["longitude"], n["latitude"]) for i, n in enumerate(public)
    )
    async with httpx.AsyncClient(trust_env=False) as client:
        provider = OSRMMatrixProvider(
            client=client,
            endpoint=os.environ["TIRODHAN_OSRM_TEST_URL"],
            graph_version="fixture-car",
            profile_version="test-car",
            max_nodes=64,
            max_snap_distance_m=300,
        )
        dense = await provider.acquire(nodes)
        cache = DirectedRoadCache(provider)
        pairs = {(nodes[0].node_id, n.node_id) for n in nodes} | {
            (n.node_id, nodes[0].node_id) for n in nodes
        }
        sparse = await cache.acquire_arcs(nodes, pairs)
        index = {n.node_id: i for i, n in enumerate(nodes)}
        assert all(sparse.arc(a, b, index) == dense.arc(a, b, index) for a, b in pairs)
        assert cache.provider_cells == 64 < len(nodes) ** 2
