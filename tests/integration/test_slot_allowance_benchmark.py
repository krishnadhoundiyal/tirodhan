"""Opt-in projection -> hold -> real checkout/capture benchmark; no accepted work seeded."""

import asyncio
import json
import os
import resource
import time
from collections import Counter
from datetime import timedelta

import pytest
from sqlalchemy import event, func, select, text
from test_commercial_admission_benchmark import (
    application_cpu_usec,
    cpu_usage_usec,
    population,
    purchase,
    quantiles,
)
from test_operational_orchestration import operational
from test_slot_holds import policy

from tirodhan.db.values import new_uuid7
from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.planning.routing.adapters import ORToolsOptimizer, VroomOptimizer
from tirodhan.modules.planning.routing.osrm import OSRMMatrixProvider
from tirodhan.modules.routing_operations import admission
from tirodhan.modules.routing_operations.capacity_models import RegionalSlotProjection
from tirodhan.modules.routing_operations.commands import OperationalInputError
from tirodhan.modules.routing_operations.operational_commands import ConfigureSlotAllowance
from tirodhan.modules.routing_operations.orchestration_models import (
    CapacityReservation,
    OperatingRegion,
)
from tirodhan.modules.routing_operations.slot_holds import HoldSlot, hold_slot
from tirodhan.modules.routing_operations.slot_projection import read_slots, refresh_slots

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("TIRODHAN_SLOT_ALLOWANCE_BENCHMARK") != "1",
        reason="explicit disposable projection/hold benchmark opt-in",
    ),
]


async def database_metrics(factory):
    async with factory() as session:
        counters = dict(
            (
                await session.execute(
                    text(
                        "SELECT xact_commit,xact_rollback,blks_read,blks_hit,temp_bytes,deadlocks "
                        "FROM pg_stat_database WHERE datname=current_database()"
                    )
                )
            )
            .mappings()
            .one()
        )
        # Test-only superuser instrumentation; the application needs no file privileges.
        counters["container_cpu_usec"] = cpu_usage_usec(
            await session.scalar(
                text(
                    "SELECT coalesce(pg_read_file('/sys/fs/cgroup/cpu.stat',0,8192,true),"
                    "pg_read_file('/sys/fs/cgroup/cpuacct/cpuacct.usage',0,8192,true))"
                )
            )
        )
        counters["memory_bytes"] = int(
            await session.scalar(
                text(
                    "SELECT coalesce(pg_read_file('/sys/fs/cgroup/memory.current',0,8192,true),"
                    "pg_read_file('/sys/fs/cgroup/memory/memory.usage_in_bytes',0,8192,true))"
                )
            )
        )
        return counters


