"""Rider request / manager decision; physical handoff remains the existing custody workflow."""

from collections.abc import Callable
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tirodhan.modules.dispatch.models import RiderAssignment, RiderAssignmentItem, RiderProfile
from tirodhan.modules.planning.models import PickupExecution
from tirodhan.modules.reliability.primitives import append_outbox_event

from .authority import RouteAuthorityError, lock_booking_population, lock_resources
from .capacity_models import ReplacementDecision, RiderReplacement
from .commands import Command
from .models import OperatingShift
from .orchestration_models import RegionCell


class RequestReplacement(Command):
    operation: Literal["REPLACEMENT_REQUEST"] = "REPLACEMENT_REQUEST"
    request_id: UUID


class RecommendReplacement(Command):
    operation: Literal["REPLACEMENT_RECOMMEND"] = "REPLACEMENT_RECOMMEND"
    replacement_id: UUID
    expected_version: int = Field(strict=True, ge=1)


class DecideReplacement(Command):
    operation: Literal["REPLACEMENT_DECIDE"] = "REPLACEMENT_DECIDE"
    replacement_id: UUID
    expected_version: int = Field(strict=True, ge=1)
    decision: Literal["APPROVE", "REJECT"]


async def replacement_command(
    session: AsyncSession,
    actor: UUID,
    command: RequestReplacement | RecommendReplacement | DecideReplacement,
    now: datetime,
    expires_at: datetime,
    clock: Callable[[], datetime] | None = None,
) -> UUID:
    from .admission_models import AdmissionSourceHead
    from .operational_commands import PrepareTransfer, _execute, _shift, custodian

    row: RiderReplacement | None
    owner: UUID | None
    if isinstance(command, RequestReplacement):
        bookings = await lock_booking_population(session, [command.request_id])
        pickup = await session.scalar(
            select(PickupExecution).where(PickupExecution.request_id == command.request_id)
        )
        if pickup is None:
            raise RouteAuthorityError("ASSIGNED_PICKUP_REQUIRED")
        if pickup.status == "COLLECTED":
            _, owner = await custodian(session, command.request_id)
        else:
            owner = await session.scalar(
                select(RiderAssignment.rider_id)
                .join(RiderAssignmentItem)
                .where(
                    RiderAssignmentItem.pickup_execution_id == pickup.pickup_execution_id,
                    RiderAssignmentItem.released_at.is_(None),
                )
            )
        if owner != actor:
            raise RouteAuthorityError("ONLY_CURRENT_RIDER_CAN_REQUEST_REPLACEMENT")
        region = await session.scalar(
            select(RegionCell.region_id).where(
                RegionCell.cell_id == bookings[command.request_id].cell_id
            )
        )
        if region is None:
            raise RouteAuthorityError("REGIONAL_BOOKING_REQUIRED")
        row = RiderReplacement(
            replacement_id=command.client_command_id,
            request_id=command.request_id,
            original_rider_id=actor,
            region_id=region,
            status="REQUESTED",
            requested_at=now,
            next_attempt_at=now,
        )
        session.add(row)
    else:
        row = await session.get(RiderReplacement, command.replacement_id)
        if row is None:
            raise RouteAuthorityError("REPLACEMENT_UNAVAILABLE")
        await lock_booking_population(session, [row.request_id])
        # Match activation's resource-before-source order. Row version arbitrates two decisions.
        target = (
            await session.get(OperatingShift, row.recommended_shift_id)
            if row.recommended_shift_id
            else None
        )
        shifts = list(
            await session.scalars(
                select(OperatingShift).where(
                    OperatingShift.rider_id == row.original_rider_id,
                    OperatingShift.closed_at.is_(None),
                )
            )
        )
        resource_shifts = shifts + ([target] if target else [])
        # Match the profile -> availability -> vehicle ordering even when a donor's
        # authorization has expired. Target live authorization is checked by _shift.
        await session.execute(
            select(RiderProfile)
            .where(RiderProfile.rider_id.in_([s.rider_id for s in resource_shifts]))
            .order_by(RiderProfile.rider_id)
            .with_for_update(key_share=True)
        )
        await lock_resources(
            session,
            [s.rider_id for s in resource_shifts],
            [s.vehicle_id for s in resource_shifts],
            authorize=False,
        )
        await session.refresh(row, with_for_update=True)
        now = max(now, clock()) if clock else now
        renewable = isinstance(command, RecommendReplacement) and row.status == "APPROVED"
        if row.version != command.expected_version or (
            row.status not in ("REQUESTED", "RECOMMENDED") and not renewable
        ):
            raise RouteAuthorityError("REPLACEMENT_VERSION_CHANGED")
        if isinstance(command, RecommendReplacement):
            physical_retry = row.transfer_intent_id is not None
            if row.transfer_intent_id:
                from .orchestration_models import CargoTransferIntent

                intent = await session.get(CargoTransferIntent, row.transfer_intent_id)
                if intent is None or intent.resolution != "ABORTED":
                    raise RouteAuthorityError("RESOLVE_PHYSICAL_TRANSFER_BEFORE_RECOMMENDING")
                row.transfer_intent_id = None
            if renewable and not physical_retry:
                # The manager retains the approved request and authorizes another
                # target search. Preserve its old constraint until a fresh result exists.
                row.failure_code = "RECOMMENDATION_REQUESTED"
            else:
                row.status, row.recommended_shift_id = "REQUESTED", None
                row.failure_code = None
            row.next_attempt_at = now
            session.add(
                ReplacementDecision(
                    decision_id=command.client_command_id,
                    replacement_id=row.replacement_id,
                    manager_id=actor,
                    decision="RECOMMEND",
                    version=row.version + 1,
                    decided_at=now,
                )
            )
        else:
            if command.decision == "APPROVE":
                if target is not None:
                    await _shift(session, target.shift_id, target.rider_id, now, clock)
                head = await session.scalar(
                    select(AdmissionSourceHead)
                    .where(AdmissionSourceHead.region_id == row.region_id)
                    .with_for_update()
                )
                if (
                    head is None
                    or row.status != "RECOMMENDED"
                    or target is None
                    or head.version != row.source_version
                ):
                    raise RouteAuthorityError("REPLACEMENT_RECOMMENDATION_STALE")
                now = max(now, clock()) if clock else now
                from .regional_projection import effective_shift_end

                if now >= await effective_shift_end(session, target.shift_id, target.ends_at):
                    raise RouteAuthorityError("REPLACEMENT_RECOMMENDATION_STALE")
                pickup = await session.scalar(
                    select(PickupExecution).where(PickupExecution.request_id == row.request_id)
                )
                if pickup is not None and pickup.status == "COLLECTED":
                    vehicle, rider = await custodian(session, row.request_id)
                    if rider != row.original_rider_id:
                        raise RouteAuthorityError("REPLACEMENT_CUSTODY_CHANGED")
                    intent_id = await _execute(
                        session,
                        actor,
                        PrepareTransfer(
                            client_command_id=command.client_command_id,
                            request_id=row.request_id,
                            from_vehicle_id=vehicle,
                            to_shift_id=target.shift_id,
                        ),
                        now,
                        expires_at,
                    )
                    await session.flush()  # Persist the real intent before its replacement FK.
                    row.transfer_intent_id = intent_id
                elif pickup is not None:
                    from .dispatch_models import VehicleRoute
                    from .execution import finish_if_empty
                    from .recovery import _release

                    item = await session.scalar(
                        select(RiderAssignmentItem)
                        .join(RiderAssignment)
                        .where(
                            RiderAssignmentItem.pickup_execution_id == pickup.pickup_execution_id,
                            RiderAssignmentItem.released_at.is_(None),
                            RiderAssignment.rider_id == row.original_rider_id,
                        )
                    )
                    root = (
                        await session.scalar(
                            select(VehicleRoute).where(
                                VehicleRoute.assignment_id == item.assignment_id,
                                VehicleRoute.status != "CLOSED",
                            )
                        )
                        if item
                        else None
                    )
                    if root is None:
                        raise RouteAuthorityError("REPLACEMENT_OWNERSHIP_CHANGED")
                    from .dispatch_models import RouteTask

                    if await session.scalar(
                        select(RouteTask.task_id).where(
                            RouteTask.pickup_execution_id == pickup.pickup_execution_id,
                            RouteTask.state == "STARTED",
                        )
                    ):
                        raise RouteAuthorityError("FINISH_ACTIVE_SERVICE_BEFORE_REPLACEMENT")
                    # Explicit rider request plus manager decision authorizes recovery,
                    # not a rollback of pickup facts. Payment/resource locks arbitrate pickup.
                    await _release(session, root, pickup, now, "REASSIGNED")
                    await session.flush()
                    await finish_if_empty(session, root, now)
                else:
                    raise RouteAuthorityError("REPLACEMENT_PICKUP_UNAVAILABLE")
                row.status = "APPROVED"
            else:
                row.status = "REJECTED"
            row.decided_by, row.decided_at = actor, now
            session.add(
                ReplacementDecision(
                    decision_id=command.client_command_id,
                    replacement_id=row.replacement_id,
                    manager_id=actor,
                    decision=command.decision,
                    version=row.version + 1,
                    decided_at=now,
                )
            )
        row.version += 1
    await session.flush()
    await append_outbox_event(
        session,
        event_key=f"routing.replacement:{command.client_command_id}",
        event_type="routing.replacement.changed",
        aggregate_type="rider_replacement",
        aggregate_id=row.replacement_id,
        payload={"replacement_id": str(row.replacement_id), "status": row.status},
        available_at=now,
    )
    return row.replacement_id
