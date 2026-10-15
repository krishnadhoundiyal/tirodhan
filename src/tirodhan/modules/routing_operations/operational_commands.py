"""Explicit receipt, consent and physical recovery commands with conventional idempotency."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal, cast
from uuid import UUID

from pydantic import AwareDatetime, Field, TypeAdapter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.evidence.models import EvidenceCapture
from tirodhan.modules.handovers.models import HandoverEventItem
from tirodhan.modules.planning.models import PickupExecution
from tirodhan.modules.receiving_points.models import ReceivingPoint
from tirodhan.modules.reliability.primitives import (
    claim_idempotency_record,
    complete_idempotency_record,
)

from .authority import RouteAuthorityError, lock_booking_population, lock_resources
from .commands import Command, OperationalInputError, require_live_role
from .dispatch_models import RouteRevision, RouteTask, VehicleRoute
from .models import CargoVehicleObservation, OperatingShift, OperatingVehicle, RoutingManifest
from .orchestration import wake
from .orchestration_models import (
    AppointmentConsent,
    CapacityReservation,
    CargoTransfer,
    CargoTransferIntent,
    CargoTransferReceipt,
    DischargeAuthorization,
    FlexibleCommitment,
    FlexibleRouteOffer,
    OperatingBreak,
    OperatingRegion,
    RegionCell,
    RegionReceivingPoint,
    RegionVehicle,
    RouteAcknowledgment,
    ShiftExtension,
    ShiftExtensionConsent,
)
from .regional_projection import effective_shift_end
from .rolling import FleetPolicy


class ConfigureRegion(Command):
    operation: Literal["REGION"] = "REGION"
    name: str = Field(min_length=1, max_length=100)
    cells: tuple[str, ...] = Field(min_length=1, max_length=10000)
    vehicle_ids: tuple[UUID, ...] = Field(min_length=1, max_length=32)
    receiving_point_ids: tuple[UUID, ...] = Field(min_length=1, max_length=64)
    default_warehouse_id: UUID
    fleet: FleetPolicy
    safety_percent: int = Field(default=125, strict=True, ge=100, le=200)
    minimum_pickup_seconds: int = Field(default=180, strict=True, ge=1, le=3600)
    minimum_discharge_seconds: int = Field(default=60, strict=True, ge=1, le=3600)


class AcknowledgeRoute(Command):
    operation: Literal["ACKNOWLEDGE"] = "ACKNOWLEDGE"
    revision_id: UUID


class DeclareBreak(Command):
    operation: Literal["BREAK"] = "BREAK"
    shift_id: UUID
    starts_at: AwareDatetime
    ends_at: AwareDatetime


class ConsentExtension(Command):
    operation: Literal["EXTENSION_CONSENT"] = "EXTENSION_CONSENT"
    shift_id: UUID
    ends_at: AwareDatetime


class AuthorizeExtension(Command):
    operation: Literal["EXTENSION"] = "EXTENSION"
    consent_id: UUID
    safe_conditions_confirmed: Literal[True]


class PrepareTransfer(Command):
    operation: Literal["TRANSFER_PREPARE"] = "TRANSFER_PREPARE"
    request_id: UUID
    from_vehicle_id: UUID
    to_shift_id: UUID


class TransferReceipt(Command):
    operation: Literal["TRANSFER_RECEIPT"] = "TRANSFER_RECEIPT"
    intent_id: UUID
    scanned_request_id: UUID
    captured_at: AwareDatetime


class WitnessTransferReceipt(Command):
    operation: Literal["TRANSFER_DONOR_WITNESS"] = "TRANSFER_DONOR_WITNESS"
    intent_id: UUID
    scanned_request_id: UUID
    captured_at: AwareDatetime
    physical_handoff_witnessed: Literal[True]


class CompleteTransfer(Command):
    operation: Literal["TRANSFER_COMPLETE"] = "TRANSFER_COMPLETE"
    intent_id: UUID


class AbortTransfer(Command):
    operation: Literal["TRANSFER_ABORT"] = "TRANSFER_ABORT"
    intent_id: UUID
    scanned_request_id: UUID
    client_capture_id: UUID
    captured_at: AwareDatetime
    material_with_current_custodian: Literal[True]


class AuthorizeDischarge(Command):
    operation: Literal["DISCHARGE_DESTINATION"] = "DISCHARGE_DESTINATION"
    request_id: UUID
    receiving_point_id: UUID
    discharge_deadline: AwareDatetime


class ConsentAppointment(Command):
    operation: Literal["APPOINTMENT_CONSENT"] = "APPOINTMENT_CONSENT"
    request_id: UUID
    reservation_id: UUID


class AcceptFlexibleOffer(Command):
    operation: Literal["FLEXIBLE_ACCEPT"] = "FLEXIBLE_ACCEPT"
    offer_id: UUID


class AcceptAdmissionOffer(Command):
    operation: Literal["ADMISSION_FLEXIBLE_ACCEPT"] = "ADMISSION_FLEXIBLE_ACCEPT"
    offer_id: UUID


class ResumeRoute(Command):
    operation: Literal["RESUME_AUTHORIZE"] = "RESUME_AUTHORIZE"
    revision_id: UUID


class AuthorizeReattempt(Command):
    operation: Literal["REATTEMPT_AUTHORIZE"] = "REATTEMPT_AUTHORIZE"
    request_id: UUID


class ObservePhysicalLoad(Command):
    operation: Literal["LOAD_OBSERVE"] = "LOAD_OBSERVE"
    request_id: UUID
    mass_g: int = Field(strict=True, gt=0, le=2**31 - 1)
    volume_ml: int = Field(strict=True, gt=0, le=2**31 - 1)
    client_capture_id: UUID
    captured_at: AwareDatetime
    evidence_media_type: Literal["PHOTO"] = "PHOTO"


OperationalCommand = Annotated[
    ConfigureRegion
    | AcknowledgeRoute
    | DeclareBreak
    | ConsentExtension
    | AuthorizeExtension
    | PrepareTransfer
    | TransferReceipt
    | WitnessTransferReceipt
    | CompleteTransfer
    | AbortTransfer
    | AuthorizeDischarge
    | ConsentAppointment
    | AcceptFlexibleOffer
    | AcceptAdmissionOffer
    | ResumeRoute
    | AuthorizeReattempt
    | ObservePhysicalLoad,
    Field(discriminator="operation"),
]
OPERATIONAL_CODEC: TypeAdapter[OperationalCommand] = TypeAdapter(cast(Any, OperationalCommand))


async def execute_operational(
    factory: async_sessionmaker[AsyncSession],
    *,
    actor_id: UUID,
    command: OperationalCommand,
    expires_at: datetime,
    now: datetime | None = None,
    clock: Callable[[], datetime] | None = None,
) -> UUID:
    clock = clock or (utc_now if now is None else None)
    now = now or utc_now()
    manager = isinstance(
        command,
        (
            ConfigureRegion,
            AuthorizeExtension,
            PrepareTransfer,
            CompleteTransfer,
            AbortTransfer,
            WitnessTransferReceipt,
            AuthorizeDischarge,
            ResumeRoute,
            AuthorizeReattempt,
            ObservePhysicalLoad,
        ),
    )
    role = (
        "MANAGER" if manager else "CUSTOMER" if isinstance(command, ConsentAppointment) else "RIDER"
    )
    async with factory() as session, session.begin():
        await require_live_role(session, actor_id, role)
        claim = await claim_idempotency_record(
            session,
            scope=f"routing.operation:{actor_id}",
            idempotency_key=str(command.client_command_id),
            request_fingerprint=hashlib.sha256(command.model_dump_json().encode()).digest(),
            expires_at=expires_at,
            now=now,
        )
        if claim.record.status == "COMPLETED":
            assert claim.record.result_resource_id is not None
            return claim.record.result_resource_id
        if not claim.created:
            raise RouteAuthorityError("COMMAND_IN_PROGRESS")
        result = await _execute(session, actor_id, command, now, expires_at, clock=clock)
        await session.flush()
        if not isinstance(
            command, (AcknowledgeRoute, PrepareTransfer, TransferReceipt, ConsentExtension)
        ):
            await wake(
                session,
                command.operation,
                now,
                urgent=isinstance(command, (CompleteTransfer, AuthorizeDischarge)),
            )
        await complete_idempotency_record(
            session,
            claim.record,
            result_resource_id=result,
            result_status_code=200,
            completed_at=now,
        )
        return result


async def _shift(
    session: AsyncSession,
    shift_id: UUID,
    rider_id: UUID,
    now: datetime,
    clock: Callable[[], datetime] | None = None,
    *,
    allow_expired: bool = False,
) -> OperatingShift:
    shift = await session.get(OperatingShift, shift_id)
    if shift is None or shift.rider_id != rider_id:
        raise RouteAuthorityError("WRONG_SHIFT")
    now = await lock_at_time(session, [rider_id], [shift.vehicle_id], now=now, clock=clock)
    await session.refresh(shift)
    if (
        shift.closed_at is not None
        or now < shift.starts_at
        or (
            not allow_expired and now >= await effective_shift_end(session, shift_id, shift.ends_at)
        )
    ):
        raise RouteAuthorityError("ACTIVE_SHIFT_REQUIRED")
    return shift


async def custodian(session: AsyncSession, request_id: UUID) -> tuple[UUID, UUID]:
    transfer = await session.scalar(
        select(CargoTransfer)
        .where(CargoTransfer.request_id == request_id)
        .order_by(CargoTransfer.sequence.desc())
        .limit(1)
    )
    if transfer:
        return transfer.to_vehicle_id, transfer.to_rider_id
    observation = await session.get(CargoVehicleObservation, request_id)
    if observation is None:
        raise RouteAuthorityError("CARGO_OBSERVATION_REQUIRED")
    from tirodhan.modules.dispatch.models import RiderAssignment
    from tirodhan.modules.pickups.models import PickupAttempt

    rider = await session.scalar(
        select(RiderAssignment.rider_id)
        .join(PickupAttempt)
        .where(PickupAttempt.pickup_attempt_id == observation.pickup_attempt_id)
    )
    assert rider is not None
    return observation.vehicle_id, rider


async def _execute(
    session: AsyncSession,
    actor: UUID,
    command: OperationalCommand,
    now: datetime,
    expires_at: datetime,
    clock: Callable[[], datetime] | None = None,
) -> UUID:
    result = command.client_command_id
    if isinstance(command, ConfigureRegion):
        import h3  # type: ignore[import-untyped]

        if any(not h3.is_valid_cell(c) or h3.get_resolution(c) != 7 for c in command.cells):
            raise OperationalInputError("CANONICAL_H3_CELLS_REQUIRED")
        if (
            command.default_warehouse_id not in command.receiving_point_ids
            or not command.fleet.regional
        ):
            raise OperationalInputError("AUTHORIZED_WAREHOUSE_AND_REGIONAL_POLICY_REQUIRED")
        points = list(
            await session.scalars(
                select(ReceivingPoint)
                .where(ReceivingPoint.receiving_point_id.in_(command.receiving_point_ids))
                .with_for_update()
            )
        )
        if len(points) != len(set(command.receiving_point_ids)) or any(
            p.status != "ACTIVE" for p in points
        ):
            raise OperationalInputError("ACTIVE_AUTHORIZED_RECEIVING_POINTS_REQUIRED")
        session.add(
            OperatingRegion(
                region_id=result,
                name=command.name,
                default_warehouse_id=command.default_warehouse_id,
                recorded_by=actor,
                policy={
                    "fleet": command.fleet.model_dump(mode="json"),
                    "safety_percent": command.safety_percent,
                    "minimum_pickup_seconds": command.minimum_pickup_seconds,
                    "minimum_discharge_seconds": command.minimum_discharge_seconds,
                },
            )
        )
        await session.flush()
        session.add_all([RegionCell(cell_id=c, region_id=result) for c in set(command.cells)])
        session.add_all(
            [RegionVehicle(vehicle_id=v, region_id=result) for v in set(command.vehicle_ids)]
        )
        session.add_all(
            [
                RegionReceivingPoint(receiving_point_id=p, region_id=result)
                for p in set(command.receiving_point_ids)
            ]
        )
    elif isinstance(command, ResumeRoute):
        revision = await session.get(RouteRevision, command.revision_id)
        root = await session.get(VehicleRoute, revision.route_id) if revision else None
        if root is None or revision is None:
            raise RouteAuthorityError("ROUTE_UNAVAILABLE")
        shift = await session.get(OperatingShift, root.shift_id)
        assert shift is not None
        now = await lock_at_time(
            session, [shift.rider_id], [shift.vehicle_id], now=now, clock=clock
        )
        await session.refresh(root)
        vehicle = await session.get(OperatingVehicle, shift.vehicle_id)
        assert vehicle is not None
        if (
            root.version != revision.version
            or root.status != "RECOVERY_REQUIRED"
            or vehicle.blocked_reason is not None
        ):
            raise RouteAuthorityError("SAFE_CURRENT_RECOVERY_REQUIRED")
        result = await authorize_resume(session, root, actor, now)
    elif isinstance(command, AuthorizeReattempt):
        requests = await lock_booking_population(session, [command.request_id])
        booking = requests[command.request_id]
        pickup = await session.scalar(
            select(PickupExecution).where(PickupExecution.request_id == booking.request_id)
        )
        if (
            pickup is None
            or pickup.status != "PENDING_ASSIGNMENT"
            or booking.status not in ("ACCEPTED", "PLANNED")
        ):
            raise RouteAuthorityError("PENDING_REATTEMPT_REQUIRED")
        from tirodhan.modules.pickups.models import PickupAttempt

        from .physical import appointment_window

        if (
            await appointment_window(
                session, booking.request_id, booking.slot_start, booking.slot_end
            )
        )[1] < now:
            raise RouteAuthorityError("CUSTOMER_APPOINTMENT_CONSENT_REQUIRED")
        attempt = await session.scalar(
            select(PickupAttempt)
            .where(
                PickupAttempt.pickup_execution_id == pickup.pickup_execution_id,
                PickupAttempt.outcome == "NOT_COLLECTED",
            )
            .order_by(PickupAttempt.attempt_number.desc())
            .limit(1)
        )
        if attempt is None:
            raise RouteAuthorityError("UNSUCCESSFUL_ATTEMPT_REQUIRED")
        from .orchestration_models import ReattemptAuthorization

        session.add(
            ReattemptAuthorization(
                authorization_id=result,
                request_id=booking.request_id,
                after_attempt_id=attempt.pickup_attempt_id,
                authorized_by=actor,
                authorized_at=now,
            )
        )
    elif isinstance(command, ObservePhysicalLoad):
        requests = await lock_booking_population(session, [command.request_id])
        booking = requests[command.request_id]
        if (
            booking.status not in ("ACCEPTED", "PLANNED")
            or not booking.created_at <= command.captured_at <= now
        ):
            raise RouteAuthorityError("CURRENT_PHYSICAL_LOAD_EVIDENCE_REQUIRED")
        pickup = await session.scalar(
            select(PickupExecution).where(PickupExecution.request_id == booking.request_id)
        )
        if pickup and pickup.status == "COLLECTED":
            current_vehicle, rider = await custodian(session, booking.request_id)
            now = await lock_at_time(
                session, [rider], [current_vehicle], authorize=False, now=now, clock=clock
            )
            await _assert_unhanded(session, booking.request_id)
            vehicle = await session.get(OperatingVehicle, current_vehicle)
            assert vehicle is not None
            vehicle.safety_version += 1
        from .orchestration_models import PhysicalLoadObservation

        capture = EvidenceCapture(
            evidence_capture_id=new_uuid7(),
            client_capture_id=command.client_capture_id,
            captured_by_user_id=actor,
            captured_at=command.captured_at,
            created_at=now,
        )
        session.add(capture)
        await session.flush()
        session.add(
            PhysicalLoadObservation(
                observation_id=result,
                request_id=booking.request_id,
                mass_g=command.mass_g,
                volume_ml=command.volume_ml,
                capture_id=capture.evidence_capture_id,
                recorded_by=actor,
                observed_at=now,
            )
        )
    elif isinstance(command, AcknowledgeRoute):
        revision = await session.get(RouteRevision, command.revision_id)
        root = await session.get(VehicleRoute, revision.route_id) if revision else None
        if root is None:
            raise RouteAuthorityError("ROUTE_UNAVAILABLE")
        shift = await _shift(session, root.shift_id, actor, now, clock)
        await session.refresh(root)
        now = max(now, clock()) if clock else now
        assert revision is not None
        if root.version != revision.version or root.status == "CLOSED":
            raise RouteAuthorityError("ROUTE_REVISION_STALE")
        if await session.get(RouteAcknowledgment, revision.revision_id) is None:
            session.add(
                RouteAcknowledgment(
                    revision_id=revision.revision_id,
                    rider_id=shift.rider_id,
                    command_id=result,
                    acknowledged_at=now,
                )
            )
        result = revision.revision_id
    elif isinstance(command, (DeclareBreak, ConsentExtension)):
        shift = await _shift(
            session,
            command.shift_id,
            actor,
            now,
            clock,
            allow_expired=isinstance(command, ConsentExtension),
        )
        end = await effective_shift_end(session, shift.shift_id, shift.ends_at)
        if isinstance(command, DeclareBreak):
            if not now <= command.starts_at < command.ends_at <= end:
                raise OperationalInputError("BREAK_OUTSIDE_SHIFT")
            overlap = await session.scalar(
                select(OperatingBreak.break_id).where(
                    OperatingBreak.shift_id == shift.shift_id,
                    OperatingBreak.starts_at < command.ends_at,
                    OperatingBreak.ends_at > command.starts_at,
                )
            )
            if overlap:
                raise OperationalInputError("BREAK_OVERLAP")
            active = await session.scalar(
                select(RouteTask.task_id)
                .join(VehicleRoute)
                .where(VehicleRoute.vehicle_id == shift.vehicle_id, RouteTask.state == "STARTED")
            )
            if active and command.starts_at <= now:
                raise OperationalInputError("FINISH_ACTIVE_SERVICE_BEFORE_BREAK")
            session.add(
                OperatingBreak(
                    break_id=result,
                    shift_id=shift.shift_id,
                    starts_at=command.starts_at,
                    ends_at=command.ends_at,
                    recorded_by=actor,
                )
            )
        else:
            if not end < command.ends_at <= shift.starts_at + timedelta(days=1):
                raise OperationalInputError("EXTENSION_REQUIRES_BOUNDED_AVAILABILITY")
            session.add(
                ShiftExtensionConsent(
                    consent_id=result,
                    shift_id=shift.shift_id,
                    rider_id=actor,
                    ends_at=command.ends_at,
                    consented_at=now,
                )
            )
    elif isinstance(command, AuthorizeExtension):
        consent = await session.get(ShiftExtensionConsent, command.consent_id)
        if consent is None:
            raise OperationalInputError("RIDER_EXTENSION_CONSENT_REQUIRED")
        shift = await _shift(
            session, consent.shift_id, consent.rider_id, now, clock, allow_expired=True
        )
        now = max(now, clock()) if clock else now
        if consent.ends_at <= now or (now - consent.consented_at).total_seconds() > 300:
            raise OperationalInputError("FRESH_RIDER_AVAILABILITY_REQUIRED")
        vehicle = await session.get(OperatingVehicle, shift.vehicle_id)
        assert vehicle is not None
        if vehicle.blocked_reason in ("BREAKDOWN", "SAFETY"):
            raise OperationalInputError("SAFE_EXTENSION_REQUIRED")
        session.add(
            ShiftExtension(
                extension_id=result,
                shift_id=shift.shift_id,
                ends_at=consent.ends_at,
                rider_confirmed_at=consent.consented_at,
                authorized_by=actor,
                authorized_at=now,
                safe_conditions_confirmed=command.safe_conditions_confirmed,
            )
        )
    elif isinstance(command, ConsentAppointment):
        requests = await lock_booking_population(session, [command.request_id])
        booking = requests[command.request_id]
        reservation = await session.scalar(
            select(CapacityReservation)
            .where(CapacityReservation.reservation_id == command.reservation_id)
            .with_for_update()
        )
        pickup = await session.scalar(
            select(PickupExecution).where(PickupExecution.request_id == booking.request_id)
        )
        if (
            booking.customer_id != actor
            or booking.status not in ("ACCEPTED", "PLANNED")
            or (pickup is not None and pickup.status != "PENDING_ASSIGNMENT")
        ):
            raise RouteAuthorityError("BOOKING_NOT_RESCHEDULABLE")
        if (
            reservation is None
            or reservation.customer_id != actor
            or reservation.context_id != booking.serviceability_context_id
            or reservation.expires_at <= now
            or reservation.request_id is not None
        ):
            raise OperationalInputError("NEW_APPOINTMENT_CAPACITY_REQUIRED")
        reservation.request_id = booking.request_id
        result = new_uuid7()
        session.add(
            AppointmentConsent(
                consent_id=result,
                request_id=booking.request_id,
                customer_id=actor,
                slot_start=reservation.slot_start,
                slot_end=reservation.slot_end,
                reservation_id=reservation.reservation_id,
                consented_at=now,
            )
        )
    elif isinstance(command, AuthorizeDischarge):
        requests = await lock_booking_population(session, [command.request_id])
        booking = requests[command.request_id]
        mapping = await session.get(RegionCell, booking.cell_id)
        allowed = (
            await session.get(RegionReceivingPoint, (mapping.region_id, command.receiving_point_id))
            if mapping
            else None
        )
        point = await session.get(ReceivingPoint, command.receiving_point_id)
        if (
            allowed is None
            or point is None
            or point.status != "ACTIVE"
            or not point.official_identifier
            or command.discharge_deadline <= now
        ):
            raise OperationalInputError("AUTHORIZED_FUTURE_DISCHARGE_REQUIRED")
        pickup = await session.scalar(
            select(PickupExecution).where(PickupExecution.request_id == booking.request_id)
        )
        vehicle_id = None
        if pickup is not None and pickup.status == "COLLECTED":
            vehicle_id, rider = await custodian(session, command.request_id)
            now = await lock_at_time(session, [rider], [vehicle_id], now=now, clock=clock)
            await _assert_unhanded(session, command.request_id)
        elif booking.status not in ("ACCEPTED", "PLANNED"):
            raise RouteAuthorityError("BOOKING_NOT_OPERATIONAL")
        else:
            from tirodhan.modules.dispatch.models import RiderAssignmentItem

            owned_root = await session.scalar(
                select(VehicleRoute)
                .join(
                    RiderAssignmentItem,
                    RiderAssignmentItem.assignment_id == VehicleRoute.assignment_id,
                )
                .join(PickupExecution)
                .where(
                    PickupExecution.request_id == booking.request_id,
                    RiderAssignmentItem.released_at.is_(None),
                    VehicleRoute.status != "CLOSED",
                )
            )
            if owned_root:
                owner_shift = await session.get(OperatingShift, owned_root.shift_id)
                assert owner_shift is not None
                vehicle_id = owned_root.vehicle_id
                now = await lock_at_time(
                    session, [owner_shift.rider_id], [vehicle_id], now=now, clock=clock
                )
        result = new_uuid7()
        session.add(
            DischargeAuthorization(
                authorization_id=result,
                request_id=command.request_id,
                receiving_point_id=command.receiving_point_id,
                discharge_deadline=command.discharge_deadline,
                authorized_by=actor,
                authorized_at=now,
            )
        )
        tasks = await session.scalars(
            select(RouteTask)
            .join(PickupExecution)
            .where(
                PickupExecution.request_id == command.request_id,
                RouteTask.kind == "DISCHARGE",
                RouteTask.state.in_(("PENDING", "STARTED")),
            )
        )
        for task in tasks:
            task.state = "CANCELLED"
        for root in await session.scalars(
            select(VehicleRoute).where(
                VehicleRoute.vehicle_id == vehicle_id, VehicleRoute.status != "CLOSED"
            )
        ):
            root.status = "RECOVERY_REQUIRED"
            vehicle = await session.get(OperatingVehicle, vehicle_id)
            assert vehicle is not None
            vehicle.safety_version += 1
            await session.flush()
            await authorize_resume(session, root, actor, now)
    elif isinstance(command, PrepareTransfer):
        await lock_booking_population(session, [command.request_id])
        current_vehicle, donor = await custodian(session, command.request_id)
        target = await session.get(OperatingShift, command.to_shift_id)
        if (
            target is None
            or current_vehicle != command.from_vehicle_id
            or target.vehicle_id == current_vehicle
        ):
            raise RouteAuthorityError("TRANSFER_RESOURCES_CHANGED")
        now = await lock_at_time(
            session,
            sorted([donor, target.rider_id]),
            sorted([current_vehicle, target.vehicle_id]),
            now=now,
            clock=clock,
            authorize=False,
        )
        await _assert_unhanded(session, command.request_id)
        busy_service = await session.scalar(
            select(RouteTask.task_id)
            .join(VehicleRoute)
            .where(
                VehicleRoute.vehicle_id.in_((current_vehicle, target.vehicle_id)),
                RouteTask.state == "STARTED",
            )
        )
        from .models import OperatingShift as PendingShift

        other_transfer = await session.scalar(
            select(CargoTransferIntent.intent_id)
            .join(PendingShift, PendingShift.shift_id == CargoTransferIntent.to_shift_id)
            .where(
                CargoTransferIntent.resolved_at.is_(None),
                (CargoTransferIntent.from_vehicle_id.in_((current_vehicle, target.vehicle_id)))
                | (PendingShift.vehicle_id.in_((current_vehicle, target.vehicle_id))),
            )
        )
        if busy_service or other_transfer:
            raise RouteAuthorityError("FINISH_ACTIVE_SERVICE_OR_TRANSFER_FIRST")
        await _shift(session, target.shift_id, target.rider_id, now, clock)
        if await session.scalar(
            select(CargoTransferIntent.intent_id).where(
                CargoTransferIntent.request_id == command.request_id,
                CargoTransferIntent.resolved_at.is_(None),
            )
        ):
            raise RouteAuthorityError("TRANSFER_ALREADY_IN_PROGRESS")
        from .physical import cargo_load, effective_manifest

        manifest = await session.get(RoutingManifest, command.request_id)
        assert manifest is not None
        measured = await effective_manifest(session, manifest)
        load = await cargo_load(session, target.vehicle_id)
        receiving_vehicle = await session.get(OperatingVehicle, target.vehicle_id)
        assert receiving_vehicle is not None
        if (
            receiving_vehicle.blocked_reason is not None
            or load[0] + measured.mass_g > receiving_vehicle.mass_capacity_g
            or load[1] + measured.volume_ml > receiving_vehicle.volume_capacity_ml
        ):
            raise RouteAuthorityError("TRANSFER_CAPACITY_UNAVAILABLE")
        session.add(
            CargoTransferIntent(
                intent_id=result,
                request_id=command.request_id,
                from_vehicle_id=current_vehicle,
                to_shift_id=target.shift_id,
                authorized_by=actor,
                expires_at=min(expires_at, now + timedelta(minutes=15)),
                created_at=now,
            )
        )
    elif isinstance(
        command, (TransferReceipt, WitnessTransferReceipt, CompleteTransfer, AbortTransfer)
    ):
        intent = await session.get(CargoTransferIntent, command.intent_id)
        if intent is None or intent.resolved_at is not None:
            raise RouteAuthorityError("TRANSFER_INTENT_EXPIRED")
        await lock_booking_population(session, [intent.request_id])
        current_vehicle, donor = await custodian(session, intent.request_id)
        target = await session.get(OperatingShift, intent.to_shift_id)
        assert target is not None
        now = await lock_at_time(
            session,
            sorted([donor, target.rider_id]),
            sorted([current_vehicle, target.vehicle_id]),
            now=now,
            clock=clock,
            authorize=False,
        )
        await session.refresh(intent)
        if intent.resolved_at is not None:
            raise RouteAuthorityError("TRANSFER_ALREADY_RESOLVED")
        if current_vehicle != intent.from_vehicle_id:
            raise RouteAuthorityError("TRANSFER_CUSTODY_CHANGED")
        await _assert_unhanded(session, intent.request_id)
        if isinstance(command, AbortTransfer):
            if (
                command.scanned_request_id != intent.request_id
                or not intent.created_at <= command.captured_at <= now
            ):
                raise RouteAuthorityError("TRANSFER_ABORT_EVIDENCE_INVALID")
            capture = EvidenceCapture(
                client_capture_id=command.client_capture_id,
                captured_by_user_id=actor,
                captured_at=command.captured_at,
                created_at=now,
            )
            session.add(capture)
            await session.flush([capture])
            intent.resolved_at, intent.resolution, intent.resolved_by = now, "ABORTED", actor
            intent.resolution_evidence_id = capture.evidence_capture_id
            await resume_transfer_resources(
                session, (current_vehicle, target.vehicle_id), actor, now
            )
            return intent.intent_id
        if isinstance(command, (TransferReceipt, WitnessTransferReceipt)):
            witness = isinstance(command, WitnessTransferReceipt)
            receipt_rider = donor if witness else actor
            if (
                (not witness and actor not in (donor, target.rider_id))
                or command.scanned_request_id != intent.request_id
                # A timed-out intent still holds both vehicles. Late physical facts must
                # remain recordable; fresh manager confirmation rechecks live authority,
                # custody, receiving duty and capacity before resolving the handoff.
                or not intent.created_at <= command.captured_at <= now
            ):
                raise RouteAuthorityError("TRANSFER_RECEIPT_INVALID")
            existing = await session.get(CargoTransferReceipt, (intent.intent_id, receipt_rider))
            if existing:
                return existing.capture_id
            capture = EvidenceCapture(
                evidence_capture_id=new_uuid7(),
                client_capture_id=result,
                captured_by_user_id=actor,
                captured_at=command.captured_at,
                created_at=now,
            )
            session.add(capture)
            await session.flush()
            session.add(
                CargoTransferReceipt(
                    intent_id=intent.intent_id,
                    rider_id=receipt_rider,
                    capture_id=capture.evidence_capture_id,
                    witnessed_by=actor if witness else None,
                )
            )
            return capture.evidence_capture_id
        await _shift(session, target.shift_id, target.rider_id, now, clock)
        captures = {
            r.rider_id: r.capture_id
            for r in await session.scalars(
                select(CargoTransferReceipt).where(
                    CargoTransferReceipt.intent_id == intent.intent_id
                )
            )
        }
        if donor not in captures or target.rider_id not in captures:
            raise RouteAuthorityError("BOTH_PHYSICAL_TRANSFER_RECEIPTS_REQUIRED")
        from .fleet_models import FleetControl
        from .rolling import FleetPolicy

        control = await session.get(FleetControl, "COMMITTED_FLEET")
        if control is None:
            raise OperationalInputError("FLEET_POLICY_REQUIRED")
        # Use the same custody projection in this transaction, never a second transaction.
        from .snapshot import _read_sources

        policy = FleetPolicy.model_validate(control.configuration)
        sources = await _read_sources(session, policy.snapshot_configuration(now))
        manifests = [
            r
            for r in sources["requests"]
            if r["pickup_status"] == "COLLECTED" and r["cargo_vehicle_id"] == target.vehicle_id
        ]
        manifest = await session.get(RoutingManifest, intent.request_id)
        assert manifest is not None
        from .physical import effective_manifest

        manifest = await effective_manifest(session, manifest)
        vehicle = await session.get(OperatingVehicle, target.vehicle_id)
        assert manifest is not None and vehicle is not None
        if (
            vehicle.blocked_reason is not None
            or sum(r["mass_g"] for r in manifests) + manifest.mass_g > vehicle.mass_capacity_g
            or sum(r["volume_ml"] for r in manifests) + manifest.volume_ml
            > vehicle.volume_capacity_ml
        ):
            raise RouteAuthorityError("TRANSFER_CAPACITY_UNAVAILABLE")
        from sqlalchemy import func

        prior_sequence = await session.scalar(
            select(func.coalesce(func.max(CargoTransfer.sequence), 0)).where(
                CargoTransfer.request_id == intent.request_id
            )
        )
        assert prior_sequence is not None
        sequence = prior_sequence + 1
        session.add(
            CargoTransfer(
                transfer_id=result,
                sequence=sequence,
                intent_id=intent.intent_id,
                request_id=intent.request_id,
                from_vehicle_id=current_vehicle,
                to_vehicle_id=target.vehicle_id,
                from_rider_id=donor,
                to_rider_id=target.rider_id,
                donor_evidence_id=captures[donor],
                receiver_evidence_id=captures[target.rider_id],
                authorized_by=intent.authorized_by,
                confirmed_by=actor,
                transferred_at=now,
            )
        )
        for task in await session.scalars(
            select(RouteTask)
            .join(PickupExecution)
            .where(
                PickupExecution.request_id == intent.request_id,
                RouteTask.kind == "DISCHARGE",
                RouteTask.state.in_(("PENDING", "STARTED")),
            )
        ):
            task.state = "CANCELLED"
        from tirodhan.modules.dispatch.models import RiderAvailability

        await session.flush()
        intent.resolved_at, intent.resolution, intent.resolved_by = now, "TRANSFERRED", actor
        availability = await session.get(RiderAvailability, target.rider_id)
        assert availability is not None
        availability.work_state = "BUSY"
        availability.version += 1
        vehicle.version += 1
        vehicle.safety_version += 1
        await session.flush()
        receiver_root = await session.scalar(
            select(VehicleRoute).where(
                VehicleRoute.vehicle_id == vehicle.vehicle_id, VehicleRoute.status != "CLOSED"
            )
        )
        if receiver_root is not None:
            receiver_root.status = "RECOVERY_REQUIRED"
            await authorize_resume(session, receiver_root, actor, now)
        donor_root = await session.scalar(
            select(VehicleRoute).where(
                VehicleRoute.vehicle_id == current_vehicle, VehicleRoute.status != "CLOSED"
            )
        )
        donor_vehicle = await session.get(OperatingVehicle, current_vehicle)
        assert donor_vehicle is not None
        donor_vehicle.safety_version += 1
        await session.flush()
        await resume_transfer_resources(session, (current_vehicle, target.vehicle_id), actor, now)
        if donor_root is not None:
            from .execution import finish_if_empty

            await finish_if_empty(session, donor_root, now)
    elif isinstance(command, AcceptAdmissionOffer):
        from .orchestration_models import (
            AdmissionFlexibleCommitment,
            AdmissionFlexibleOffer,
            CapacityAdmissionRequest,
        )

        offer = await session.get(AdmissionFlexibleOffer, command.offer_id)
        if offer is None:
            raise RouteAuthorityError("FLEXIBLE_OFFER_UNAVAILABLE")
        admission = await session.scalar(
            select(CapacityAdmissionRequest)
            .where(CapacityAdmissionRequest.admission_id == offer.admission_id)
            .with_for_update()
        )
        assert admission is not None
        shift = await session.get(OperatingShift, offer.shift_id)
        if shift is None or shift.rider_id != actor or shift.resource_kind != "FLEXIBLE":
            raise RouteAuthorityError("WRONG_FLEXIBLE_SHIFT")
        now = await lock_at_time(session, [actor], [shift.vehicle_id], now=now, clock=clock)
        if offer.expires_at <= now or admission.expires_at <= now or shift.closed_at is not None:
            raise RouteAuthorityError("FLEXIBLE_OFFER_EXPIRED")
        from tirodhan.modules.dispatch.models import RiderAvailability

        availability = await session.get(RiderAvailability, actor)
        if availability is None or availability.availability_intent != "AVAILABLE":
            raise RouteAuthorityError("FLEXIBLE_RIDER_UNAVAILABLE")
        admission_commitment = await session.get(
            AdmissionFlexibleCommitment, admission.admission_id
        )
        if admission_commitment is not None and admission_commitment.accepted_by != actor:
            raise RouteAuthorityError("FLEXIBLE_OFFER_ALREADY_WON")
        if admission_commitment is None:
            if admission.status != "NEEDS_ACCEPTANCE":
                raise RouteAuthorityError("ADMISSION_NOT_WAITING_FOR_ACCEPTANCE")
            session.add(
                AdmissionFlexibleCommitment(
                    admission_id=admission.admission_id,
                    offer_id=offer.offer_id,
                    shift_id=shift.shift_id,
                    accepted_by=actor,
                    accepted_at=now,
                )
            )
            admission.status, admission.failure_code = "PENDING", None
        result = offer.offer_id
    else:
        assert isinstance(command, AcceptFlexibleOffer)
        paid_offer = await session.get(FlexibleRouteOffer, command.offer_id)
        if paid_offer is None:
            raise RouteAuthorityError("FLEXIBLE_OFFER_UNAVAILABLE")
        requests = await lock_booking_population(session, [paid_offer.request_id])
        shift = await _shift(session, paid_offer.shift_id, actor, now, clock)
        now = max(now, clock()) if clock else now
        booking = requests[paid_offer.request_id]
        if (
            shift.resource_kind != "FLEXIBLE"
            or paid_offer.expires_at <= now
            or booking.status not in ("ACCEPTED", "PLANNED")
        ):
            raise RouteAuthorityError("FLEXIBLE_OFFER_EXPIRED")
        from tirodhan.modules.dispatch.models import RiderAssignmentItem, RiderAvailability

        pickup = await session.scalar(
            select(PickupExecution).where(PickupExecution.request_id == booking.request_id)
        )
        if pickup and await session.scalar(
            select(RiderAssignmentItem.assignment_id).where(
                RiderAssignmentItem.pickup_execution_id == pickup.pickup_execution_id,
                RiderAssignmentItem.released_at.is_(None),
            )
        ):
            raise RouteAuthorityError("BOOKING_ALREADY_ASSIGNED")
        availability = await session.get(RiderAvailability, actor)
        if availability is None or availability.availability_intent != "AVAILABLE":
            raise RouteAuthorityError("FLEXIBLE_RIDER_UNAVAILABLE")
        commitment = await session.get(FlexibleCommitment, booking.request_id)
        if commitment is not None and commitment.accepted_by != actor:
            raise RouteAuthorityError("FLEXIBLE_OFFER_ALREADY_WON")
        if commitment is None:
            session.add(
                FlexibleCommitment(
                    request_id=booking.request_id,
                    shift_id=shift.shift_id,
                    offer_id=paid_offer.offer_id,
                    accepted_by=actor,
                    accepted_at=now,
                )
            )
        paid_offer.accepted_at, paid_offer.accepted_by = now, actor
        result = paid_offer.offer_id
    return result


async def _assert_unhanded(session: AsyncSession, request_id: UUID) -> None:
    pickup = await session.scalar(
        select(PickupExecution).where(PickupExecution.request_id == request_id).with_for_update()
    )
    if (
        pickup is None
        or pickup.status != "COLLECTED"
        or await session.scalar(
            select(HandoverEventItem.handover_event_id).where(
                HandoverEventItem.pickup_execution_id == pickup.pickup_execution_id,
                HandoverEventItem.status == "VALIDATED",
            )
        )
    ):
        raise RouteAuthorityError("UNHANDED_COLLECTED_CARGO_REQUIRED")


async def authorize_resume(
    session: AsyncSession, root: VehicleRoute, actor: UUID, now: datetime
) -> UUID:
    from .orchestration_models import RouteResumeAuthorization

    revision_id = await session.scalar(
        select(RouteRevision.revision_id).where(
            RouteRevision.route_id == root.route_id, RouteRevision.version == root.version
        )
    )
    vehicle = await session.get(OperatingVehicle, root.vehicle_id)
    assert revision_id is not None and vehicle is not None
    authorization = RouteResumeAuthorization(
        revision_id=revision_id,
        vehicle_safety_version=vehicle.safety_version,
        authorized_by=actor,
        authorized_at=now,
    )
    session.add(authorization)
    await session.flush()
    return authorization.authorization_id


async def lock_at_time(
    session: AsyncSession,
    riders: list[UUID],
    vehicles: list[UUID],
    *,
    now: datetime,
    clock: Callable[[], datetime] | None,
    authorize: bool = True,
) -> datetime:
    await lock_resources(session, riders, vehicles, authorize=authorize)
    return max(now, clock()) if clock is not None else now


async def resume_transfer_resources(
    session: AsyncSession, vehicles: tuple[UUID, ...], actor: UUID, now: datetime
) -> None:
    await session.flush()
    for root in await session.scalars(
        select(VehicleRoute).where(
            VehicleRoute.vehicle_id.in_(vehicles), VehicleRoute.status == "RECOVERY_REQUIRED"
        )
    ):
        vehicle = await session.get(OperatingVehicle, root.vehicle_id)
        assert vehicle is not None
        if vehicle.blocked_reason is None:
            await authorize_resume(session, root, actor, now)
