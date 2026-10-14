"""Opt-in synthetic road/fleet measurements; emits public-fixture aggregates only.

Run inside the pinned native test image with TIRODHAN_OSRM_TEST_URL configured.
This is a benchmark, not a production policy or a claim about full Delhi traffic.
"""

import asyncio
import json
import os
import resource
import time
from dataclasses import asdict
from datetime import datetime, timezone
from uuid import UUID

import httpx

from tirodhan.modules.planning.routing.adapters import ORToolsOptimizer, VroomOptimizer
from tirodhan.modules.planning.routing.contracts import (
    ROUTING_CONTRACT_V2,
    CargoBalance,
    CargoLot,
    ReceivingPointSnapshot,
    ResourceKind,
    RoutingLimits,
    RoutingProblem,
    RoutingStop,
    StopKind,
    TimeWindow,
    VehicleSnapshot,
)
from tirodhan.modules.planning.routing.osrm import OSRMMatrixProvider, RoadNode
from tirodhan.modules.planning.routing.validation import validate_proposal
from tirodhan.modules.routing_operations.economics import EconomicPolicy, insertion_search, measure
from tirodhan.modules.routing_operations.matrix_cache import JobMatrixCache


def uid(i):
    return UUID(int=i)


def build(matrix, vehicles, requests):
    lots, stops, fleet = [], [], []
    for i in range(vehicles):
        cargo = ()
        if i % 4 == 0:
            lot = uid(4000 + i)
            lots.append(CargoLot(lot, lot, 1, "whole-lot", (1000, 1000), (uid(1),), 12000, None))
            cargo = (CargoBalance(lot, 1),)
            stops.append(
                RoutingStop(
                    uid(8000 + i),
                    uid(1),
                    StopKind.DISCHARGE,
                    lot,
                    1,
                    15,
                    TimeWindow(0, 12000),
                    True,
                    (uid(1000 + i),),
                    uid(1000 + i),
                    uid(1),
                )
            )
        fleet.append(
            VehicleSnapshot(
                uid(1000 + i),
                uid(2000 + i),
                1,
                ResourceKind.COMMITTED,
                uid(3000 + i),
                matrix.matrix_id,
                uid(2 + i),
                uid(1),
                0,
                TimeWindow(0, 14400),
                (12000, 12000),
                cargo,
                (),
                (TimeWindow(1200, 1320),),
            )
        )
    for i in range(requests):
        lot, pickup, discharge = uid(5000 + i), uid(6000 + i), uid(7000 + i)
        lots.append(CargoLot(lot, lot, 1, "whole-lot", (1000, 1000), (uid(1),), 12000, pickup))
        window = 600 + (i % 3) * 1800
        eligible = tuple(v.vehicle_id for v in fleet)
        stops.extend(
            (
                RoutingStop(
                    pickup,
                    uid(2 + vehicles + i),
                    StopKind.PICKUP,
                    lot,
                    1,
                    90,
                    TimeWindow(window, window + 1800),
                    False,
                    eligible,
                    None,
                    None,
                ),
                RoutingStop(
                    discharge,
                    uid(1),
                    StopKind.DISCHARGE,
                    lot,
                    1,
                    15,
                    TimeWindow(0, 12000),
                    False,
                    eligible,
                    None,
                    uid(1),
                ),
            )
        )
    return RoutingProblem(
        uid(9000),
        uid(9001),
        ROUTING_CONTRACT_V2,
        "PUBLIC_SYNTHETIC_7",
        datetime(2026, 10, 14, tzinfo=timezone.utc),
        ("grams", "milliliters"),
        RoutingLimits(512, 300, 32, 8),
        (matrix,),
        tuple(fleet),
        (ReceivingPointSnapshot(uid(1), uid(1), 1, True, (TimeWindow(0, 14400),)),),
        tuple(lots),
        tuple(stops),
    )


