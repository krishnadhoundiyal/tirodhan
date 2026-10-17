"""Opt-in real admission -> certificate -> checkout -> authenticated test capture.

No accepted bookings or capacity certificates are bulk seeded. Only synthetic identity,
resolved serviceability and declared future operating capacity are fixture inputs.
"""

import asyncio
import json
import math
import os
import time
from collections import Counter
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import h3
import httpx
import pytest
from geoalchemy2.elements import WKTElement
from sqlalchemy import event, func, insert, select, text
from test_collection_payment import (
    ITEMS,
    FixedPricing,
    create_ready_attempt,
    success_event,
)
from test_operational_orchestration import regional

from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.collection_requests.service import (
    CreateCollectionRequestCommand,
    create_collection_request,
)
from tirodhan.modules.dispatch.models import RiderAvailability, RiderProfile
from tirodhan.modules.identity.models import AppUser, UserRole
from tirodhan.modules.payments.service import process_authenticated_payment_event
from tirodhan.modules.planning.routing.adapters import ORToolsOptimizer, VroomOptimizer
from tirodhan.modules.planning.routing.contracts import OptimizerEngine
from tirodhan.modules.planning.routing.osrm import OSRMMatrixProvider
from tirodhan.modules.routing_operations.admission import ReserveCapacity
from tirodhan.modules.routing_operations.admission_jobs import request_admission, run_admissions
from tirodhan.modules.routing_operations.commands import (
    PositionReport,
    ShiftOpen,
    VehicleCreate,
    WindowDeclare,
)
from tirodhan.modules.routing_operations.fleet_models import FleetControl
from tirodhan.modules.routing_operations.orchestration_models import (
    CapacityAdmissionRequest,
    CapacityReservation,
    RegionCell,
    RegionVehicle,
)
from tirodhan.modules.serviceability.models import ServiceabilityContext

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("TIRODHAN_ADMISSION_BENCHMARK") != "1",
        reason="explicit real commercial admission benchmark opt-in",
    ),
]


def quantiles(values):
    ordered = sorted(values)
    return {f"p{p}": ordered[max(0, math.ceil(p / 100 * len(ordered)) - 1)] for p in (50, 95)}


