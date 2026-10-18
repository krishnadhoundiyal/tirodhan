"""Admission outcome/recovery QA, including real multi-profile roads and solvers."""

import json
import os
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select
from test_commercial_admission_benchmark import population, purchase
from test_geographical_qa import verify_source_coverage
from test_operational_orchestration import admission, regional
from test_routing_fleet import LineMatrix, engines

from tirodhan.db.values import new_uuid7
from tirodhan.modules.planning.routing.adapters import ORToolsOptimizer, VroomOptimizer
from tirodhan.modules.planning.routing.contracts import OptimizerEngine
from tirodhan.modules.planning.routing.osrm import OSRMMatrixProvider
from tirodhan.modules.planning.routing.validation import RoutingValidationError, validate_proposal
from tirodhan.modules.routing_operations import admission as service
from tirodhan.modules.routing_operations.admission_jobs import request_admission, run_admissions
from tirodhan.modules.routing_operations.admission_state import _READ_CACHES
from tirodhan.modules.routing_operations.commands import (
    PositionReport,
    WindowDeclare,
    execute_resource_command,
)
from tirodhan.modules.routing_operations.models import OperatingShift, OperatingVehicle
from tirodhan.modules.routing_operations.orchestration_models import (
    CapacityAdmissionRequest,
    CapacityReservation,
    RegionCell,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("TIRODHAN_GEO_QA") != "1", reason="explicit recovery QA qualification"
    ),
]


@pytest.mark.parametrize("unrelated_window", [False, True])
async def test_window_before_routing_epoch_cannot_block_valid_future_booking(
    database_session_factory,
    monkeypatch,
    unrelated_window,
):
    """A broad read must not turn an out-of-horizon opening into a negative interval."""
    import asyncio

    from tirodhan.modules.routing_operations.orchestration_models import OperatingRegion

    factory = database_session_factory
    monkeypatch.setenv("TIRODHAN_ADMISSION_VEHICLES", "2")
    inputs, now = await population(factory, 1)
    customer, command = inputs[0]
    async with factory() as session:
        region = await session.scalar(select(OperatingRegion))
    if unrelated_window:
        await execute_resource_command(
            factory,
            manager_id=region.recorded_by,
            command=WindowDeclare(
                client_command_id=new_uuid7(),
                receiving_point_id=region.default_warehouse_id,
                starts_at=command.slot_start - timedelta(hours=14),
                ends_at=command.slot_start - timedelta(hours=13),
            ),
            now=now,
            expires_at=now + timedelta(hours=1),
        )
    observed = dict(unrelated_window=unrelated_window)
    original = service.build_problem

    async def capture(snapshot, config, *args, **kwargs):
        observed["window_offsets_seconds"] = [
            [
                (w["starts_at"] - config.epoch).total_seconds(),
                (w["ends_at"] - config.epoch).total_seconds(),
            ]
            for w in snapshot.sources["windows"]
        ]
        return await original(snapshot, config, *args, **kwargs)

    monkeypatch.setattr(service, "build_problem", capture)
    await request_admission(
        factory, customer_id=customer, command=command, now=now, expires_at=now + timedelta(hours=1)
    )
    await run_admissions(
        factory,
        providers={"test-car": LineMatrix()},
        optimizers=engines(),
        limit=1,
        clock=lambda: now,
    )
    async with factory() as session:
        row = await session.get(CapacityAdmissionRequest, command.client_command_id)
        observed.update(
            status=row.status,
            failure_code=row.failure_code,
            reservations=await session.scalar(
                select(func.count()).select_from(CapacityReservation)
            ),
        )
    if base := os.getenv("TIRODHAN_RECOVERY_QA_OUTPUT"):
        output = Path(base).with_name(f"GEOGRAPHICAL_QA_WINDOW_{int(unrelated_window)}.json")
        await asyncio.to_thread(
            output.write_text, json.dumps(observed, indent=2, sort_keys=True) + "\n"
        )
    assert observed["status"] == "SUCCEEDED", observed


async def test_unacquired_arc_retries_after_cache_loss_without_false_infeasibility(
    database_session_factory,
):
    factory = database_session_factory
    data = await regional(factory, empty=True)
    clock, customer, context = data[6], data[8], data[9]
    command = admission(context, clock[0])
    kwargs = dict(
        customer_id=customer,
        command=command,
        expires_at=clock[0] + timedelta(hours=1),
        now=clock[0],
    )
    await request_admission(factory, **kwargs)

    class Unacquired:
        async def acquire(self, nodes):
            raise RoutingValidationError("ROAD_ARC_NOT_ACQUIRED")

    async def work(source):
        return await run_admissions(
            factory,
            providers={"test-car": source},
            optimizers=engines(),
            limit=1,
            clock=lambda: clock[0],
        )

    assert await work(Unacquired()) == 1
    async with factory() as session:
        row = await session.get(CapacityAdmissionRequest, command.client_command_id)
        assert row.status == "PENDING" and row.failure_code == "ROAD_ARC_NOT_ACQUIRED"
        assert await session.scalar(select(func.count()).select_from(CapacityReservation)) == 0
    _READ_CACHES.pop(factory.kw["bind"], None)  # Model loss of disposable process memory.
    clock[0] += timedelta(seconds=5)
    assert await work(LineMatrix()) == 1
    assert await work(LineMatrix()) == 0
    async with factory() as session:
        row = await session.get(CapacityAdmissionRequest, command.client_command_id)
        assert row.status == "SUCCEEDED" and row.attempts == 2
        assert await session.scalar(select(func.count()).select_from(CapacityReservation)) == 1


