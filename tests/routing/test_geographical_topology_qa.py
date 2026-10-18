"""Independent topology, sparse-arc and pruning checks; QA dependencies are mandatory."""

import asyncio
import json
import os
import random
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from geographical_fixture import candidates, sample
from geographical_history import audit

from tirodhan.modules.planning.routing.contracts import TimeWindow
from tirodhan.modules.planning.routing.execution import RoutingAdapterError
from tirodhan.modules.planning.routing.osrm import OSRMMatrixProvider, RoadNode
from tirodhan.modules.planning.routing.scheduling import RouteSchedule
from tirodhan.modules.planning.routing.validation import RoutingValidationError, _arc
from tirodhan.modules.routing_operations.admission_roads import DirectedRoadCache


@pytest.mark.parametrize(
    "missing",
    [
        "TIRODHAN_TEST_DATABASE_URL",
        "TIRODHAN_OSRM_TEST_URL",
        "TIRODHAN_OSRM_GRAPH_VERSION",
    ],
)
def test_qualification_runner_fails_on_missing_mandatory_dependency(missing):
    environment = dict(os.environ)
    environment.update(
        TIRODHAN_GEO_QA="1",
        TIRODHAN_ROUTING_ENGINE_TESTS="1",
        TIRODHAN_TEST_DATABASE_URL="unused-test-placeholder",
        TIRODHAN_OSRM_TEST_URL="http://127.0.0.1:1",
        TIRODHAN_OSRM_GRAPH_VERSION="fixture-car",
    )
    environment.pop(missing, None)
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("geographical_verify.py")),
            "--output",
            "/tmp/not-a-qualified-run.json",
            "unused-selection",
        ],
        env=environment,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert b"Mandatory" in result.stderr and b"skipped" not in result.stdout


def test_geographical_sampler_is_deterministic_and_has_real_public_provenance():
    rows, ways = candidates(Path(__file__).with_name("data") / "delhi-road.osm.pbf")
    a = sample(rows, 512)
    assert a == sample(rows, 512)
    assert len({(r["longitude"], r["latitude"]) for r in a}) == 512
    assert all(r["ways"] and r["osm_node_id"] for r in a)
    assert any(r["one_way"] for r in a)
    assert any(tags.get("bridge") == "yes" for _, tags, _ in ways)


def test_independent_history_audit_preserves_every_prior_obligation_and_detects_omission():
    data = dict(
        reservations=[("a", 2), ("b", 3)],
        members=[("proof-a", 2, None), ("proof-b", 3, None)],
        links=[("proof-a", "a"), ("proof-b", "b")],
    )
    assert audit(data)["checked_obligation_pairs"] == 3
    data["members"][0] = ("proof-a", 2, 3)
    with pytest.raises(AssertionError, match="missing"):
        audit(data)


def independent_order_valid(problem, order):
    """Brute-force integer scheduling; no summary/gate/calendar helpers."""
    vehicle = problem.vehicles[0]
    stops = {s.stop_id: s for s in problem.stops}
    lots = {lot.lot_id: lot for lot in problem.lots}
    matrix = problem.matrices[0]
    indices = {n: i for i, n in enumerate(matrix.node_ids)}
    balance = {c.lot_id: c.quantity for c in vehicle.initial_cargo}
    possible = {max(vehicle.available_at, vehicle.shift.start)}
    node = vehicle.start_node_id
    for identity in (*order, None):
        stop = stops[identity] if identity else None
        target = stop.node_id if stop else vehicle.end_node_id
        duration, _ = matrix.arc(node, target, indices)
        if duration is None:
            return False
        arrivals = {
            depart + duration
            for end in possible
            for depart in range(end, vehicle.shift.end - duration + 1)
            if not any(depart < b.end and depart + duration > b.start for b in vehicle.breaks)
        }
        if not stop:
            return any(t <= vehicle.shift.end for t in arrivals)
        starts = {
            start
            for arrival in arrivals
            for start in range(
                max(arrival, stop.service_start_window.start), stop.service_start_window.end + 1
            )
            if not any(
                start < b.end and start + stop.service_seconds > b.start for b in vehicle.breaks
            )
        }
        possible = {s + stop.service_seconds for s in starts}
        if stop.kind.value == "PICKUP":
            balance[stop.lot_id] = balance.get(stop.lot_id, 0) + stop.quantity
        else:
            if balance.get(stop.lot_id, 0) < stop.quantity:
                return False
            balance[stop.lot_id] -= stop.quantity
            possible = {t for t in possible if t <= lots[stop.lot_id].discharge_deadline}
        load = [
            sum(lots[k].unit_load[d] * q for k, q in balance.items())
            for d in range(len(vehicle.capacity))
        ]
        if not all(q <= cap for q, cap in zip(load, vehicle.capacity, strict=True)):
            return False
        if not possible:
            return False
        node = target
    return False


