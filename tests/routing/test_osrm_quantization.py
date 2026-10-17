"""Pinned native upstream defect regression: reject it, never normalize or reroute it."""

import os
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from probe_regional_matrix import diagnostic_nodes, probe

from tirodhan.modules.planning.routing.adapters import ORToolsOptimizer, VroomOptimizer
from tirodhan.modules.planning.routing.contracts import TimeWindow
from tirodhan.modules.planning.routing.execution import RoutingAdapterError
from tirodhan.modules.planning.routing.osrm import OSRMMatrixProvider, RoadNode
from tirodhan.modules.planning.routing.validation import validate_proposal
from tirodhan.workers.routing_regional import LocalRoadProfile, road_processes


@pytest.mark.asyncio
async def test_raw_phantom_quantization_remains_a_fail_closed_release_blocker():
    graph = os.getenv("TIRODHAN_REGIONAL_GRAPH_DIR")
    if not graph:
        pytest.skip("explicit pinned local OSRM graph required")
    result = await probe(Path(graph) / "car/map.osrm")
    assert result["strict_provider"] == "INVALID_ROAD_MATRIX"
    assert result["negative_cells"] == {"durations": 0, "distances": 69}
    assert result["quantized_same_edge_negative_cells"] == 69
    assert result["invalid_diagonals"] == {"durations": 0, "distances": 0}
    assert result["one_way_example"] == dict(
        source_index=4,
        target_index=20,
        table_distance=-0.2,
        table_duration=0,
        route=dict(distance=0.2, duration=0, weight=0),
    )


@pytest.mark.asyncio
async def test_same_native_provider_serves_valid_roads_after_rejected_input(routing_problem):
    graph = os.getenv("TIRODHAN_REGIONAL_GRAPH_DIR")
    if not graph or os.getenv("TIRODHAN_ROUTING_ENGINE_TESTS") != "1":
        pytest.skip("explicit pinned roads and native engines required")
    profile = LocalRoadProfile(
        profile_version="profile-1",
        graph_version="fixture-car",
        graph_path=Path(graph) / "car/map.osrm",
        port=5030,
        threads=1,
        max_snap_distance_m=300,
        block_nodes=128,
        max_pairs=65536,
    )
    with road_processes([profile]):
        async with httpx.AsyncClient(trust_env=False) as client:
            provider = OSRMMatrixProvider(
                client=client,
                endpoint="http://127.0.0.1:5030",
                graph_version="fixture-car",
                profile_version="profile-1",
                max_nodes=128,
                max_snap_distance_m=300,
            )
            with pytest.raises(RoutingAdapterError, match="INVALID_ROAD_MATRIX"):
                await provider.acquire(diagnostic_nodes())
            anchors = [(77.207, 28.632), (77.209, 28.632), (77.2105, 28.6305), (77.211, 28.630)]
            matrix = await provider.acquire(
                tuple(RoadNode(UUID(int=i), *p) for i, p in enumerate(anchors))
            )
            assert matrix.graph_version == "fixture-car" and matrix.profile_version == "profile-1"
            problem = replace(
                routing_problem,
                matrices=(matrix,),
                vehicles=tuple(
                    replace(v, matrix_id=matrix.matrix_id, shift=TimeWindow(0, 5000))
                    for v in routing_problem.vehicles
                ),
                stops=tuple(
                    replace(s, service_start_window=TimeWindow(0, 4000))
                    for s in routing_problem.stops
                ),
                receiving_points=tuple(
                    replace(p, opening_windows=(TimeWindow(0, 5000),))
                    for p in routing_problem.receiving_points
                ),
                lots=tuple(replace(lot, discharge_deadline=4000) for lot in routing_problem.lots),
            )
            for optimizer in (VroomOptimizer(), ORToolsOptimizer()):
                proposal = await optimizer.propose(problem, search_seconds=1)
                assessment = validate_proposal(problem, proposal)
                assert assessment.routes[0].stops[-1].load_after == (0, 0)