async def test_projected_commercial_workflow(database_session_factory, monkeypatch):
    factory = database_session_factory
    count = int(os.getenv("TIRODHAN_ADMISSION_COUNT", "192"))
    concurrency = int(os.getenv("TIRODHAN_ADMISSION_CONCURRENCY", "1"))
    inputs, now = await population(factory, count)
    async with factory() as session:
        region = await session.scalar(select(OperatingRegion))
    await operational(
        factory,
        region.recorded_by,
        ConfigureSlotAllowance(
            client_command_id=new_uuid7(),
            region_id=region.region_id,
            expected_version=region.version,
            policy=policy(),
        ),
        now,
    )

    async def forbidden(*args, **kwargs):
        pytest.fail("projection/hold/checkout invoked a retired feasibility dependency")

    monkeypatch.setattr(admission, "reserve_capacity", forbidden)
    monkeypatch.setattr(OSRMMatrixProvider, "acquire", forbidden)
    monkeypatch.setattr(VroomOptimizer, "propose", forbidden)
    monkeypatch.setattr(ORToolsOptimizer, "propose", forbidden)
    sql = {"calls": 0, "seconds": 0.0, "max_seconds": 0.0}
    bind = factory.kw["bind"]

    def before(conn, cursor, statement, parameters, context, executemany):
        context._allowance_started = time.perf_counter()

    def after(conn, cursor, statement, parameters, context, executemany):
        elapsed = time.perf_counter() - context._allowance_started
        sql["calls"] += 1
        sql["seconds"] += elapsed
        sql["max_seconds"] = max(sql["max_seconds"], elapsed)

    db_before = await database_metrics(factory)
    app_before = await application_cpu_usec()
    event.listen(bind.sync_engine, "before_cursor_execute", before)
    event.listen(bind.sync_engine, "after_cursor_execute", after)
    holds, checkout, decisions = [], [], []
    failures = Counter()
    successful = []
    started = time.perf_counter()
    try:
        stamp = time.perf_counter()
        size = await refresh_slots(factory, region.region_id, now=now)
        initial_refresh = time.perf_counter() - stamp
        async with factory() as session:
            generation = await session.scalar(select(RegionalSlotProjection.generation).limit(1))
        semaphore = asyncio.Semaphore(concurrency)

        async def claim(customer, original):
            submitted = time.perf_counter()
            async with semaphore:
                stamp = time.perf_counter()
                command = HoldSlot(**original.model_dump(), expected_generation=generation)
                try:
                    await hold_slot(
                        factory,
                        customer_id=customer,
                        command=command,
                        expires_at=now + timedelta(hours=1),
                        now=now,
                    )
                except OperationalInputError as error:
                    failures[str(error)] += 1
                else:
                    successful.append((customer, command))
                holds.append(time.perf_counter() - stamp)
                decisions.append(time.perf_counter() - submitted)

        await asyncio.gather(*(claim(*item) for item in inputs))
        hold_phase = time.perf_counter() - started
        # Match the prior non-interleaved benchmark: all holds, then real booking/payment.
        for customer, command in successful:
            stamp = time.perf_counter()
            await purchase(factory, customer, command, now)
            checkout.append(time.perf_counter() - stamp)
        elapsed = time.perf_counter() - started
    finally:
        event.remove(bind.sync_engine, "before_cursor_execute", before)
        event.remove(bind.sync_engine, "after_cursor_execute", after)
    db_after = await database_metrics(factory)
    app_cpu = await application_cpu_usec() - app_before
    db_delta = {k: db_after[k] - db_before[k] for k in db_before if k != "memory_bytes"}
    db_delta["cpu_fraction_of_two_cores"] = db_delta["container_cpu_usec"] / 1e6 / elapsed / 2
    db_delta["memory_bytes_at_end"] = db_after["memory_bytes"]
    async with factory() as session:
        accepted = await session.scalar(
            select(func.count())
            .select_from(CollectionRequest)
            .where(CollectionRequest.status == "ACCEPTED")
        )
        assert accepted == len(successful)
        assert (
            await session.scalar(select(func.count()).select_from(CapacityReservation)) == accepted
        )
    refresh_times, read_times = [], []
    for _ in range(30):
        stamp = time.perf_counter()
        await refresh_slots(factory, region.region_id, now=now)
        refresh_times.append(time.perf_counter() - stamp)
        stamp = time.perf_counter()
        async with factory() as session:
            await read_slots(session, region.region_id, now=now)
        read_times.append(time.perf_counter() - stamp)
    result = dict(
        count=count,
        accepted=accepted,
        concurrency=concurrency,
        vehicles=int(os.getenv("TIRODHAN_ADMISSION_VEHICLES", "20")),
        interleaved=False,
        elapsed_seconds=elapsed,
        hold_phase_seconds=hold_phase,
        throughput_per_second=accepted / elapsed,
        hold_throughput_per_second=accepted / hold_phase,
        initial_refresh_seconds=initial_refresh,
        hold_seconds=quantiles(holds),
        queued_decision_seconds=quantiles(decisions),
        checkout_capture_seconds=quantiles(checkout) if checkout else None,
        slots=size,
        refresh_seconds=quantiles(refresh_times),
        read_seconds=quantiles(read_times),
        failures=dict(failures),
        failure_fraction=sum(failures.values()) / count,
        sql=sql,
        database=db_delta,
        application=dict(
            container_cpu_usec=app_cpu,
            cpu_fraction_of_two_cores=app_cpu / 1e6 / elapsed / 2,
            peak_process_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
        ),
        road_matrix_calls=0,
        optimizer_calls=0,
        synthetic_policy=policy().model_dump(mode="json"),
    )
    print("PROJECTED_SLOT_ALLOWANCE_RESULT=" + json.dumps(result, sort_keys=True))
    assert accepted == count, result