@pytest.mark.parametrize("seed", range(20))
def test_pruning_matches_separate_exhaustive_time_and_gap_oracle(routing_problem, seed):
    rng = random.Random(20261018 + seed)
    times = tuple(tuple(0 if i == j else rng.randrange(0, 5) for j in range(4)) for i in range(4))
    problem = replace(
        routing_problem,
        matrices=(replace(routing_problem.matrices[0], travel_seconds=times),),
        vehicles=(
            replace(
                routing_problem.vehicles[0], shift=TimeWindow(0, 30), breaks=(TimeWindow(12, 15),)
            ),
        ),
        lots=tuple(replace(lot, discharge_deadline=30) for lot in routing_problem.lots),
        receiving_points=tuple(
            replace(p, opening_windows=(TimeWindow(0, 30),))
            for p in routing_problem.receiving_points
        ),
        stops=tuple(
            replace(s, service_seconds=1, service_start_window=TimeWindow(0, 25))
            for s in routing_problem.stops
        ),
    )
    old = (UUID(int=50), UUID(int=52))
    assert independent_order_valid(problem, old), dict(seed=seed, reason="invalid oracle incumbent")
    expected = []
    for i in range(1, len(old) + 1):
        for j in range(i, len(old) + 1):
            order = old[:i] + (UUID(int=51),) + old[i:j] + (UUID(int=53),) + old[j:]
            if independent_order_valid(problem, order):
                expected.append(order)
    result = RouteSchedule(problem, problem.vehicles[0], old).insert(
        problem.stops[1],
        problem.stops[3],
        max_evaluations=100,
        deadline=time.monotonic() + 10,
    )
    assert result.order == (expected[0] if expected else None), dict(seed=seed, expected=expected)


