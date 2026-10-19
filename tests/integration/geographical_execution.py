"""Actual ordered vehicle-bound execution; no physical facts are inserted by the fixture."""

from datetime import timedelta
from uuid import UUID

from geographical_fixture import resource
from sqlalchemy import select

from tirodhan.db.values import new_uuid7
from tirodhan.modules.handovers.models import HandoverEvent
from tirodhan.modules.pickups.models import PickupAttempt
from tirodhan.modules.planning.models import PickupExecution
from tirodhan.modules.routing_operations.commands import PositionReport
from tirodhan.modules.routing_operations.dispatch_models import (
    RouteDischargeProof,
    RouteRevision,
    RouteRevisionStop,
    RouteTask,
    VehicleRoute,
)
from tirodhan.modules.routing_operations.execution import (
    DepartTask,
    DischargeTask,
    PickupTask,
    StartTask,
    current_tasks,
    execute_task,
)
from tirodhan.modules.routing_operations.models import CargoVehicleObservation, RoutingManifest
from tirodhan.modules.routing_operations.physical import cargo_load, effective_manifest


async def progress(factory, region, route, mode):
    identity = UUID(route["route_id"])
    rider = UUID(route["rider_id"])
    vehicle = UUID(route["vehicle_id"])
    events = []
    for _ in range(1000):
        async with factory() as session:
            root = await session.get(VehicleRoute, identity)
            tasks = await current_tasks(session, root)
            if not tasks:
                return events
            task = tasks[0]
            pickup = await session.get(PickupExecution, task.pickup_execution_id)
            manifest = await effective_manifest(
                session, await session.get(RoutingManifest, pickup.request_id)
            )
            before_load = await cargo_load(session, vehicle)
            revision = await session.scalar(
                select(RouteRevision).where(
                    RouteRevision.route_id == identity, RouteRevision.version == root.version
                )
            )
            schedule = await session.scalar(
                select(RouteRevisionStop).where(
                    RouteRevisionStop.revision_id == revision.revision_id,
                    RouteRevisionStop.task_id == task.task_id,
                )
            )
        if mode == "depart":
            command = DepartTask(
                client_command_id=new_uuid7(),
                task_id=task.task_id,
                revision_id=revision.revision_id,
            )
            at = region["clock"]
        else:
            node = (
                next(b for b in region["bookings"] if b["booking_id"] == str(pickup.request_id))
                if task.kind == "PICKUP"
                else region["origin"]
            )
            at = max(region["clock"], schedule.planned_start)
            await resource(
                factory,
                region["manager"],
                PositionReport(
                    client_command_id=new_uuid7(),
                    vehicle_id=vehicle,
                    longitude=node["longitude"],
                    latitude=node["latitude"],
                    observed_at=at,
                ),
                at,
            )
            await execute_task(
                factory,
                rider_id=rider,
                command=StartTask(
                    client_command_id=new_uuid7(),
                    task_id=task.task_id,
                    revision_id=revision.revision_id,
                ),
                expires_at=at + timedelta(days=2),
                now=at,
            )
            at += schedule.planned_end - schedule.planned_start
            if task.kind == "PICKUP":
                command = PickupTask(
                    client_command_id=new_uuid7(),
                    task_id=task.task_id,
                    revision_id=revision.revision_id,
                    outcome="COLLECTED",
                )
            else:
                command = DischargeTask(
                    client_command_id=new_uuid7(),
                    task_id=task.task_id,
                    revision_id=revision.revision_id,
                    scanned_pickup_id=pickup.pickup_execution_id,
                    scanned_receiving_identifier=f"QA-{region['point']}",
                    client_capture_id=new_uuid7(),
                    captured_at=at,
                    longitude=node["longitude"],
                    latitude=node["latitude"],
                )
        result = await execute_task(
            factory, rider_id=rider, command=command, expires_at=at + timedelta(days=2), now=at
        )
        assert (
            await execute_task(
                factory, rider_id=rider, command=command, expires_at=at + timedelta(days=2), now=at
            )
            == result
        )
        region["clock"] = at
        fact = {}
        async with factory() as session:
            after_load = await cargo_load(session, vehicle)
            persisted_task = await session.get(RouteTask, task.task_id)
            if mode == "depart":
                assert persisted_task.committed_at == at
                assert after_load == before_load
            elif task.kind == "PICKUP":
                attempt = await session.get(PickupAttempt, result)
                observation = await session.get(CargoVehicleObservation, pickup.request_id)
                assert attempt.outcome == "COLLECTED"
                assert observation.vehicle_id == vehicle and observation.recorded_by == rider
                assert persisted_task.state == "COMPLETED"
                assert after_load == (
                    before_load[0] + manifest.mass_g,
                    before_load[1] + manifest.volume_ml,
                )
                fact = dict(
                    pickup_attempt_id=str(result),
                    outcome=attempt.outcome,
                    cargo_vehicle=str(observation.vehicle_id),
                    custodian=str(observation.recorded_by),
                )
            else:
                handover = await session.get(HandoverEvent, result)
                proof = await session.get(RouteDischargeProof, result)
                assert handover.status == "VALIDATED" and proof.task_id == task.task_id
                assert persisted_task.state == "COMPLETED"
                assert after_load == (
                    before_load[0] - manifest.mass_g,
                    before_load[1] - manifest.volume_ml,
                )
                fact = dict(
                    handover_id=str(result),
                    status=handover.status,
                    evidence_capture_id=str(proof.evidence_capture_id),
                )
        events.append(
            dict(
                command=command.model_dump(mode="json"),
                result=str(result),
                at=at.isoformat(),
                cargo_before=before_load,
                cargo_after=after_load,
                persisted_fact=fact,
            )
        )
        if mode in {"depart", "collect"} or task.kind == "DISCHARGE":
            return events
    raise AssertionError("Execution failed to reach a discharge in the bounded route")
