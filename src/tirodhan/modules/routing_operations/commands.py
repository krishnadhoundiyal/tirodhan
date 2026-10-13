from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter, model_validator
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.db.values import utc_now
from tirodhan.modules.dispatch.models import RiderAssignment, RiderProfile
from tirodhan.modules.identity.models import AppUser, UserRole
from tirodhan.modules.reliability.primitives import (
    claim_idempotency_record,
    complete_idempotency_record,
)

from .models import (
    CargoVehicleObservation,
    OperatingShift,
    OperatingVehicle,
    ReceivingWindow,
    RoutingManifest,
    VehiclePosition,
)

Positive = Annotated[int, Field(strict=True, gt=0, le=2**31 - 1)]
Seconds = Annotated[int, Field(strict=True, ge=0, le=86400)]


class OperationalInputError(ValueError):
    pass


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    client_command_id: UUID


class VehicleCreate(Command):
    operation: Literal["VEHICLE"] = "VEHICLE"
    mass_capacity_g: Positive
    volume_capacity_ml: Positive
    profile_version: Annotated[str, Field(min_length=1, max_length=100)]
    end_receiving_point_id: UUID


class ShiftOpen(Command):
    operation: Literal["SHIFT_OPEN"] = "SHIFT_OPEN"
    vehicle_id: UUID
    rider_id: UUID
    resource_kind: Literal["COMMITTED", "FLEXIBLE"]
    commitment_id: UUID
    starts_at: AwareDatetime
    ends_at: AwareDatetime

    @model_validator(mode="after")
    def ordered(self) -> ShiftOpen:
        if self.ends_at <= self.starts_at:
            raise ValueError("invalid shift window")
        return self


class ShiftClose(Command):
    operation: Literal["SHIFT_CLOSE"] = "SHIFT_CLOSE"
    shift_id: UUID


class PositionReport(Command):
    operation: Literal["POSITION"] = "POSITION"
    vehicle_id: UUID
    longitude: Annotated[float, Field(ge=-180, le=180, allow_inf_nan=False)]
    latitude: Annotated[float, Field(ge=-90, le=90, allow_inf_nan=False)]
    observed_at: AwareDatetime


class CargoObserve(Command):
    operation: Literal["CARGO_OBSERVE"] = "CARGO_OBSERVE"
    request_id: UUID
    vehicle_id: UUID


class ManifestDeclare(Command):
    operation: Literal["MANIFEST"] = "MANIFEST"
    request_id: UUID
    mass_g: Positive
    volume_ml: Positive
    pickup_seconds: Seconds
    discharge_seconds: Seconds
    discharge_deadline: AwareDatetime
    receiving_point_id: UUID


class WindowDeclare(Command):
    operation: Literal["RECEIVING_WINDOW"] = "RECEIVING_WINDOW"
    receiving_point_id: UUID
    starts_at: AwareDatetime
    ends_at: AwareDatetime

    @model_validator(mode="after")
    def ordered(self) -> WindowDeclare:
        if self.ends_at <= self.starts_at:
            raise ValueError("invalid receiving window")
        return self


ResourceCommand = Annotated[
    VehicleCreate
    | ShiftOpen
    | ShiftClose
    | PositionReport
    | CargoObserve
    | ManifestDeclare
    | WindowDeclare,
    Field(discriminator="operation"),
]
COMMAND_CODEC: TypeAdapter[ResourceCommand] = TypeAdapter(ResourceCommand)


async def require_live_role(session: AsyncSession, user_id: UUID, role: str) -> None:
    # Same point-in-time live authorization rule as ADR-007; no token role authority.
    authorized = await session.scalar(
        select(
            exists().where(
                AppUser.user_id == user_id,
                AppUser.status == "ACTIVE",
                UserRole.user_id == AppUser.user_id,
                UserRole.role_code == role,
                UserRole.revoked_at.is_(None),
            )
        )
    )
    if not authorized:
        raise OperationalInputError("LIVE_AUTHORIZATION_REQUIRED")


async def execute_resource_command(
    factory: async_sessionmaker[AsyncSession],
    *,
    manager_id: UUID,
    command: ResourceCommand,
    expires_at: datetime,
    now: datetime | None = None,
) -> UUID:
    now = now or utc_now()
    fingerprint = hashlib.sha256(COMMAND_CODEC.dump_json(command)).digest()
    async with factory() as session, session.begin():
        await require_live_role(session, manager_id, "MANAGER")
        claim = await claim_idempotency_record(
            session,
            scope=f"routing.resources:{manager_id}",
            idempotency_key=str(command.client_command_id),
            request_fingerprint=fingerprint,
            expires_at=expires_at,
            now=now,
        )
        if claim.record.status == "COMPLETED":
            if claim.record.result_resource_id is None:
                raise OperationalInputError("MISSING_COMMAND_RESULT")
            return claim.record.result_resource_id
        if not claim.created:
            raise OperationalInputError("COMMAND_IN_PROGRESS")
        result = await _apply(session, manager_id, command, now)
        await complete_idempotency_record(
            session,
            claim.record,
            result_resource_id=result,
            result_status_code=200,
            completed_at=now,
        )
        return result


