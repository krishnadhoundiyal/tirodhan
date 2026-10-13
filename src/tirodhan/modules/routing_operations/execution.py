from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter
from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.customers.service import GeoPoint
from tirodhan.modules.dispatch.models import RiderAssignment, RiderAssignmentItem, RiderAvailability
from tirodhan.modules.evidence.models import EvidenceCapture, HandoverEvidenceLink
from tirodhan.modules.handovers.models import HandoverEvent, HandoverEventItem
from tirodhan.modules.handovers.service import record_handover_in_session
from tirodhan.modules.pickups.models import PickupAttempt
from tirodhan.modules.planning.models import PickupExecution
from tirodhan.modules.receiving_points.models import ReceivingPoint
from tirodhan.modules.reliability.primitives import (
    append_outbox_event,
    claim_idempotency_record,
    complete_idempotency_record,
)

from .authority import RouteAuthorityError, lock_booking_population, lock_resources
from .commands import require_live_role
from .dispatch_models import (
    RouteActivation,
    RouteDischargeProof,
    RouteRevision,
    RouteRevisionStop,
    RouteTask,
    VehicleRoute,
)
from .models import (
    CargoVehicleObservation,
    OperatingShift,
    OperatingVehicle,
    ReceivingWindow,
    RoutingManifest,
    RoutingShadowRun,
    VehiclePosition,
)
from .snapshot import ShadowConfiguration


class TaskCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    client_command_id: UUID
    task_id: UUID
    revision_id: UUID


class StartTask(TaskCommand):
    operation: Literal["START"] = "START"


class PickupTask(TaskCommand):
    operation: Literal["PICKUP"] = "PICKUP"
    outcome: Literal["COLLECTED", "NOT_COLLECTED"]


class DischargeTask(TaskCommand):
    operation: Literal["DISCHARGE"] = "DISCHARGE"
    scanned_pickup_id: UUID
    scanned_receiving_identifier: str = Field(min_length=1, max_length=200)
    client_capture_id: UUID
    captured_at: AwareDatetime
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)
    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)


ExecutionCommand = Annotated[
    StartTask | PickupTask | DischargeTask, Field(discriminator="operation")
]
EXECUTION_CODEC: TypeAdapter[ExecutionCommand] = TypeAdapter(ExecutionCommand)


async def current_tasks(session: AsyncSession, root: VehicleRoute) -> list[RouteTask]:
    return list(
        await session.scalars(
            select(RouteTask)
            .join(RouteRevisionStop, RouteRevisionStop.task_id == RouteTask.task_id)
            .join(RouteRevision, RouteRevision.revision_id == RouteRevisionStop.revision_id)
            .where(
                RouteRevision.route_id == root.route_id,
                RouteRevision.version == root.version,
                RouteTask.state.in_(("PENDING", "STARTED")),
            )
            .order_by(RouteRevisionStop.ordinal)
        )
    )


