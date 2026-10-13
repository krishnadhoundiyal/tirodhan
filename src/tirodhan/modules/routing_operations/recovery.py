from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.db.values import utc_now
from tirodhan.modules.dispatch.models import RiderAssignment, RiderAssignmentItem
from tirodhan.modules.planning.models import PickupExecution
from tirodhan.modules.reliability.primitives import (
    append_outbox_event,
    claim_idempotency_record,
    complete_idempotency_record,
)

from .authority import RouteAuthorityError, lock_resources
from .commands import require_live_role
from .dispatch_models import RouteRecovery, RouteRevision, RouteTask, VehicleRoute
from .execution import finish_if_empty
from .models import OperatingVehicle


class RecoveryCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    client_command_id: UUID
    route_id: UUID
    revision_id: UUID
    reason_code: Literal["VEHICLE_UNAVAILABLE", "RIDER_UNAVAILABLE", "EXECUTION_EXCEPTION"]


async def recover_route(
    factory: async_sessionmaker[AsyncSession],
    *,
    manager_id: UUID,
    command: RecoveryCommand,
    expires_at: datetime,
    now: datetime | None = None,
) -> UUID:
    now = now or utc_now()
    async with factory() as session, session.begin():
        await require_live_role(session, manager_id, "MANAGER")
        claim = await claim_idempotency_record(
            session,
            scope=f"routing.recover:{manager_id}",
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
        root = await session.get(VehicleRoute, command.route_id)
        if root is None:
            raise RouteAuthorityError("ROUTE_NOT_FOUND")
        assignment = await session.get(RiderAssignment, root.assignment_id)
        assert assignment is not None
        await lock_resources(session, [assignment.rider_id], [root.vehicle_id], authorize=False)
        await session.refresh(root)
        revision = await session.get(RouteRevision, command.revision_id)
        if (
            revision is None
            or revision.route_id != root.route_id
            or revision.version != root.version
        ):
            raise RouteAuthorityError("ROUTE_REVISION_STALE")
        if root.status != "ACTIVE":
            raise RouteAuthorityError("ROUTE_NOT_ACTIVE")
        items = list(
            await session.scalars(
                select(RiderAssignmentItem)
                .where(
                    RiderAssignmentItem.assignment_id == root.assignment_id,
                    RiderAssignmentItem.released_at.is_(None),
                )
                .order_by(RiderAssignmentItem.pickup_execution_id)
            )
        )
        for item in items:
            pickup = await session.get(PickupExecution, item.pickup_execution_id)
            assert pickup is not None
            if pickup.status != "COLLECTED":
                await _release(session, root, pickup, now, "REASSIGNED")
        root.status = "RECOVERY_REQUIRED"
        vehicle = await session.get(OperatingVehicle, root.vehicle_id)
        assert vehicle is not None
        vehicle.version += 1
        recovery = RouteRecovery(
            route_id=root.route_id,
            manager_id=manager_id,
            reason_code=command.reason_code,
            created_at=now,
        )
        session.add(recovery)
        await session.flush()
        await finish_if_empty(session, root, now)
        await _event(session, root, str(recovery.recovery_id))
        await complete_idempotency_record(
            session,
            claim.record,
            result_resource_id=recovery.recovery_id,
            result_status_code=200,
            completed_at=now,
        )
        return recovery.recovery_id


async def detach_cancelled_booking(session: AsyncSession, request_id: UUID, now: datetime) -> None:
    """Caller holds Payment -> work-unit -> request; same transaction as refund intent."""
    pickup = await session.scalar(
        select(PickupExecution).where(PickupExecution.request_id == request_id)
    )
    if pickup is None:
        return
    root = await session.scalar(
        select(VehicleRoute)
        .join(RiderAssignmentItem, RiderAssignmentItem.assignment_id == VehicleRoute.assignment_id)
        .where(
            RiderAssignmentItem.pickup_execution_id == pickup.pickup_execution_id,
            RiderAssignmentItem.released_at.is_(None),
            VehicleRoute.status != "CLOSED",
        )
    )
    if root is None:
        return
    assignment = await session.get(RiderAssignment, root.assignment_id)
    assert assignment is not None
    await lock_resources(session, [assignment.rider_id], [root.vehicle_id], authorize=False)
    await session.refresh(root)
    await session.refresh(pickup)
    if pickup.status == "COLLECTED":
        raise RouteAuthorityError("COLLECTED_WORK_CANNOT_BE_CANCELLED")
    await _release(session, root, pickup, now, "CANCELLED")
    root.status = "RECOVERY_REQUIRED"
    vehicle = await session.get(OperatingVehicle, root.vehicle_id)
    assert vehicle is not None
    vehicle.version += 1
    await session.flush()
    await finish_if_empty(session, root, now)
    await _event(session, root, f"cancel:{request_id}")


async def _release(
    session: AsyncSession, root: VehicleRoute, pickup: PickupExecution, now: datetime, reason: str
) -> None:
    item = await session.get(RiderAssignmentItem, (root.assignment_id, pickup.pickup_execution_id))
    assert item is not None
    if item.released_at is None:
        item.released_at, item.release_reason_code = now, reason
    tasks = await session.scalars(
        select(RouteTask).where(
            RouteTask.route_id == root.route_id,
            RouteTask.pickup_execution_id == pickup.pickup_execution_id,
            RouteTask.state.in_(("PENDING", "STARTED")),
        )
    )
    for task in tasks:
        task.state = "CANCELLED"
    pickup.status, pickup.updated_at = "PENDING_ASSIGNMENT", now


async def _event(session: AsyncSession, root: VehicleRoute, identity: str) -> None:
    await append_outbox_event(
        session,
        event_key=f"vehicle-route-recovery:{root.route_id}:{identity}",
        aggregate_type="VehicleRoute",
        aggregate_id=root.route_id,
        event_type="VehicleRouteRecoveryRequired",
        payload={"route_id": str(root.route_id)},
    )