async def _apply(
    session: AsyncSession, actor: UUID, command: ResourceCommand, now: datetime
) -> UUID:
    shift: OperatingShift | None
    audit = {"recorded_by": actor, "created_at": now}
    if isinstance(command, VehicleCreate):
        created_vehicle = OperatingVehicle(
            **command.model_dump(exclude={"operation", "client_command_id"}), **audit
        )
        session.add(created_vehicle)
        await session.flush()
        return created_vehicle.vehicle_id
    if isinstance(command, ManifestDeclare):
        # PK arbitrates different commands; a manifest is immutable historical load reservation.
        if await session.get(RoutingManifest, command.request_id):
            raise OperationalInputError("MANIFEST_ALREADY_DECLARED")
        session.add(
            RoutingManifest(
                **command.model_dump(exclude={"operation", "client_command_id"}), **audit
            )
        )
        await session.flush()
        return command.request_id
    if isinstance(command, WindowDeclare):
        window = ReceivingWindow(
            **command.model_dump(exclude={"operation", "client_command_id"}), **audit
        )
        session.add(window)
        await session.flush()
        return window.window_id
    if isinstance(command, ShiftClose):
        shift = await session.get(OperatingShift, command.shift_id)
        if shift is None:
            raise OperationalInputError("SHIFT_NOT_FOUND")
        vehicle_id = shift.vehicle_id
    else:
        vehicle_id = command.vehicle_id
    vehicle = await session.scalar(
        select(OperatingVehicle).where(OperatingVehicle.vehicle_id == vehicle_id).with_for_update()
    )
    if vehicle is None:
        raise OperationalInputError("VEHICLE_NOT_FOUND")
    if isinstance(command, CargoObserve):
        from tirodhan.modules.pickups.models import PickupAttempt
        from tirodhan.modules.planning.models import PickupExecution

        observation = await session.get(CargoVehicleObservation, command.request_id)
        if observation is not None:
            raise OperationalInputError("CARGO_ALREADY_OBSERVED")
        collected = (
            await session.execute(
                select(PickupAttempt.pickup_attempt_id, RiderAssignment.rider_id)
                .join(
                    RiderAssignment,
                    RiderAssignment.assignment_id == PickupAttempt.rider_assignment_id,
                )
                .join(
                    PickupExecution,
                    PickupExecution.pickup_execution_id == PickupAttempt.pickup_execution_id,
                )
                .where(
                    PickupExecution.request_id == command.request_id,
                    PickupAttempt.outcome == "COLLECTED",
                )
            )
        ).one_or_none()
        shift = await session.scalar(
            select(OperatingShift).where(
                OperatingShift.vehicle_id == vehicle_id, OperatingShift.closed_at.is_(None)
            )
        )
        if collected is None or shift is None or shift.rider_id != collected.rider_id:
            raise OperationalInputError("CARGO_CUSTODIAN_MISMATCH")
        session.add(
            CargoVehicleObservation(
                request_id=command.request_id,
                vehicle_id=vehicle_id,
                pickup_attempt_id=collected.pickup_attempt_id,
                **audit,
            )
        )
        await session.flush()
        vehicle.version += 1
        return command.request_id
    if isinstance(command, PositionReport):
        last = await session.scalar(
            select(VehiclePosition)
            .where(VehiclePosition.vehicle_id == vehicle_id)
            .order_by(VehiclePosition.revision.desc())
            .limit(1)
        )
        if command.observed_at > now or (
            last is not None and command.observed_at <= last.observed_at
        ):
            raise OperationalInputError("POSITION_NOT_MONOTONIC")
        vehicle.version += 1
        position = VehiclePosition(
            vehicle_id=vehicle_id,
            revision=vehicle.version,
            location=f"SRID=4326;POINT({command.longitude} {command.latitude})",
            observed_at=command.observed_at,
            **audit,
        )
        session.add(position)
        await session.flush()
        return position.position_id
    if isinstance(command, ShiftOpen):
        await require_live_role(session, command.rider_id, "RIDER")
        profile = await session.get(RiderProfile, command.rider_id)
        if profile is None or profile.status != "ACTIVE":
            raise OperationalInputError("ACTIVE_RIDER_REQUIRED")
        shift = OperatingShift(
            **command.model_dump(exclude={"operation", "client_command_id"}), **audit
        )
        session.add(shift)
        vehicle.version += 1
        await session.flush()
        return shift.shift_id
    # Closing is not permission to forget cargo or existing work.
    from .snapshot import rider_has_obligations

    shift = await session.scalar(
        select(OperatingShift)
        .where(OperatingShift.shift_id == command.shift_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    assert shift is not None
    if shift.closed_at is None:
        active = await session.scalar(
            select(
                exists().where(
                    RiderAssignment.rider_id == shift.rider_id, RiderAssignment.status == "ACTIVE"
                )
            )
        )
        if active or await rider_has_obligations(session, shift.rider_id):
            raise OperationalInputError("SHIFT_HAS_OBLIGATIONS")
        shift.closed_at = now
        vehicle.version += 1
    return shift.shift_id
