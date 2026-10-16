"""Opt-in real PostgreSQL + in-container roads + both native engines, 2000/20."""

import json
import os
import time
from datetime import timedelta
from pathlib import Path

import h3
import httpx
import pytest
from geoalchemy2.elements import WKTElement
from sqlalchemy import insert, select
from test_regional_parallel import prepared

from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.collection_requests.models import CollectionRequest, CollectionRequestItem
from tirodhan.modules.dispatch.models import RiderAvailability, RiderProfile
from tirodhan.modules.identity.models import AppUser, UserRole
from tirodhan.modules.payments.models import Payment, PaymentAttempt
from tirodhan.modules.planning.routing.osrm import OSRMMatrixProvider
from tirodhan.modules.routing_operations.commands import PositionReport, ShiftOpen, VehicleCreate
from tirodhan.modules.routing_operations.fleet_models import FleetControl
from tirodhan.modules.routing_operations.models import RoutingManifest
from tirodhan.modules.routing_operations.operational_commands import ConfirmShift
from tirodhan.modules.routing_operations.orchestration_models import (
    OperatingRegion,
    RegionCell,
    RegionVehicle,
)
from tirodhan.modules.routing_operations.parallel_models import PlanningOutcome
from tirodhan.modules.routing_operations.regional_matrix import RegionalMatrixCache
from tirodhan.modules.routing_operations.regional_planner import run_region
from tirodhan.modules.serviceability.models import ServiceabilityContext
from tirodhan.workers.routing_regional import LocalRoadProfile, road_processes

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.routing_engine,
    pytest.mark.skipif(
        os.getenv("TIRODHAN_REGIONAL_BENCHMARK") != "1",
        reason="explicit expensive 2000/20 benchmark opt-in",
    ),
]


