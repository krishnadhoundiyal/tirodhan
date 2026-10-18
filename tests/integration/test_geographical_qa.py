"""Independent QA instrumentation; never imported by the application.

Explicit invocation is mandatory: missing graphs/engines/database fail, not skip.
The original benchmark service/payment workflow is reused unchanged.
"""

import asyncio
import json
import math
import os
import resource
import time
from collections import Counter
from pathlib import Path

import h3
import pytest
import test_commercial_admission_benchmark as benchmark
from geoalchemy2.elements import WKTElement
from sqlalchemy import event, select, text

from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.planning.routing import scheduling, validation
from tirodhan.modules.routing_operations import admission, admission_roads, admission_store
from tirodhan.modules.routing_operations.orchestration_models import (
    CapacityAdmissionRequest,
    CapacityReservation,
    RegionCell,
)
from tirodhan.modules.serviceability.models import ServiceabilityContext

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("TIRODHAN_GEO_QA") != "1",
        reason="explicit geographical QA campaign (runner fails missing dependencies)",
    ),
]
FIXTURE = Path(__file__).parents[2] / "docs/benchmarks/GEOGRAPHICAL_QA_FIXTURE.json"


@pytest.fixture(autouse=True)
def mandatory_dependencies():
    import shutil

    assert os.environ.get("TIRODHAN_TEST_DATABASE_URL"), "Mandatory PostgreSQL missing"
    assert os.environ.get("TIRODHAN_OSRM_TEST_URL"), "Mandatory OSRM missing"
    assert shutil.which("vroom"), "Mandatory VROOM missing"


def quantiles(values):
    ordered = sorted(values)
    return {f"p{p}": ordered[max(0, math.ceil(p / 100 * len(ordered)) - 1)] for p in (50, 95, 99)}


async def verify_source_coverage(session, reservation, problem):
    """DB expectations independent of snapshot/projection and proof sharing.

    These campaigns have no cancelled/expired holds or handed-over cargo.
    Operational custody/expired inputs have separate execution tests.
    """
    rows = (
        await session.execute(
            select(
                CapacityReservation.reservation_id,
                CapacityReservation.request_id,
                CollectionRequest.status,
            )
            .outerjoin(
                CollectionRequest, CollectionRequest.request_id == CapacityReservation.request_id
            )
            .where(CapacityReservation.region_id == reservation.region_id)
        )
    ).all()
    expected = {booked if status == "ACCEPTED" else reserved for reserved, booked, status in rows}
    represented = {lot.collection_request_id for lot in problem.lots}
    assert expected <= represented and reservation.reservation_id in represented
    return len(expected)


def simple_oracle(problem, proposal):
    """Separate integer simulator for break-free QA routes, including custody/load.

    It does not call scheduling summaries, shared proofs or feasibility-gate helpers.
    The full independent gate is additionally run for all contract constraints.
    """
    stops = {s.stop_id: s for s in problem.stops}
    lots = {lot.lot_id: lot for lot in problem.lots}
    vehicles = {v.vehicle_id: v for v in problem.vehicles}
    visited = []
    for route in proposal.routes:
        vehicle = vehicles[route.vehicle_id]
        assert not vehicle.breaks, "Oracle campaign must be break-free"
        matrix = next(m for m in problem.matrices if m.matrix_id == vehicle.matrix_id)
        index = {node: i for i, node in enumerate(matrix.node_ids)}
        balance = {c.lot_id: c.quantity for c in vehicle.initial_cargo}
        node, at = vehicle.start_node_id, max(vehicle.available_at, vehicle.shift.start)
        for identity in route.stop_ids:
            stop = stops[identity]
            seconds, distance = matrix.arc(node, stop.node_id, index)
            assert seconds is not None and distance is not None and distance >= 0
            at = max(at + seconds, stop.service_start_window.start)
            assert at <= stop.service_start_window.end
            assert vehicle.vehicle_id in stop.eligible_vehicle_ids
            assert stop.bound_vehicle_id in (None, vehicle.vehicle_id)
            if stop.kind.value == "PICKUP":
                balance[stop.lot_id] = balance.get(stop.lot_id, 0) + stop.quantity
            else:
                assert stop.receiving_point_id in lots[stop.lot_id].authorized_receiving_point_ids
                assert balance.get(stop.lot_id, 0) >= stop.quantity
                balance[stop.lot_id] -= stop.quantity
            at += stop.service_seconds
            if stop.kind.value != "PICKUP":
                assert at <= lots[stop.lot_id].discharge_deadline
            load = [
                sum(lots[key].unit_load[d] * q for key, q in balance.items())
                for d in range(len(vehicle.capacity))
            ]
            assert all(0 <= q <= cap for q, cap in zip(load, vehicle.capacity, strict=True))
            node = stop.node_id
            visited.append(identity)
        seconds, distance = matrix.arc(node, vehicle.end_node_id, index)
        assert seconds is not None and distance is not None
        assert at + seconds <= vehicle.shift.end
        assert not any(balance.values())
    assert len(visited) == len(set(visited))
    assert {s.stop_id for s in problem.stops if s.required} <= set(visited)