async def main():
    results = []
    economics = EconomicPolicy(
        version="synthetic-7",
        additional_vehicle_seconds=300,
        disruption_seconds=10,
        waiting_weight=0,
        window_risk_buffer_seconds=300,
        window_risk_weight=1,
        max_candidate_evaluations=300,
    )
    async with httpx.AsyncClient(trust_env=False) as client:
        source = OSRMMatrixProvider(
            client=client,
            endpoint=os.environ["TIRODHAN_OSRM_TEST_URL"],
            graph_version=os.environ["TIRODHAN_OSRM_GRAPH_VERSION"],
            profile_version="osrm-6.0-car",
            max_nodes=512,
            max_snap_distance_m=300,
            max_response_bytes=16000000,
        )
        for nvehicle, nrequest, repetitions in ((4, 8, 10), (8, 32, 5), (16, 64, 3), (32, 128, 3)):
            nodes = tuple(
                RoadNode(
                    uid(i + 1),
                    (77.207, 77.208, 77.210, 77.211)[i % 4],
                    (28.632, 28.631, 28.631, 28.630)[i % 4],
                )
                for i in range(1 + nvehicle + nrequest)
            )
            cache = JobMatrixCache(source, ttl_seconds=60)
            start = time.perf_counter()
            matrix = await cache.acquire(nodes)
            matrix_seconds = time.perf_counter() - start
            started = time.perf_counter()
            await cache.acquire(nodes)
            cache_seconds = time.perf_counter() - started
            problem = build(matrix, nvehicle, nrequest)
            samples = []
            for _sample in range(repetitions):
                begun = time.perf_counter()
                entries = []
                for name, engine in (("VROOM", VroomOptimizer()), ("OR_TOOLS", ORToolsOptimizer())):
                    before = resource.getrusage(resource.RUSAGE_CHILDREN)
                    started = time.perf_counter()
                    try:
                        proposal = await engine.propose(problem, search_seconds=1)
                        native_seconds = time.perf_counter() - started
                        validate_proposal(problem, proposal)
                        started = time.perf_counter()
                        improved, cost, evaluations = insertion_search(
                            problem, proposal, economics, {}
                        )
                        entries.append(
                            dict(
                                engine=name,
                                native_seconds=native_seconds,
                                refinement_seconds=time.perf_counter() - started,
                                evaluations=evaluations,
                                native=asdict(measure(problem, proposal, economics, {})),
                                refined=asdict(cost),
                            )
                        )
                    except Exception as error:
                        entries.append(
                            dict(
                                engine=name,
                                failure_type=type(error).__name__,
                                failure_code=str(error),
                                native_seconds=time.perf_counter() - started,
                            )
                        )
                    after = resource.getrusage(resource.RUSAGE_CHILDREN)
                    entries[-1].update(
                        child_cpu_seconds=(
                            after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime
                        ),
                        cumulative_child_max_rss_kib=after.ru_maxrss,
                    )
                samples.append(dict(wall_seconds=time.perf_counter() - begun, engines=entries))
            results.append(
                dict(
                    vehicles=nvehicle,
                    requests=nrequest,
                    samples=samples,
                    matrix_seconds=matrix_seconds,
                    cache_seconds=cache_seconds,
                    matrix_hits=cache.hits,
                    matrix_misses=cache.misses,
                )
            )
        nodes = tuple(
            RoadNode(
                uid(20000 + i),
                (77.207, 77.208, 77.210, 77.211)[i % 4],
                (28.632, 28.631, 28.631, 28.630)[i % 4],
            )
            for i in range(512)
        )
        for concurrency in (1, 2, 4):
            start = time.perf_counter()
            await asyncio.gather(*(source.acquire(nodes) for _ in range(concurrency)))
            results.append(
                dict(
                    matrix_nodes=512,
                    concurrency=concurrency,
                    wall_seconds=time.perf_counter() - start,
                )
            )
    print(
        json.dumps(
            dict(
                fixture_only=True,
                search_seconds=1,
                results=results,
                parent_max_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            )
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
