"""QA reproduction: physical custody must outlive an operating shift.

Failures are intentionally visible qualification failures, never xfail/skip.
No production invariant, guard or assignment authority is patched.
"""

import asyncio
import json
import os
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from test_operational_orchestration import (
    admission,
    operational,
    regional,
    route_revision,
)
from test_routing_dispatch import execute
from test_routing_fleet import LineMatrix, engines, tick

from tirodhan.db.values import new_uuid7
from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.planning.models import PickupExecution
from tirodhan.modules.routing_operations.admission_jobs import request_admission, run_admissions
from tirodhan.modules.routing_operations.commands import (
    PositionReport,
    ShiftClose,
    ShiftOpen,
    WindowDeclare,
)
from tirodhan.modules.routing_operations.execution import (
    DischargeTask,
    PickupTask,
    StartTask,
    current_tasks,
)
from tirodhan.modules.routing_operations.models import OperatingShift
from tirodhan.modules.routing_operations.operational_commands import AuthorizeDischarge
from tirodhan.modules.routing_operations.orchestration_models import (
    CapacityAdmissionRequest,
    DischargeAuthorization,
)
from tirodhan.modules.routing_operations.physical import cargo_load
from tirodhan.modules.routing_operations.recovery import RecoveryCommand, recover_route
from tirodhan.modules.routing_operations.snapshot import capture_snapshot

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("TIRODHAN_GEO_QA") != "1", reason="explicit custody QA qualification"
    ),
]