async def population(factory, count, *, adopted=False):
    values = await regional(factory, empty=True)
    f, _, point, _, _, resource, clock, region_id, *_ = values
    now = max(utc_now(), clock[0])
    local = now.astimezone(ZoneInfo("Asia/Kolkata"))
    start = (local + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    await resource(
        WindowDeclare(
            client_command_id=new_uuid7(),
            receiving_point_id=point.receiving_point_id,
            starts_at=start - timedelta(hours=1),
            ends_at=start + timedelta(hours=12),
        )
    )
    riders = [new_uuid7() for _ in range(20)]
    async with factory() as session, session.begin():
        await session.execute(insert(AppUser), [dict(user_id=r, status="ACTIVE") for r in riders])
        await session.execute(
            insert(RiderProfile), [dict(rider_id=r, status="ACTIVE") for r in riders]
        )
        await session.execute(
            insert(UserRole), [dict(user_id=r, role_code="RIDER", granted_at=now) for r in riders]
        )
        await session.execute(
            insert(RiderAvailability),
            [
                dict(
                    rider_id=r,
                    availability_intent="AVAILABLE",
                    work_state="IDLE",
                    version=1,
                    updated_at=now,
                )
                for r in riders
            ],
        )
        control = await session.get(FleetControl, "COMMITTED_FLEET")
        control.configuration = dict(
            control.configuration, max_requests=128, max_nodes=256, max_vehicles=32
        )
        fleet_configuration = control.configuration
    if adopted:
        from tirodhan.modules.routing_operations.regional_planner import (
            RegionalPolicy,
            configure_region,
        )
        from tirodhan.modules.routing_operations.rolling import FleetPolicy

        await configure_region(
            factory,
            region_id=region_id,
            manager_id=f.manager_id,
            now=now,
            policy=RegionalPolicy(
                fleet=FleetPolicy.model_validate(fleet_configuration),
                processes=2,
                max_admitted=128,
                neighbourhood_size=8,
                capacity_multiplier=2,
                max_discovery=5000,
                max_adjustments=50,
                selection_seconds=2,
                starvation_seconds=600,
            ),
        )
    for rider in riders:
        vehicle = await resource(
            VehicleCreate(
                client_command_id=new_uuid7(),
                mass_capacity_g=10000,
                volume_capacity_ml=10000,
                profile_version="test-car",
                end_receiving_point_id=point.receiving_point_id,
            )
        )
        await resource(
            ShiftOpen(
                client_command_id=new_uuid7(),
                vehicle_id=vehicle,
                rider_id=rider,
                resource_kind="COMMITTED",
                commitment_id=new_uuid7(),
                starts_at=start - timedelta(minutes=15),
                ends_at=start + timedelta(hours=8),
            )
        )
        await resource(
            PositionReport(
                client_command_id=new_uuid7(),
                vehicle_id=vehicle,
                longitude=77.207,
                latitude=28.632,
                observed_at=clock[0],
            )
        )
        async with factory() as session, session.begin():
            session.add(RegionVehicle(region_id=region_id, vehicle_id=vehicle))
    public = json.loads(
        (Path(__file__).parents[1] / "routing/data/inc8-road-nodes.json").read_text()
    )
    inputs = []
    async with factory() as session, session.begin():
        for i in range(count):
            customer, context = new_uuid7(), new_uuid7()
            n = public[(i * 17) % len(public)]
            cell = h3.latlng_to_cell(n["latitude"], n["longitude"], 7)
            if await session.get(RegionCell, cell) is None:
                session.add(RegionCell(cell_id=cell, region_id=region_id))
            session.add(AppUser(user_id=customer, status="ACTIVE"))
            await session.flush()
            session.add(UserRole(user_id=customer, role_code="CUSTOMER", granted_at=now))
            session.add(
                ServiceabilityContext(
                    serviceability_context_id=context,
                    user_id=customer,
                    address_snapshot_encrypted=b"synthetic-public-fixture",
                    location=WKTElement(f"POINT({n['longitude']} {n['latitude']})", srid=4326),
                    cell_id=cell,
                    status="SERVICEABLE",
                    created_at=now,
                    expires_at=now + timedelta(hours=2),
                )
            )
            # 70% clustered first two slots, remainder spread across the next four hours.
            slot = start + timedelta(minutes=30 * (i % 2 if i % 10 < 7 else 2 + i % 6))
            inputs.append(
                (
                    customer,
                    ReserveCapacity(
                        client_command_id=new_uuid7(),
                        context_id=context,
                        slot_start=slot,
                        slot_end=slot + timedelta(minutes=30),
                        mass_g=1000 + (i % 4) * 500,
                        volume_ml=1000 + (i % 3) * 500,
                        pickup_seconds=180,
                        discharge_seconds=60,
                    ),
                )
            )
    return inputs, now


async def purchase(factory, customer, command, now):
    result = await create_collection_request(
        factory,
        CreateCollectionRequestCommand(
            customer_id=customer,
            client_request_id=new_uuid7(),
            serviceability_context_id=command.context_id,
            slot_start=command.slot_start,
            slot_end=command.slot_end,
            items=ITEMS,
            payment_expires_at=now + timedelta(minutes=30),
            capacity_reservation_id=command.client_command_id,
        ),
        FixedPricing(),
        idempotency_expires_at=now + timedelta(hours=2),
        require_capacity_reservation=True,
        planning_lead_time_minutes=15,
    )
    attempt, _ = await create_ready_attempt(
        factory,
        customer,
        result.request.request_id,
        key=str(new_uuid7()),
        order_id=str(new_uuid7()),
    )
    await process_authenticated_payment_event(
        factory,
        success_event(attempt, str(new_uuid7())),
        payload_hash=b"synthetic-admission-capture",
        planning_lead_time_minutes=15,
    )
    return result


async def test_real_commercial_workflow(database_session_factory):
    factory = database_session_factory
    count = int(os.getenv("TIRODHAN_ADMISSION_COUNT", "48"))
    concurrency = int(os.getenv("TIRODHAN_ADMISSION_CONCURRENCY", "1"))
    adopted = os.getenv("TIRODHAN_ADMISSION_ADOPTED") == "1"
    inputs, now = await population(factory, count, adopted=adopted)
    interleaved = os.getenv("TIRODHAN_ADMISSION_INTERLEAVED") == "1"
    by_id = {c.client_command_id: (customer, c) for customer, c in inputs}
    bought = set()
    timings, enqueue, checkout = [], [], []
    submitted, completed = {}, {}
    sql = {"calls": 0, "seconds": 0.0, "max_seconds": 0.0}
    bind = factory.kw["bind"]

    def before(conn, cursor, statement, parameters, context, executemany):
        context._admission_started = time.perf_counter()

    def after(conn, cursor, statement, parameters, context, executemany):
        seconds = time.perf_counter() - context._admission_started
        sql["calls"] += 1
        sql["seconds"] += seconds
        sql["max_seconds"] = max(sql["max_seconds"], seconds)

    event.listen(bind.sync_engine, "before_cursor_execute", before)
    event.listen(bind.sync_engine, "after_cursor_execute", after)
    begun = time.perf_counter()
    for customer, command in inputs:
        stamp = time.perf_counter()
        submitted[command.client_command_id] = stamp
        await request_admission(
            factory,
            customer_id=customer,
            command=command,
            expires_at=now + timedelta(hours=1),
            now=now,
        )
        enqueue.append(time.perf_counter() - stamp)
    async with httpx.AsyncClient(trust_env=False) as client:
        provider = OSRMMatrixProvider(
            client=client,
            endpoint=os.environ["TIRODHAN_OSRM_TEST_URL"],
            graph_version="fixture-car",
            profile_version="test-car",
            max_nodes=256,
            max_snap_distance_m=300,
        )
        solvers = {
            OptimizerEngine.VROOM: VroomOptimizer(),
            OptimizerEngine.OR_TOOLS: ORToolsOptimizer(),
        }
        deadline = time.perf_counter() + int(os.getenv("TIRODHAN_ADMISSION_BUDGET", "300"))

        async def consume():
            while time.perf_counter() < deadline:
                stamp = time.perf_counter()
                processed = await run_admissions(
                    factory,
                    providers={"test-car": provider},
                    optimizers=solvers,
                    limit=1,
                    clock=lambda: max(now, utc_now()),
                )
                if not processed:
                    async with factory() as session:
                        remaining = await session.scalar(
                            select(func.count())
                            .select_from(CapacityAdmissionRequest)
                            .where(CapacityAdmissionRequest.status.in_(("PENDING", "RUNNING")))
                        )
                    if not remaining:
                        break
                    await asyncio.sleep(0.25)
                    continue
                timings.append(time.perf_counter() - stamp)
                async with factory() as session:
                    terminal = await session.scalars(
                        select(CapacityAdmissionRequest.admission_id).where(
                            CapacityAdmissionRequest.status.in_(
                                ("SUCCEEDED", "FAILED", "NEEDS_ACCEPTANCE")
                            )
                        )
                    )
                    for identity in terminal:
                        if identity not in completed:
                            completed[identity] = time.perf_counter() - submitted[identity]
                if interleaved:
                    async with factory() as session:
                        successes = list(
                            await session.scalars(
                                select(CapacityAdmissionRequest.admission_id).where(
                                    CapacityAdmissionRequest.status == "SUCCEEDED"
                                )
                            )
                        )
                    for identity in successes:
                        if identity not in bought:
                            bought.add(identity)  # Test-process ownership before awaiting.
                            stamp = time.perf_counter()
                            await purchase(factory, *by_id[identity], now)
                            checkout.append(time.perf_counter() - stamp)

        await asyncio.gather(*(consume() for _ in range(concurrency)))
    statuses, failures = Counter(), Counter()
    accepted = 0
    for customer, command in inputs:
        async with factory() as session:
            row = await session.get(CapacityAdmissionRequest, command.client_command_id)
        statuses[row.status] += 1
        if row.failure_code:
            failures[row.failure_code] += 1
        if row.status != "SUCCEEDED":
            continue
        stamp = time.perf_counter()
        if command.client_command_id not in bought:
            await purchase(factory, customer, command, now)
            checkout.append(time.perf_counter() - stamp)
        accepted += 1
    elapsed = time.perf_counter() - begun
    assert accepted > 0, dict(failures)
    event.remove(bind.sync_engine, "before_cursor_execute", before)
    event.remove(bind.sync_engine, "after_cursor_execute", after)
    async with factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(CollectionRequest)
                .where(CollectionRequest.status == "ACCEPTED")
            )
            == accepted
        )
        assert (
            await session.scalar(select(func.count()).select_from(CapacityReservation)) == accepted
        )
        db = (
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
    result = dict(
        count=count,
        concurrency=concurrency,
        interleaved=interleaved,
        adopted_policy=adopted,
        elapsed_seconds=elapsed,
        throughput_per_second=len(completed) / elapsed,
        decision_seconds=quantiles(list(completed.values())) if completed else None,
        enqueue_seconds=quantiles(enqueue),
        certification_attempt_seconds=quantiles(timings),
        checkout_capture_seconds=quantiles(checkout) if checkout else None,
        statuses=dict(statuses),
        failures=dict(failures),
        accepted=accepted,
        sql=sql,
        database=dict(db),
    )
    print("COMMERCIAL_ADMISSION_RESULT=" + json.dumps(result, sort_keys=True))