@pytest.mark.asyncio
@pytest.mark.skipif(os.getenv("TIRODHAN_GEO_QA") != "1", reason="explicit real topology QA")
async def test_real_directed_bridge_restriction_profile_cold_warm_and_missing_arc():
    assert os.getenv("TIRODHAN_GEO_QA") == "1", "Explicit real QA dependencies required"
    nodes = tuple(
        RoadNode(UUID(int=i + 1), lon, lat)
        for i, (lon, lat) in enumerate(((77.0005, 28.0), (77.0025, 28.0), (77.0105, 28.01)))
    )
    results = {}
    async with httpx.AsyncClient(trust_env=False) as client:
        for profile, port in (("car", 5031), ("bicycle", 5032)):
            provider = OSRMMatrixProvider(
                client=client,
                endpoint=f"http://127.0.0.1:{port}",
                graph_version="synthetic-topology",
                profile_version=profile,
                max_nodes=16,
                max_snap_distance_m=2,
            )
            cache = DirectedRoadCache(provider)
            stamp = time.perf_counter()
            sparse = await cache.acquire_arcs(nodes, {(nodes[0].node_id, nodes[1].node_id)})
            cold = time.perf_counter() - stamp
            calls = cache.queries
            stamp = time.perf_counter()
            await cache.acquire_arcs(nodes, {(nodes[0].node_id, nodes[1].node_id)})
            warm = time.perf_counter() - stamp
            assert cache.queries == calls and cache.hits > 0
            with pytest.raises(RoutingValidationError, match="ROAD_ARC_NOT_ACQUIRED"):
                _arc(sparse, {}, nodes[1].node_id, nodes[0].node_id)
            full = await cache.acquire(nodes)
            assert full.travel_seconds[0][1] is not None
            assert full.travel_seconds[0][2] is None and full.travel_seconds[2][0] is None
            if profile == "car":
                assert full.travel_seconds[1][0] is None  # Directed bridge, restricted alternative.
                with pytest.raises(RoutingValidationError, match="UNREACHABLE_ARC"):
                    _arc(
                        full,
                        {n.node_id: i for i, n in enumerate(nodes)},
                        nodes[1].node_id,
                        nodes[0].node_id,
                    )
            else:
                assert full.travel_seconds[1][0] is not None  # Explicit bicycle authorization.
            moved = (*nodes[:2], replace(nodes[2], longitude=77.0106))
            before_movement = cache.queries
            await cache.acquire_arcs(moved, {(moved[2].node_id, moved[2].node_id)})
            assert cache.queries > before_movement
            results[profile] = dict(
                cold_seconds=cold,
                warm_seconds=warm,
                queries=cache.queries,
                provider_cells=cache.provider_cells,
                hits=cache.hits,
                travel_seconds=full.travel_seconds,
                distance_m=full.distance_m,
            )
    if output := os.getenv("TIRODHAN_TOPOLOGY_QA_OUTPUT"):
        await asyncio.to_thread(
            Path(output).write_text, json.dumps(results, indent=2, sort_keys=True) + "\n"
        )


@pytest.mark.asyncio
@pytest.mark.skipif(os.getenv("TIRODHAN_GEO_QA") != "1", reason="explicit real provider QA")
async def test_public_near_coincident_negative_distance_is_rejected_without_repair():
    assert os.getenv("TIRODHAN_GEO_QA") == "1", "Mandatory real OSRM required"
    fixture = json.loads(
        (Path(__file__).parents[2] / "docs/benchmarks/GEOGRAPHICAL_QA_FIXTURE.json").read_text()
    )
    example = fixture["campaigns"]["E"]["invalid_examples"][0]
    sites = [example["source"], example["destination"]]
    async with httpx.AsyncClient(trust_env=False) as client:
        coords = ";".join(f"{r['longitude']},{r['latitude']}" for r in sites)
        raw = (
            await client.get(
                f"http://127.0.0.1:5000/table/v1/driving/{coords}",
                params={"annotations": "distance,duration"},
            )
        ).json()
        assert raw["code"] == "Ok"
        assert any(v is not None and v < 0 for row in raw["distances"] for v in row)
        nodes = tuple(
            RoadNode(UUID(int=i + 1), r["longitude"], r["latitude"]) for i, r in enumerate(sites)
        )
        provider = OSRMMatrixProvider(
            client=client,
            endpoint="http://127.0.0.1:5000",
            graph_version="fixture-car",
            profile_version="test-car",
            max_nodes=16,
            max_snap_distance_m=300,
        )
        cache = DirectedRoadCache(provider)
        with pytest.raises(RoutingAdapterError, match="INVALID_ROAD_MATRIX"):
            await cache.acquire(nodes)
        assert not cache.entries
    if output := os.getenv("TIRODHAN_NEGATIVE_QA_OUTPUT"):
        await asyncio.to_thread(
            Path(output).write_text,
            json.dumps(
                dict(
                    seed=20261018,
                    input_sites=sites,
                    raw_osrm=raw,
                    result="INVALID_ROAD_MATRIX; no cache entries or invented distances",
                ),
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
