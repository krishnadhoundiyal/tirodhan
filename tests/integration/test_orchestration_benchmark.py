"""Opt-in end-to-end PostgreSQL/road/dual-native measurements; no production calibration."""

import os
import time
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import event
from test_operational_orchestration import admission, regional
from test_routing_dispatch import add_booking
from test_routing_fleet import policy

from tirodhan.db.values import new_uuid7
from tirodhan.modules.planning.routing.adapters import ORToolsOptimizer, VroomOptimizer
from tirodhan.modules.planning.routing.contracts import OptimizerEngine
from tirodhan.modules.planning.routing.osrm import OSRMMatrixProvider
from tirodhan.modules.routing_operations.admission import reserve_capacity
from tirodhan.modules.routing_operations.commands import PositionReport, ShiftOpen, VehicleCreate
from tirodhan.modules.routing_operations.orchestration_models import RegionVehicle
from tirodhan.modules.routing_operations.rolling import (
    ConfigureFleet,
    configure_fleet,
    run_fleet_cycle,
)
from tirodhan.modules.routing_operations.snapshot import capture_snapshot

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.routing_engine,
    pytest.mark.skipif(
        os.getenv("TIRODHAN_ORCHESTRATION_BENCHMARK") != "1",
        reason="expensive explicit benchmark opt-in",
    ),
]


@pytest.mark.parametrize("vehicles,demand", [(4, 8), (8, 32), (16, 64), (32, 127)])
async def test_end_to_end(database_session_factory, vehicles, demand):
    import json

    factory = database_session_factory
    f, initial, point, ids, config, resource, clock, region_id, customer, context = await regional(
        factory, second=True
    )
    from tirodhan.modules.dispatch.models import RiderAvailability, RiderProfile
    from tirodhan.modules.identity.models import AppUser, UserRole

    riders = []
    async with factory() as session, session.begin():
        for _ in range(vehicles - 2):
            user = AppUser(status="ACTIVE")
            session.add(user)
            await session.flush([user])
            session.add(RiderProfile(rider_id=user.user_id, status="ACTIVE"))
            await session.flush()
            session.add(
                RiderAvailability(
                    rider_id=user.user_id,
                    availability_intent="AVAILABLE",
                    work_state="IDLE",
                    version=1,
                    updated_at=clock[0],
                )
            )
            session.add(UserRole(user_id=user.user_id, role_code="RIDER", granted_at=clock[0]))
            riders.append(user.user_id)
    for rider in riders:
        vehicle = await resource(
            VehicleCreate(
                client_command_id=new_uuid7(),
                mass_capacity_g=30000,
                volume_capacity_ml=30000,
                profile_version="test-car",
                end_receiving_point_id=point.receiving_point_id,
            )
        )
        await resource(
            ShiftOpen(
                client_command_id=new_uuid7(),
                rider_id=rider,
                vehicle_id=vehicle,
                starts_at=clock[0],
                ends_at=clock[0] + timedelta(hours=4),
                resource_kind="COMMITTED",
                commitment_id=new_uuid7(),
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
    for _ in range(demand - 2):
        booking = await add_booking(factory, config, point, ids[0], f.manager_id, road=True)
        from tirodhan.modules.collection_requests.models import CollectionRequest

        async with factory() as session, session.begin():
            original = await session.get(CollectionRequest, ids[0])
            new = await session.get(CollectionRequest, booking)
            new.cell_id = original.cell_id
    p = policy(
        regional=True,
        planning_lead_seconds=600,
        max_vehicles=32,
        max_requests=128,
        max_nodes=512,
        lease_seconds=300,
        search_seconds=30 if vehicles == 32 else 1,
        economics=policy().economics.model_copy(update={"max_candidate_evaluations": 300}),
    )
    await configure_fleet(
        factory,
        manager_id=f.manager_id,
        command=ConfigureFleet(client_command_id=new_uuid7(), expected_version=2, policy=p),
        expires_at=clock[0] + timedelta(days=1),
        now=clock[0],
    )
    queries = [0]

    def count(*_):
        queries[0] += 1

    engine = factory.kw["bind"]
    event.listen(engine.sync_engine, "before_cursor_execute", count)
    try:
        snapshot_times = []
        start_queries = queries[0]
        for _ in range(7):
            started = time.perf_counter()
            snapshot = await capture_snapshot(factory, p.snapshot_configuration(clock[0]))
            assert len(snapshot.sources["requests"]) == demand
            snapshot_times.append(time.perf_counter() - started)
        snapshot_queries = queries[0] - start_queries
        async with httpx.AsyncClient(trust_env=False) as client:
            providers = {
                "test-car": OSRMMatrixProvider(
                    client=client,
                    endpoint=os.environ["TIRODHAN_OSRM_TEST_URL"],
                    graph_version="public-fixture",
                    profile_version="test-car",
                    max_nodes=512,
                    max_snap_distance_m=300,
                )
            }
            solvers = {
                OptimizerEngine.VROOM: VroomOptimizer(),
                OptimizerEngine.OR_TOOLS: ORToolsOptimizer(),
            }
            started, before = time.perf_counter(), queries[0]
            cycle = await run_fleet_cycle(
                factory, providers=providers, optimizers=solvers, clock=lambda: clock[0]
            )
            assert cycle.status == "ACTIVATED", cycle.failure_code
            cycle_seconds, cycle_queries = time.perf_counter() - started, queries[0] - before
            started, before = time.perf_counter(), queries[0]
            command = admission(context, clock[0], mass_g=100, volume_ml=100)
            reserved = await reserve_capacity(
                factory,
                customer_id=customer,
                command=command,
                providers=providers,
                optimizers=solvers,
                expires_at=clock[0] + timedelta(hours=1),
                now=clock[0],
            )
            from tirodhan.modules.routing_operations.orchestration_models import CapacityReservation

            async with factory() as session:
                assert (await session.get(CapacityReservation, reserved)).request_id is None
            admission_seconds, admission_queries = (
                time.perf_counter() - started,
                queries[0] - before,
            )
        print(
            "INC7_PG_BENCHMARK",
            json.dumps(
                dict(
                    vehicles=vehicles,
                    demand=demand,
                    snapshot_seconds=snapshot_times,
                    snapshot_queries=snapshot_queries,
                    cycle_seconds=cycle_seconds,
                    cycle_queries=cycle_queries,
                    admission_seconds=admission_seconds,
                    admission_queries=admission_queries,
                )
            ),
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", count)