async def test_expired_shift_keeps_cargo_without_blocking_other_vehicle(database_session_factory):
    factory = database_session_factory
    data = await regional(factory, second=True)
    f, vehicles, point, _, config, resource, clock, region, customer, context = data
    async with factory() as session:
        first = await session.scalar(
            select(OperatingShift).where(OperatingShift.vehicle_id == vehicles[0])
        )
        other = await session.scalar(
            select(OperatingShift).where(OperatingShift.vehicle_id == vehicles[1])
        )
        expired = first.ends_at + timedelta(seconds=1)
    # Close the empty second resource and declare its replacement shift through
    # the real command boundary; immutable existing shift terms are preserved.
    await resource(ShiftClose(client_command_id=new_uuid7(), shift_id=other.shift_id))
    await resource(
        ShiftOpen(
            client_command_id=new_uuid7(),
            vehicle_id=vehicles[1],
            rider_id=f.rider_ids[1],
            resource_kind="COMMITTED",
            commitment_id=new_uuid7(),
            starts_at=clock[0],
            ends_at=expired + timedelta(hours=8),
        )
    )
    await resource(
        WindowDeclare(
            client_command_id=new_uuid7(),
            receiving_point_id=point.receiving_point_id,
            starts_at=clock[0],
            ends_at=expired + timedelta(hours=8),
        )
    )
    # Authorize a still-open discharge deadline *before* route activation. This
    # avoids superseding a live task and isolates expiry from late-delivery issues.
    for request_id in data[3]:
        await operational(
            factory,
            f.manager_id,
            AuthorizeDischarge(
                client_command_id=new_uuid7(),
                request_id=request_id,
                receiving_point_id=point.receiving_point_id,
                discharge_deadline=expired + timedelta(hours=6),
            ),
            clock[0],
        )
    assert (await tick(factory, clock)).status == "ACTIVATED"
    root, revision = await route_revision(factory, vehicles[0])
    async with factory() as session:
        task = (await current_tasks(session, root))[0]
        pickup = await session.get(PickupExecution, task.pickup_execution_id)
        booking = await session.get(CollectionRequest, pickup.request_id)
    from test_operational_orchestration import ack

    await ack(factory, root, revision, f.rider_ids[0], clock[0])
    at = booking.slot_start
    await execute(
        factory,
        f.rider_ids[0],
        StartTask(
            client_command_id=new_uuid7(),
            task_id=task.task_id,
            revision_id=revision.revision_id,
        ),
        at,
    )
    await execute(
        factory,
        f.rider_ids[0],
        PickupTask(
            client_command_id=new_uuid7(),
            task_id=task.task_id,
            revision_id=revision.revision_id,
            outcome="COLLECTED",
        ),
        at,
    )
    collected = 1
    while True:
        async with factory() as session:
            pending = await current_tasks(session, root)
            if not pending or pending[0].kind != "PICKUP":
                break
            next_task = pending[0]
            next_pickup = await session.get(PickupExecution, next_task.pickup_execution_id)
            next_booking = await session.get(CollectionRequest, next_pickup.request_id)
        at = max(at, next_booking.slot_start)
        await resource(
            PositionReport(
                client_command_id=new_uuid7(),
                vehicle_id=vehicles[0],
                longitude=77.210,
                latitude=28.631,
                observed_at=at,
            ),
            at=at,
        )
        await execute(
            factory,
            f.rider_ids[0],
            StartTask(
                client_command_id=new_uuid7(),
                task_id=next_task.task_id,
                revision_id=revision.revision_id,
            ),
            at,
        )
        await execute(
            factory,
            f.rider_ids[0],
            PickupTask(
                client_command_id=new_uuid7(),
                task_id=next_task.task_id,
                revision_id=revision.revision_id,
                outcome="COLLECTED",
            ),
            at,
        )
        collected += 1
    assert collected == 2, "Counterexample must not leave unrelated overdue pickups"
    await recover_route(
        factory,
        manager_id=f.manager_id,
        command=RecoveryCommand(
            client_command_id=new_uuid7(),
            route_id=root.route_id,
            revision_id=revision.revision_id,
            reason_code="EXECUTION_EXCEPTION",
        ),
        now=at,
        expires_at=at + timedelta(days=1),
    )
    for vehicle in vehicles:
        await resource(
            PositionReport(
                client_command_id=new_uuid7(),
                vehicle_id=vehicle,
                longitude=77.211,
                latitude=28.630,
                observed_at=expired,
            ),
            at=expired,
        )
    projected_config = config.model_copy(
        update=dict(
            epoch=expired,
            horizon_end=expired + timedelta(hours=8),
            as_of=expired,
            region_id=region,
            regional=True,
            fleet_mode=True,
            admission_mode=True,
        )
    )
    snapshot = await capture_snapshot(factory, projected_config)
    observed = dict(
        collected_before_expiry=collected,
        expired_shift_excluded=vehicles[0]
        not in {v["vehicle_id"] for v in snapshot.sources["vehicles"]},
        independent_vehicle_present=vehicles[1]
        in {v["vehicle_id"] for v in snapshot.sources["vehicles"]},
        orphaned_route_explicit=root.route_id
        in {r["route_id"] for r in snapshot.sources["orphaned_routes"]},
    )
    async with factory() as session:
        authorizations = list(await session.scalars(select(DischargeAuthorization)))
        observed["discharge_deadline_after_expiry"] = len(authorizations) == 2 and all(
            row.discharge_deadline > expired for row in authorizations
        )
        observed["onboard_mass_g"] = (await cargo_load(session, vehicles[0]))[0]
        observed["cargo_preserved"] = observed["onboard_mass_g"] == 8000
        observed["collected_history_preserved"] = (
            await session.get(PickupExecution, task.pickup_execution_id)
        ).status == "COLLECTED"
    try:
        await resource(
            ShiftClose(client_command_id=new_uuid7(), shift_id=first.shift_id), at=expired
        )
        observed["shift_close"] = "SUCCEEDED"
    except Exception as error:
        observed["shift_close"] = str(error)
    try:
        await resource(
            ShiftOpen(
                client_command_id=new_uuid7(),
                vehicle_id=vehicles[0],
                rider_id=f.rider_ids[0],
                resource_kind="COMMITTED",
                commitment_id=new_uuid7(),
                starts_at=expired,
                ends_at=expired + timedelta(hours=4),
            ),
            at=expired,
        )
        observed["future_shift_same_vehicle"] = "SUCCEEDED"
    except Exception as error:
        # Do not leak SQL parameters, raw UUIDs or exception payloads.
        observed["future_shift_same_vehicle"] = type(error).__name__
        original = getattr(error, "orig", None)
        cause = getattr(original, "__cause__", None)
        observed["future_shift_constraint"] = getattr(cause, "constraint_name", None)
    command = admission(context, expired + timedelta(minutes=10))
    await request_admission(
        factory,
        customer_id=customer,
        command=command,
        expires_at=expired + timedelta(hours=1),
        now=expired,
    )
    await run_admissions(
        factory,
        providers={"test-car": LineMatrix()},
        optimizers=engines(),
        limit=1,
        clock=lambda: expired,
    )
    async with factory() as session:
        row = await session.get(CapacityAdmissionRequest, command.client_command_id)
        observed["independent_admission"] = row.status
        observed["independent_admission_code"] = row.failure_code
        discharge = (await current_tasks(session, root))[0]
    try:
        await execute(
            factory,
            f.rider_ids[0],
            StartTask(
                client_command_id=new_uuid7(),
                task_id=discharge.task_id,
                revision_id=revision.revision_id,
            ),
            expired,
        )
        await execute(
            factory,
            f.rider_ids[0],
            DischargeTask(
                client_command_id=new_uuid7(),
                task_id=discharge.task_id,
                revision_id=revision.revision_id,
                scanned_pickup_id=task.pickup_execution_id,
                scanned_receiving_identifier="test-kiosk-42",
                client_capture_id=new_uuid7(),
                captured_at=expired,
                longitude=77.211,
                latitude=28.630,
            ),
            expired,
        )
        observed["handover"] = "SUCCEEDED"
    except Exception as error:
        observed["handover"] = str(error)
    async with factory() as session:
        observed["cargo_released_after_handover"] = await cargo_load(session, vehicles[0]) == (0, 0)
    if output := os.getenv("TIRODHAN_CUSTODY_QA_OUTPUT"):
        await asyncio.to_thread(
            Path(output).write_text, json.dumps(observed, indent=2, sort_keys=True) + "\n"
        )
    print("CUSTODY_QA_RESULT=" + json.dumps(observed, sort_keys=True))
    assert observed["expired_shift_excluded"] and observed["cargo_preserved"]
    assert observed["discharge_deadline_after_expiry"]
    assert observed["orphaned_route_explicit"] and observed["collected_history_preserved"]
    assert observed["independent_admission"] == "SUCCEEDED", observed
    assert observed["future_shift_same_vehicle"] == "SUCCEEDED", observed
    assert observed["cargo_released_after_handover"], observed