@pytest.mark.parametrize("cause", ["no_declared_shift", "physical_load_exceeds_all_vehicles"])
async def test_independently_infeasible_inputs_never_create_a_promise(
    database_session_factory, cause
):
    factory = database_session_factory
    data = await regional(factory, empty=True)
    clock, customer, context = data[6], data[8], data[9]
    if cause == "no_declared_shift":
        from tirodhan.modules.routing_operations.commands import ShiftClose

        async with factory() as session:
            shift = await session.scalar(select(OperatingShift))
        await data[5](ShiftClose(client_command_id=new_uuid7(), shift_id=shift.shift_id))
    command = admission(
        context, clock[0], mass_g=1_000_000 if cause.startswith("physical") else 1000
    )
    await request_admission(
        factory,
        customer_id=customer,
        command=command,
        expires_at=clock[0] + timedelta(hours=1),
        now=clock[0],
    )
    await run_admissions(
        factory,
        providers={"test-car": LineMatrix()},
        optimizers=engines(),
        limit=1,
        clock=lambda: clock[0],
    )
    async with factory() as session:
        row = await session.get(CapacityAdmissionRequest, command.client_command_id)
        assert row.status != "SUCCEEDED"
        assert await session.scalar(select(func.count()).select_from(CapacityReservation)) == 0
        if cause.startswith("physical"):
            # Search failure cannot claim a general global infeasibility proof,
            # even when this fixture independently proves a capacity shortfall.
            assert row.status == "PENDING" and row.failure_code.startswith("UNKNOWN_")
        else:
            assert row.failure_code == "NO_COMMITTED_CAPACITY"


