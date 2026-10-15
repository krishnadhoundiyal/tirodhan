from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.db.values import utc_now
from tirodhan.modules.dispatch.models import RiderAssignment
from tirodhan.modules.identity.models import RefreshSession
from tirodhan.modules.reliability.primitives import (
    append_outbox_event,
    claim_idempotency_record,
    complete_idempotency_record,
)

from .authority import RouteAuthorityError, lock_resources
from .dispatch_models import VehicleRoute
from .fleet_models import FleetException
from .models import OperatingShift, OperatingVehicle, VehiclePosition


class TelemetryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    max_age_seconds: int = Field(strict=True, ge=1, le=3600)
    max_accuracy_m: int = Field(strict=True, ge=1, le=1000)
    max_speed_m_per_second: int = Field(strict=True, ge=1, le=100)


class PositionCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    client_command_id: UUID
    shift_id: UUID
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)
    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    observed_at: AwareDatetime
    accuracy_m: int = Field(strict=True, ge=0, le=1000)


async def report_position(
    factory: async_sessionmaker[AsyncSession],
    *,
    rider_id: UUID,
    session_id: UUID,
    command: PositionCommand,
    policy: TelemetryPolicy,
    expires_at: datetime,
    now: datetime | None = None,
) -> UUID:
    now = now or utc_now()
    async with factory() as session, session.begin():
        shift = await session.get(OperatingShift, command.shift_id)
        if shift is None or shift.rider_id != rider_id:
            raise RouteAuthorityError("POSITION_WRONG_SHIFT")
        await lock_resources(session, [rider_id], [shift.vehicle_id])
        await session.refresh(shift)
        login = await session.scalar(
            select(RefreshSession)
            .where(
                RefreshSession.refresh_session_id == session_id,
            )
            .with_for_update(read=True, key_share=True)
        )
        if (
            login is None
            or login.user_id != rider_id
            or login.revoked_at is not None
            or login.expires_at <= now
        ):
            raise RouteAuthorityError("LIVE_SESSION_REQUIRED")
        claim = await claim_idempotency_record(
            session,
            scope=f"routing.position:{rider_id}",
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
        from .regional_projection import effective_shift_end

        if shift.closed_at is not None or not shift.starts_at <= now < await effective_shift_end(
            session, shift.shift_id, shift.ends_at
        ):
            raise RouteAuthorityError("ACTIVE_SHIFT_REQUIRED")
        if (
            not 0 <= (now - command.observed_at).total_seconds() <= policy.max_age_seconds
            or command.accuracy_m > policy.max_accuracy_m
        ):
            raise RouteAuthorityError("POSITION_QUALITY_INVALID")
        last = await session.scalar(
            select(VehiclePosition)
            .where(
                VehiclePosition.vehicle_id == shift.vehicle_id,
            )
            .order_by(VehiclePosition.revision.desc())
            .limit(1)
        )
        location = f"SRID=4326;POINT({command.longitude} {command.latitude})"
        if last is not None:
            delta = (command.observed_at - last.observed_at).total_seconds()
            if delta <= 0:
                raise RouteAuthorityError("POSITION_NOT_MONOTONIC")
            distance = await session.scalar(
                select(
                    func.ST_Distance(
                        VehiclePosition.location,
                        func.ST_GeogFromText(location),
                    )
                ).where(VehiclePosition.position_id == last.position_id)
            )
            assert distance is not None
            if distance > delta * policy.max_speed_m_per_second + command.accuracy_m + (
                last.accuracy_m or 0
            ):
                raise RouteAuthorityError("POSITION_MOVEMENT_IMPLAUSIBLE")
        vehicle = await session.get(OperatingVehicle, shift.vehicle_id)
        assert vehicle is not None
        vehicle.version += 1
        position = VehiclePosition(
            vehicle_id=shift.vehicle_id,
            revision=vehicle.version,
            location=location,
            observed_at=command.observed_at,
            accuracy_m=command.accuracy_m,
            session_id=session_id,
            recorded_by=rider_id,
            created_at=now,
        )
        session.add(position)
        await session.flush([position])
        # Ingestion appends facts; it never enqueues one optimization per GPS sample.
        await complete_idempotency_record(
            session,
            claim.record,
            result_resource_id=position.position_id,
            result_status_code=200,
            completed_at=now,
        )
        return position.position_id


class VehicleIssueCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    client_command_id: UUID
    route_id: UUID
    reason: Literal["BREAKDOWN", "LOAD_UNCERTAIN", "DISCHARGE_DELAY", "SAFETY"]


async def report_vehicle_issue(
    factory: async_sessionmaker[AsyncSession],
    *,
    rider_id: UUID,
    command: VehicleIssueCommand,
    expires_at: datetime,
    now: datetime | None = None,
) -> UUID:
    now = now or utc_now()
    async with factory() as session, session.begin():
        root = await session.get(VehicleRoute, command.route_id)
        if root is None:
            raise RouteAuthorityError("ROUTE_NOT_FOUND")
        assignment = await session.get(RiderAssignment, root.assignment_id)
        if assignment is None or assignment.rider_id != rider_id:
            raise RouteAuthorityError("ROUTE_WRONG_RIDER")
        await lock_resources(session, [rider_id], [root.vehicle_id])
        await session.refresh(root)
        claim = await claim_idempotency_record(
            session,
            scope=f"routing.issue:{rider_id}",
            idempotency_key=str(command.client_command_id),
            request_fingerprint=hashlib.sha256(command.model_dump_json().encode()).digest(),
            expires_at=expires_at,
            now=now,
        )
        if claim.record.status == "COMPLETED":
            assert claim.record.result_resource_id is not None
            return claim.record.result_resource_id
        if not claim.created or root.status == "CLOSED":
            raise RouteAuthorityError("ROUTE_NOT_EXECUTABLE")
        # Freeze allocation/collection; keep every ownership/cargo fact for controlled recovery.
        vehicle = await session.get(OperatingVehicle, root.vehicle_id)
        assert vehicle is not None
        vehicle.blocked_reason = command.reason
        vehicle.safety_version += 1
        vehicle.version += 1
        root.status = "RECOVERY_REQUIRED"
        issue = FleetException(
            route_id=root.route_id, reported_by=rider_id, reason=command.reason, created_at=now
        )
        session.add(issue)
        await session.flush([issue])
        if command.reason == "LOAD_UNCERTAIN":
            from tirodhan.modules.dispatch.models import RiderAssignmentItem
            from tirodhan.modules.planning.models import PickupExecution

            requests = await session.scalars(
                select(PickupExecution.request_id)
                .join(
                    RiderAssignmentItem,
                    RiderAssignmentItem.pickup_execution_id == PickupExecution.pickup_execution_id,
                )
                .where(
                    RiderAssignmentItem.assignment_id == root.assignment_id,
                    RiderAssignmentItem.released_at.is_(None),
                )
            )
            for request_id in requests:
                session.add(
                    FleetException(
                        route_id=root.route_id,
                        request_id=request_id,
                        reported_by=rider_id,
                        reason="LOAD_UNCERTAIN",
                        created_at=now,
                    )
                )
            # Whole-lot handover may still conserve physical custody. Fresh collection
            # and released work cannot reuse an explicitly disputed load reservation.
        await append_outbox_event(
            session,
            event_key=f"fleet-issue:{issue.exception_id}",
            aggregate_type="VehicleRoute",
            aggregate_id=root.route_id,
            event_type="VehicleRouteRecoveryRequired",
            payload={"route_id": str(root.route_id)},
        )
        await complete_idempotency_record(
            session,
            claim.record,
            result_resource_id=issue.exception_id,
            result_status_code=200,
            completed_at=now,
        )
        return issue.exception_id