async def test_geographical_commercial_pipeline(database_session_factory, monkeypatch):
    assert os.environ.get("TIRODHAN_TEST_DATABASE_URL"), "Mandatory disposable PostgreSQL missing"
    assert os.environ.get("TIRODHAN_OSRM_TEST_URL"), "Mandatory real OSRM missing"
    import ortools

    assert ortools.__version__ == "9.14.6206"
    assert FIXTURE.is_file(), "Run geographical_preflight.py first"
    data = json.loads(FIXTURE.read_text())
    campaign = os.environ["TIRODHAN_GEO_CAMPAIGN"]
    sites = data["sites"][campaign]
    count = {"A": 64, "B": 128, "C": 256}.get(campaign, 512)
    assert data["campaigns"][campaign]["independent_witness"]["valid"], "No feasible witness"
    monkeypatch.setenv("TIRODHAN_ADMISSION_COUNT", str(count))
    monkeypatch.setenv("TIRODHAN_ADMISSION_EXPECT_ALL", "0")
    monkeypatch.setenv("TIRODHAN_ADMISSION_BENCHMARK", "1")
    monkeypatch.setenv("TIRODHAN_ADMISSION_VEHICLES", "80")
    monkeypatch.setenv("TIRODHAN_ADMISSION_CONCURRENCY", "4")
    metrics = Counter(
        native_vroom_calls=0,
        native_ortools_calls=0,
        provider_failures=0,
        database_sqlstate_40001=0,
        database_sqlstate_40P01=0,
    )
    metrics["serialization_seconds"] = 0.0
    bind = database_session_factory.kw["bind"]

    def database_error(context):
        code = getattr(context.original_exception, "sqlstate", None)
        if code in ("40001", "40P01"):
            metrics["database_sqlstate_" + code] += 1

    event.listen(bind.sync_engine, "handle_error", database_error)
    for codec in (admission_store.PROOF_CODEC, admission_store.PROPOSAL_CODEC):
        original_dump = codec.dump_python

        def dump(*args, _original=original_dump, **kwargs):
            started = time.perf_counter()
            try:
                return _original(*args, **kwargs)
            finally:
                metrics["serialization_calls"] += 1
                metrics["serialization_seconds"] += time.perf_counter() - started

        monkeypatch.setattr(codec, "dump_python", dump)
    provider_latency, validations = [], []
    verified = set()
    original_population = benchmark.population

    async def population(factory, count, **kwargs):
        inputs, now = await original_population(factory, count, **kwargs)
        async with factory() as session, session.begin():
            for i, (_, command) in enumerate(inputs):
                site = sites[(i * 17) % len(sites)]
                cell = h3.latlng_to_cell(site["latitude"], site["longitude"], 7)
                region = (await session.scalars(select(RegionCell))).first().region_id
                if await session.get(RegionCell, cell) is None:
                    session.add(RegionCell(cell_id=cell, region_id=region))
                context = await session.get(ServiceabilityContext, command.context_id)
                context.location = WKTElement(
                    f"POINT({site['longitude']} {site['latitude']})", srid=4326
                )
                context.cell_id = cell
        return inputs, now

    monkeypatch.setattr(benchmark, "population", population)
    monkeypatch.setattr(benchmark, "quantiles", quantiles)
    original_acquire = benchmark.OSRMMatrixProvider.acquire

    async def acquire(self, nodes):
        stamp = time.perf_counter()
        metrics["provider_requests"] += 1
        try:
            return await original_acquire(self, nodes)
        except Exception as error:
            metrics["provider_failures"] += 1
            metrics["provider_failure_" + getattr(error, "code", type(error).__name__)] += 1
            raise
        finally:
            provider_latency.append(time.perf_counter() - stamp)

    monkeypatch.setattr(benchmark.OSRMMatrixProvider, "acquire", acquire)
    original_roads = admission_roads.DirectedRoadCache.acquire_arcs

    async def roads(self, nodes, pairs):
        coordinates = {n.node_id: (n.longitude, n.latitude) for n in nodes}
        metrics["requested_arcs"] += len(pairs)
        metrics["cache_misses_at_call"] += sum(
            (coordinates[a], coordinates[b]) not in self.entries for a, b in pairs
        )
        return await original_roads(self, nodes, pairs)

    monkeypatch.setattr(admission_roads.DirectedRoadCache, "acquire_arcs", roads)
    original_insert = scheduling.RouteSchedule.insert

    def insert(self, *args, **kwargs):
        result = original_insert(self, *args, **kwargs)
        metrics["insertion_pairs"] += result.evaluations
        metrics["prunes"] += result.prefix_prunes
        return result

    monkeypatch.setattr(scheduling.RouteSchedule, "insert", insert)
    original_assess = validation._validate_routes

    def assess(*args, **kwargs):
        metrics["complete_validator_calls"] += 1
        return original_assess(*args, **kwargs)

    monkeypatch.setattr(validation, "_validate_routes", assess)
    for engine, label in (
        (benchmark.VroomOptimizer, "vroom"),
        (benchmark.ORToolsOptimizer, "ortools"),
    ):
        original = engine.propose

        async def optimize(self, *args, _original=original, _label=label, **kwargs):
            metrics["native_" + _label + "_calls"] += 1
            return await _original(self, *args, **kwargs)

        monkeypatch.setattr(engine, "propose", optimize)
    original_persist = admission.persist_certificate

    async def persist(session, reservation, problem, proposal, proofs, sources, stamp):
        await original_persist(session, reservation, problem, proposal, proofs, sources, stamp)
        begun = time.perf_counter()
        # Independently reconstruct relational history, rather than trusting compose's result.
        restored = await admission_store.load_certificate(session, reservation)
        assert restored == proposal
        validation.validate_proposal(problem, restored)
        simple_oracle(problem, restored)
        await verify_source_coverage(session, reservation, problem)
        metrics["source_coverage_checks"] += 1
        for vehicle in sources["vehicles"]:
            assert vehicle["has_role"] and vehicle["user_status"] == "ACTIVE"
            assert vehicle["rider_status"] == "ACTIVE"
        metrics["certificates_independently_verified"] += 1
        verified.add(reservation.reservation_id)
        validations.append(time.perf_counter() - begun)

    monkeypatch.setattr(admission, "persist_certificate", persist)
    results = []

    def result_print(line):
        if line.startswith("COMMERCIAL_ADMISSION_RESULT="):
            results.append(json.loads(line.split("=", 1)[1]))

    monkeypatch.setattr(benchmark, "print", result_print, raising=False)
    memory = dict(database_bytes=0, child_peak_rss_bytes=0, parent_peak_rss_bytes=0)
    queue = dict(max_pending=0, samples=[])
    stop = asyncio.Event()

    async def sample_memory():
        while not stop.is_set():
            memory["parent_peak_rss_bytes"] = (
                resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
            )
            memory["child_peak_rss_bytes"] = (
                resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss * 1024
            )
            async with database_session_factory() as session:
                value = await session.scalar(
                    text(
                        "SELECT coalesce(pg_read_file('/sys/fs/cgroup/memory.current',0,8192,true),"
                        "pg_read_file('/sys/fs/cgroup/memory/memory.usage_in_bytes',0,8192,true))"
                    )
                )
                memory["database_bytes"] = max(memory["database_bytes"], int(value))
                pending = await session.scalar(
                    text(
                        "SELECT count(*) FROM capacity_admission_request "
                        "WHERE status IN ('PENDING','RUNNING')"
                    )
                )
                queue["max_pending"] = max(queue["max_pending"], pending)
                queue["samples"].append([time.monotonic(), pending])
            try:
                await asyncio.wait_for(stop.wait(), 1)
            except TimeoutError:
                pass

    sampler = asyncio.create_task(sample_memory())
    try:
        await benchmark.test_real_commercial_workflow(database_session_factory)
    finally:
        stop.set()
        await sampler
        event.remove(bind.sync_engine, "handle_error", database_error)
    result = results[0]
    async with database_session_factory() as session:
        rows = list(await session.scalars(select(CapacityAdmissionRequest)))
    result.update(
        campaign=campaign,
        geography=data["campaigns"][campaign],
        metrics=dict(metrics),
        provider_seconds=quantiles(provider_latency) if provider_latency else None,
        independent_validation_seconds=sum(validations),
        memory=memory,
        queue=queue,
        unknown=sum(r.status == "PENDING" and r.failure_code is not None for r in rows),
        expired=sum(r.status == "EXPIRED" for r in rows),
        retried=sum(r.attempts > 1 for r in rows),
        total_attempts=sum(r.attempts for r in rows),
        measurement="QA instrumentation and independent validation included in wall time",
    )
    output = os.environ.get("TIRODHAN_GEO_OUTPUT")
    if output:
        await asyncio.to_thread(
            Path(output).write_text, json.dumps(result, indent=2, sort_keys=True) + "\n"
        )
    console = dict(
        result,
        geography={
            key: value
            for key, value in result["geography"].items()
            if key not in ("bounds", "invalid_examples", "independent_witness")
        },
    )
    print("GEOGRAPHICAL_QA_RESULT=" + json.dumps(console, sort_keys=True))
    successful = {r.admission_id for r in rows if r.status == "SUCCEEDED"}
    assert successful <= verified and len(successful) == result["accepted"]
    assert result["accepted"] == count, result["failures"]