async def test_real_multi_profile_admission_survives_source_change_and_process_restart(
    database_session_factory,
    monkeypatch,
):
    assert os.getenv("TIRODHAN_GEO_QA") == "1", "Mandatory native/road QA environment missing"
    factory = database_session_factory
    monkeypatch.setenv("TIRODHAN_ADMISSION_VEHICLES", "2")
    inputs, now = await population(factory, 8)
    async with factory() as session, session.begin():
        vehicles = list(
            await session.scalars(
                select(OperatingVehicle).order_by(OperatingVehicle.created_at.desc()).limit(2)
            )
        )
        # Master fixture inputs only, before any accepted promise or physical pickup.
        vehicles[0].profile_version = "test-bicycle"
    proofs = []
    original = service.persist_certificate

    async def independent(session, reservation, problem, proposal, *args):
        result = await original(session, reservation, problem, proposal, *args)
        validate_proposal(problem, proposal)
        expected_count = await verify_source_coverage(session, reservation, problem)
        proofs.append(
            {
                "profiles": sorted(m.profile_version for m in problem.matrices),
                "lots": len(problem.lots),
                "durable_obligations": expected_count,
            }
        )
        return result

    monkeypatch.setattr(service, "persist_certificate", independent)
    async with httpx.AsyncClient(trust_env=False) as client:
        providers = {
            f"test-{profile}": OSRMMatrixProvider(
                client=client,
                endpoint=f"http://127.0.0.1:{port}",
                graph_version=f"fixture-{profile}",
                profile_version=f"test-{profile}",
                max_nodes=64,
                max_snap_distance_m=300,
            )
            for profile, port in (("car", 5000), ("bicycle", 5001))
        }
        optimizers = {
            OptimizerEngine.VROOM: VroomOptimizer(),
            OptimizerEngine.OR_TOOLS: ORToolsOptimizer(),
        }
        for i, (customer, command) in enumerate(inputs):
            await request_admission(
                factory,
                customer_id=customer,
                command=command,
                expires_at=now + timedelta(hours=1),
                now=now,
            )
            assert (
                await run_admissions(
                    factory,
                    providers=providers,
                    optimizers=optimizers,
                    limit=1,
                    clock=lambda now=now: now,
                )
                == 1
            )
            async with factory() as session:
                row = await session.get(CapacityAdmissionRequest, command.client_command_id)
                assert row.status == "SUCCEEDED", row.failure_code
            await purchase(factory, customer, command, now)
            if i == 2:
                # Source change through the real resource command, followed by cold
                # read/matrix cache recovery; accepted paid commitments stay intact.
                from tirodhan.modules.routing_operations.fleet_models import FleetControl

                async with factory() as session:
                    control = await session.get(FleetControl, "COMMITTED_FLEET")
                await execute_resource_command(
                    factory,
                    manager_id=control.manager_id,
                    command=PositionReport(
                        client_command_id=new_uuid7(),
                        vehicle_id=vehicles[0].vehicle_id,
                        longitude=77.208,
                        latitude=28.631,
                        observed_at=now + timedelta(seconds=1),
                    ),
                    expires_at=now + timedelta(hours=1),
                    now=now + timedelta(seconds=1),
                )
                now += timedelta(seconds=1)
                _READ_CACHES.pop(factory.kw["bind"], None)
        assert (
            await run_admissions(
                factory, providers=providers, optimizers=optimizers, limit=1, clock=lambda: now
            )
            == 0
        )
    assert len(proofs) == 8 and all(p["profiles"] == ["test-bicycle", "test-car"] for p in proofs)
    if output := os.getenv("TIRODHAN_RECOVERY_QA_OUTPUT"):
        import asyncio

        await asyncio.to_thread(
            Path(output).write_text,
            json.dumps(
                dict(
                    admitted=8,
                    certificates_full_gate=8,
                    accepted_source_change_after=3,
                    process_cache_restarts=1,
                    profiles=["test-car", "test-bicycle"],
                    proofs=proofs,
                ),
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )


async def test_real_negative_osrm_admission_retries_without_losing_accepted_booking(
    database_session_factory,
    monkeypatch,
):
    import asyncio

    import h3
    from geoalchemy2.elements import WKTElement

    from tirodhan.modules.serviceability.models import ServiceabilityContext

    factory = database_session_factory
    monkeypatch.setenv("TIRODHAN_ADMISSION_VEHICLES", "2")
    inputs, now = await population(factory, 2)
    fixture = json.loads(
        (Path(__file__).parents[2] / "docs/benchmarks/GEOGRAPHICAL_QA_FIXTURE.json").read_text()
    )
    example = fixture["campaigns"]["E"]["invalid_examples"][0]
    async with factory() as session, session.begin():
        region = (await session.scalars(select(RegionCell))).first().region_id
        for (_, command), site in zip(
            inputs, (example["source"], example["destination"]), strict=True
        ):
            context = await session.get(ServiceabilityContext, command.context_id)
            context.location = WKTElement(
                f"POINT({site['longitude']} {site['latitude']})",
                srid=4326,
            )
            cell = h3.latlng_to_cell(site["latitude"], site["longitude"], 7)
            if await session.get(RegionCell, cell) is None:
                session.add(RegionCell(cell_id=cell, region_id=region))
            context.cell_id = cell
    async with httpx.AsyncClient(trust_env=False) as client:
        provider = OSRMMatrixProvider(
            client=client,
            endpoint="http://127.0.0.1:5000",
            graph_version="fixture-car",
            profile_version="test-car",
            max_nodes=64,
            max_snap_distance_m=300,
        )
        optimizers = {
            OptimizerEngine.VROOM: VroomOptimizer(),
            OptimizerEngine.OR_TOOLS: ORToolsOptimizer(),
        }

        async def work():
            return await run_admissions(
                factory,
                providers={"test-car": provider},
                optimizers=optimizers,
                limit=1,
                clock=lambda: now,
            )

        customer, first = inputs[0]
        await request_admission(
            factory,
            customer_id=customer,
            command=first,
            expires_at=now + timedelta(hours=1),
            now=now,
        )
        assert await work() == 1
        async with factory() as session:
            assert (
                await session.get(CapacityAdmissionRequest, first.client_command_id)
            ).status == "SUCCEEDED"
        await purchase(factory, customer, first, now)
        customer, second = inputs[1]
        await request_admission(
            factory,
            customer_id=customer,
            command=second,
            expires_at=now + timedelta(hours=1),
            now=now,
        )
        assert await work() == 1
        async with factory() as session:
            row = await session.get(CapacityAdmissionRequest, second.client_command_id)
            assert row.status == "PENDING" and row.failure_code == "INVALID_ROAD_MATRIX"
        _READ_CACHES.pop(factory.kw["bind"], None)
        now += timedelta(seconds=5)
        assert await work() == 1
        async with factory() as session:
            row = await session.get(CapacityAdmissionRequest, second.client_command_id)
            assert row.status == "PENDING" and row.failure_code == "INVALID_ROAD_MATRIX"
            assert row.attempts == 2
            assert await session.scalar(select(func.count()).select_from(CapacityReservation)) == 1
            from tirodhan.modules.collection_requests.models import CollectionRequest

            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(CollectionRequest)
                    .where(CollectionRequest.status == "ACCEPTED")
                )
                == 1
            )
    output = os.getenv("TIRODHAN_DURABLE_NEGATIVE_OUTPUT")
    if not output and (recovery_output := os.getenv("TIRODHAN_RECOVERY_QA_OUTPUT")):
        output = str(Path(recovery_output).with_name("GEOGRAPHICAL_QA_DURABLE_NEGATIVE.json"))
    if output:
        await asyncio.to_thread(
            Path(output).write_text,
            json.dumps(
                dict(
                    accepted_bookings_preserved=1,
                    reservations=1,
                    negative_intent_status="PENDING",
                    negative_intent_code="INVALID_ROAD_MATRIX",
                    attempts=2,
                    process_cache_restarts=1,
                    false_promises=0,
                ),
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