async def execute_task(
    factory: async_sessionmaker[AsyncSession],
    *,
    rider_id: UUID,
    command: ExecutionCommand,
    expires_at: datetime,
    now: datetime | None = None,
) -> UUID:
    now = now or utc_now()
    async with factory() as session, session.begin():
        await require_live_role(session, rider_id, "RIDER")
        claim = await claim_idempotency_record(
            session,
            scope=f"routing.execute:{rider_id}",
            idempotency_key=str(command.client_command_id),
            request_fingerprint=hashlib.sha256(EXECUTION_CODEC.dump_json(command)).digest(),
            expires_at=expires_at,
            now=now,
        )
        if claim.record.status == "COMPLETED":
            assert claim.record.result_resource_id is not None
            return claim.record.result_resource_id
        if not claim.created:
            raise RouteAuthorityError("COMMAND_IN_PROGRESS")
        task = await session.get(RouteTask, command.task_id)
        if task is None:
            raise RouteAuthorityError("TASK_NOT_FOUND")
        root = await session.get(VehicleRoute, task.route_id)
        assert root is not None
        assignment = await session.get(RiderAssignment, root.assignment_id)
        assert assignment is not None
        if assignment.rider_id != rider_id:
            raise RouteAuthorityError("ROUTE_WRONG_RIDER")
        pickup = await session.get(PickupExecution, task.pickup_execution_id)
        assert pickup is not None
        # Cancellation and pickup must have one winner. No resource -> Payment lock inversion.
        if task.kind == "PICKUP":
            await lock_booking_population(session, [pickup.request_id])
        await lock_resources(session, [rider_id], [root.vehicle_id])
        await session.refresh(root)
        await session.refresh(task)
        await session.refresh(pickup)
        if root.status == "CLOSED" or (
            root.status == "RECOVERY_REQUIRED" and task.kind != "DISCHARGE"
        ):
            raise RouteAuthorityError("ROUTE_NOT_EXECUTABLE")
        revision = await session.get(RouteRevision, command.revision_id)
        if (
            revision is None
            or revision.route_id != root.route_id
            or revision.version != root.version
        ):
            raise RouteAuthorityError("ROUTE_REVISION_STALE")
        tasks = await current_tasks(session, root)
        if not tasks or tasks[0].task_id != task.task_id:
            raise RouteAuthorityError("TASK_NOT_NEXT")
        shift = await session.get(OperatingShift, root.shift_id)
        assert shift is not None
        if shift.closed_at is not None or not shift.starts_at <= now < shift.ends_at:
            raise RouteAuthorityError("ACTIVE_SHIFT_REQUIRED")
        run_id = await session.scalar(
            select(RouteActivation.run_id).where(
                RouteActivation.activation_id == revision.activation_id
            )
        )
        run = await session.get(RoutingShadowRun, run_id)
        assert run is not None
        config = ShadowConfiguration.model_validate(run.configuration)
        position = await session.scalar(
            select(VehiclePosition)
            .where(VehiclePosition.vehicle_id == root.vehicle_id)
            .order_by(VehiclePosition.revision.desc())
            .limit(1)
        )
        if (
            position is None
            or not 0
            <= (now - position.observed_at).total_seconds()
            <= config.max_position_age_seconds
        ):
            raise RouteAuthorityError("FRESH_POSITION_REQUIRED")
        manifest = await session.get(RoutingManifest, pickup.request_id)
        assert manifest is not None
        if isinstance(command, StartTask):
            if task.state == "PENDING":
                if task.kind == "PICKUP":
                    request = await session.get(CollectionRequest, pickup.request_id)
                    assert request is not None
                    if (
                        request.status not in ("ACCEPTED", "PLANNED")
                        or not request.slot_start <= now <= request.slot_end
                    ):
                        raise RouteAuthorityError("PICKUP_WINDOW_OR_BOOKING_INVALID")
                    request.status = "PLANNED"
                else:
                    await _require_discharge_ready(session, root, task, pickup, manifest, now)
                task.state, task.started_at = "STARTED", now
            result = task.task_id
        elif isinstance(command, PickupTask):
            if task.kind != "PICKUP" or task.state != "STARTED" or pickup.status != "ASSIGNED":
                raise RouteAuthorityError("PICKUP_NOT_STARTED")
            owner = await session.scalar(
                select(RiderAssignmentItem).where(
                    RiderAssignmentItem.pickup_execution_id == pickup.pickup_execution_id,
                    RiderAssignmentItem.released_at.is_(None),
                )
            )
            if owner is None or owner.assignment_id != root.assignment_id:
                raise RouteAuthorityError("OWNERSHIP_CHANGED")
            if command.outcome == "COLLECTED":
                await _require_capacity(session, root, manifest)
            number = int(
                await session.scalar(
                    select(func.coalesce(func.max(PickupAttempt.attempt_number), 0) + 1).where(
                        PickupAttempt.pickup_execution_id == pickup.pickup_execution_id
                    )
                )
                or 1
            )
            attempt = PickupAttempt(
                pickup_attempt_id=new_uuid7(),
                pickup_execution_id=pickup.pickup_execution_id,
                rider_assignment_id=root.assignment_id,
                client_attempt_id=command.client_command_id,
                attempt_number=number,
                outcome=command.outcome,
                attempted_at=now,
                created_at=now,
            )
            session.add(attempt)
            await session.flush([attempt])
            if command.outcome == "COLLECTED":
                pickup.status, pickup.collected_at, pickup.updated_at = "COLLECTED", now, now
                task.state, task.completed_at = "COMPLETED", now
                session.add(
                    CargoVehicleObservation(
                        request_id=pickup.request_id,
                        vehicle_id=root.vehicle_id,
                        pickup_attempt_id=attempt.pickup_attempt_id,
                        recorded_by=rider_id,
                        created_at=now,
                    )
                )
            result = attempt.pickup_attempt_id
        else:
            if task.kind != "DISCHARGE" or task.state != "STARTED":
                raise RouteAuthorityError("DISCHARGE_NOT_STARTED")
            await _require_discharge_ready(session, root, task, pickup, manifest, now)
            if command.scanned_pickup_id != pickup.pickup_execution_id:
                raise RouteAuthorityError("CARGO_LABEL_MISMATCH")
            if task.started_at is None or not task.started_at <= command.captured_at <= now:
                raise RouteAuthorityError("CAPTURE_TIMESTAMP_INVALID")
            point = await session.scalar(
                select(ReceivingPoint)
                .where(ReceivingPoint.receiving_point_id == task.receiving_point_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            assert point is not None
            if (
                point.official_identifier is None
                or command.scanned_receiving_identifier != point.official_identifier
            ):
                raise RouteAuthorityError("RECEIVING_IDENTIFIER_MISMATCH")
            event = await record_handover_in_session(
                session,
                client_handover_id=command.client_command_id,
                rider_id=rider_id,
                receiving_point_id=point.receiving_point_id,
                pickup_execution_ids=[pickup.pickup_execution_id],
                observed_location=GeoPoint(command.latitude, command.longitude),
                now=now,
            )
            capture = EvidenceCapture(
                client_capture_id=command.client_capture_id,
                captured_by_user_id=rider_id,
                captured_at=command.captured_at,
                created_at=now,
            )
            session.add(capture)
            await session.flush([capture])
            session.add(
                HandoverEvidenceLink(
                    handover_event_id=event.handover_event_id,
                    evidence_capture_id=capture.evidence_capture_id,
                )
            )
            session.add(
                RouteDischargeProof(
                    handover_event_id=event.handover_event_id,
                    task_id=task.task_id,
                    evidence_capture_id=capture.evidence_capture_id,
                    receiving_identifier_snapshot=point.official_identifier,
                    evidence_kind="PHOTO",
                )
            )
            if event.status == "VALIDATED":
                task.state, task.completed_at = "COMPLETED", now
            result = event.handover_event_id
        vehicle = await session.get(OperatingVehicle, root.vehicle_id)
        assert vehicle is not None
        vehicle.version += 1
        await session.flush()
        await finish_if_empty(session, root, now)
        await append_outbox_event(
            session,
            event_key=f"route-task-command:{command.client_command_id}:{rider_id}",
            aggregate_type="VehicleRoute",
            aggregate_id=root.route_id,
            event_type="VehicleRouteProgressed",
            payload={"route_id": str(root.route_id), "task_id": str(task.task_id)},
        )
        await complete_idempotency_record(
            session,
            claim.record,
            result_resource_id=result,
            result_status_code=200,
            completed_at=now,
        )
        return result


async def _require_discharge_ready(
    session: AsyncSession,
    root: VehicleRoute,
    task: RouteTask,
    pickup: PickupExecution,
    manifest: RoutingManifest,
    now: datetime,
) -> None:
    observation = await session.get(CargoVehicleObservation, pickup.request_id)
    if (
        pickup.status != "COLLECTED"
        or observation is None
        or observation.vehicle_id != root.vehicle_id
    ):
        raise RouteAuthorityError("VEHICLE_CARGO_REQUIRED")
    if task.receiving_point_id != manifest.receiving_point_id or now > manifest.discharge_deadline:
        raise RouteAuthorityError("DISCHARGE_DESTINATION_OR_DEADLINE_INVALID")
    window = await session.scalar(
        select(ReceivingWindow.window_id)
        .where(
            ReceivingWindow.receiving_point_id == manifest.receiving_point_id,
            ReceivingWindow.starts_at <= now,
            ReceivingWindow.ends_at >= now,
        )
        .limit(1)
    )
    if window is None:
        raise RouteAuthorityError("RECEIVING_POINT_CLOSED")


async def _require_capacity(
    session: AsyncSession, root: VehicleRoute, new: RoutingManifest
) -> None:
    handed = exists().where(
        HandoverEventItem.pickup_execution_id == PickupExecution.pickup_execution_id,
        HandoverEventItem.status == "VALIDATED",
        HandoverEventItem.handover_event_id == HandoverEvent.handover_event_id,
        HandoverEvent.status == "VALIDATED",
    )
    loads = (
        await session.execute(
            select(
                func.coalesce(func.sum(RoutingManifest.mass_g), 0),
                func.coalesce(func.sum(RoutingManifest.volume_ml), 0),
            )
            .join(
                CargoVehicleObservation,
                CargoVehicleObservation.request_id == RoutingManifest.request_id,
            )
            .join(PickupExecution, PickupExecution.request_id == RoutingManifest.request_id)
            .where(CargoVehicleObservation.vehicle_id == root.vehicle_id, ~handed)
        )
    ).one()
    vehicle = await session.get(OperatingVehicle, root.vehicle_id)
    assert vehicle is not None
    if (
        loads[0] + new.mass_g > vehicle.mass_capacity_g
        or loads[1] + new.volume_ml > vehicle.volume_capacity_ml
    ):
        raise RouteAuthorityError("PHYSICAL_CAPACITY_EXCEEDED")


async def finish_if_empty(session: AsyncSession, root: VehicleRoute, now: datetime) -> None:
    remaining = await session.scalar(
        select(RouteTask.task_id)
        .where(RouteTask.route_id == root.route_id, RouteTask.state.in_(("PENDING", "STARTED")))
        .limit(1)
    )
    if remaining is not None:
        return
    root.status, root.closed_at = "CLOSED", now
    assignment = await session.get(RiderAssignment, root.assignment_id)
    assert assignment is not None
    assignment.status, assignment.completed_at = "COMPLETED", now
    availability = await session.get(RiderAvailability, assignment.rider_id)
    assert availability is not None
    availability.work_state, availability.updated_at = "IDLE", now
    availability.version += 1