async def populate(factory):
    from test_operational_orchestration import operational

    values, p = await prepared(factory)
    f, vehicles, point, ids, config, resource, clock, region_id, *_ = values
    async with factory() as session, session.begin():
        riders = [new_uuid7() for _ in range(18)]
        await session.execute(insert(AppUser), [dict(user_id=r, status="ACTIVE") for r in riders])
        await session.execute(
            insert(RiderProfile), [dict(rider_id=r, status="ACTIVE") for r in riders]
        )
        await session.execute(
            insert(UserRole),
            [dict(user_id=r, role_code="RIDER", granted_at=clock[0]) for r in riders],
        )
        await session.execute(
            insert(RiderAvailability),
            [
                dict(
                    rider_id=r,
                    availability_intent="AVAILABLE",
                    work_state="IDLE",
                    version=1,
                    updated_at=clock[0],
                )
                for r in riders
            ],
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
        shift = await resource(
            ShiftOpen(
                client_command_id=new_uuid7(),
                vehicle_id=vehicle,
                rider_id=rider,
                resource_kind="COMMITTED",
                commitment_id=new_uuid7(),
                starts_at=clock[0] - timedelta(seconds=1),
                ends_at=clock[0] + timedelta(hours=4),
            )
        )
        await operational(
            factory,
            rider,
            ConfirmShift(client_command_id=new_uuid7(), shift_id=shift),
            clock[0] - timedelta(seconds=2),
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
    async with factory() as session:
        original = await session.get(CollectionRequest, ids[0])
        region = await session.get(OperatingRegion, region_id)
        assert region is not None
        minimums = region.policy
    service_mode = os.getenv("TIRODHAN_BENCH_SERVICE", "synthetic_fast")
    pickup_seconds = (
        minimums["minimum_pickup_seconds"] if service_mode == "regional_minimum" else 30
    )
    discharge_seconds = (
        minimums["minimum_discharge_seconds"] if service_mode == "regional_minimum" else 30
    )
    contexts, bookings, items, manifests, payments, attempts = [], [], [], [], [], []
    public_nodes = json.loads(
        (Path(__file__).parents[1] / "routing" / "data" / "inc8-road-nodes.json").read_text()
    )
    cells = set()
    for i in range(1998):
        context, booking, payment, attempt = (new_uuid7() for _ in range(4))
        x, y = [(77.207, 28.632), (77.209, 28.632), (77.2105, 28.6305), (77.211, 28.630)][i % 4]
        if os.getenv("TIRODHAN_BENCH_COORDINATES", "dense") == "varied":
            public = public_nodes[i % len(public_nodes)]
            x, y = public["longitude"], public["latitude"]
        cell = h3.latlng_to_cell(y, x, 7)
        cells.add(cell)
        location = WKTElement(f"POINT({x} {y})", srid=4326)
        contexts.append(
            dict(
                serviceability_context_id=context,
                user_id=original.customer_id,
                address_snapshot_encrypted=original.pickup_address_snapshot_encrypted,
                location=location,
                cell_id=cell,
                status="SERVICEABLE",
                created_at=config.epoch,
                expires_at=config.horizon_end,
            )
        )
        bookings.append(
            dict(
                request_id=booking,
                client_request_id=booking,
                customer_id=original.customer_id,
                serviceability_context_id=context,
                pickup_address_snapshot_encrypted=original.pickup_address_snapshot_encrypted,
                pickup_location=location,
                cell_id=cell,
                slot_start=config.epoch + timedelta(minutes=30 + (i % 3) * 30),
                slot_end=config.epoch + timedelta(minutes=60 + (i % 3) * 30),
                quoted_amount_minor=500,
                currency="INR",
                status="ACCEPTED",
                created_at=config.epoch,
                payment_expires_at=config.epoch + timedelta(minutes=20),
                accepted_at=config.epoch,
            )
        )
        items.append(
            dict(
                request_id=booking,
                item_category_code="TEST",
                quoted_line_amount_minor=500,
                currency="INR",
            )
        )
        manifests.append(
            dict(
                request_id=booking,
                mass_g=2000,
                volume_ml=2000,
                pickup_seconds=pickup_seconds,
                discharge_seconds=discharge_seconds,
                discharge_deadline=config.epoch + timedelta(hours=3),
                receiving_point_id=point.receiving_point_id,
                recorded_by=f.manager_id,
                created_at=config.epoch,
            )
        )
        payments.append(
            dict(
                payment_id=payment,
                request_id=booking,
                amount_minor=500,
                currency="INR",
                status="SUCCEEDED",
                successful_attempt_id=attempt,
                created_at=config.epoch,
                succeeded_at=config.epoch,
            )
        )
        attempts.append(
            dict(
                payment_attempt_id=attempt,
                payment_id=payment,
                provider="fake",
                provider_idempotency_key=str(attempt),
                provider_payment_id=str(attempt),
                status="SUCCEEDED",
                created_at=config.epoch,
                completed_at=config.epoch,
            )
        )
    async with factory() as session, session.begin():
        for model, rows in [
            (ServiceabilityContext, contexts),
            (CollectionRequest, bookings),
            (CollectionRequestItem, items),
            (RoutingManifest, manifests),
            (Payment, payments),
            (PaymentAttempt, attempts),
        ]:
            await session.execute(insert(model), rows)
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        await session.execute(
            pg_insert(RegionCell)
            .values([dict(cell_id=cell, region_id=region_id) for cell in sorted(cells)])
            .on_conflict_do_nothing(index_elements=["cell_id"])
        )
        # Test fixture configuration only; production configuration remains insert-once.
        control = await session.get(FleetControl, f"REGION:{region_id}")
        p = p.model_copy(
            update={
                "processes": int(os.getenv("TIRODHAN_BENCH_PROCESSES", "2")),
                "max_admitted": int(os.getenv("TIRODHAN_BENCH_ADMITTED", "160")),
                "neighbourhood_size": 24,
                "max_discovery": 5000,
                "fleet": p.fleet.model_copy(
                    update={"max_vehicles": 32, "max_nodes": 512, "lease_seconds": 900}
                ),
            }
        )
        control.configuration = p.model_dump(mode="json")
    return values, p


@pytest.mark.parametrize("strategy", ["global", "zonal", "on_demand"])
@pytest.mark.parametrize("repeat", range(int(os.getenv("TIRODHAN_BENCH_REPEATS", "1"))))
async def test_complete_regional_2000_bookings_20_riders(
    database_session_factory, strategy, repeat
):
    factory = database_session_factory
    values, p = await populate(factory)
    profiles = [
        LocalRoadProfile(
            profile_version="test-car",
            graph_version="fixture-car",
            graph_path=Path("/graphs/car/map.osrm"),
            port=5011,
            threads=1,
            max_snap_distance_m=300,
            block_nodes=128,
            max_pairs=65536,
            matrix_strategy=strategy,
        )
    ]
    started = time.perf_counter()
    business_start = utc_now()
    import sys

    sys.path.insert(0, str(Path(__file__).parents[1] / "routing"))
    from regional_metrics import ProcessSampler

    observed = {}
    with ProcessSampler() as sampled, road_processes(profiles):
        startup = time.perf_counter() - started
        async with httpx.AsyncClient(trust_env=False) as client:
            cache = RegionalMatrixCache(
                OSRMMatrixProvider(
                    client=client,
                    endpoint="http://127.0.0.1:5011",
                    graph_version="fixture-car",
                    profile_version="test-car",
                    max_nodes=128,
                    max_snap_distance_m=300,
                ),
                profile="test-car",
                graph="fixture-car",
                block_nodes=128,
                max_pairs=65536,
                strategy=strategy,
            )
            cycle = await run_region(
                factory,
                region_id=values[7],
                providers={"test-car": cache},
                clock=lambda: business_start + timedelta(seconds=time.perf_counter() - started),
                observations=observed,
            )
            elapsed = time.perf_counter() - started
    assert cycle.status == "ACTIVATED"
    async with factory() as session:
        outcomes = list(
            await session.scalars(
                select(PlanningOutcome).where(PlanningOutcome.cycle_id == cycle.cycle_id)
            )
        )
    assert len(outcomes) == 2000
    assert len({o.request_id for o in outcomes}) == 2000
    assert any(o.outcome == "ASSIGNED" for o in outcomes)
    record = dict(
        bookings=2000,
        riders=20,
        processes=p.processes,
        strategy=strategy,
        total_seconds=elapsed,
        startup_seconds=startup,
        assigned=sum(o.outcome == "ASSIGNED" for o in outcomes),
        unresolved=sum(o.outcome == "DEFERRED" for o in outcomes),
        matrix_queries=cache.queries,
        matrix_hits=cache.hits,
        matrix_pairs=len(cache.entries),
        peak_rss_bytes=sampled.peak_rss_bytes,
        peak_cgroup_bytes=sampled.peak_cgroup_bytes,
        sampled_peak_cpu_cores=sampled.peak_cpu_cores,
        sampled_cpu_seconds=sampled.cpu_seconds,
        samples=sampled.samples,
        repeat=repeat,
        admitted=p.max_admitted,
        coordinates=os.getenv("TIRODHAN_BENCH_COORDINATES", "dense"),
        service_mode=os.getenv("TIRODHAN_BENCH_SERVICE", "synthetic_fast"),
        measurements=cycle.measurements,
    )
    # Controlled aggregate data only; no input identifiers, coordinates or addresses.
    record["measurements"] = {
        key: cycle.measurements[key] for key in ("timings", "worker_failures", "routing_metrics")
    }
    record["measurements"]["timings"] = observed
    print("INC8_REGIONAL_BENCHMARK", json.dumps(record))
