"""Rider request / manager decision; physical handoff remains the existing custody workflow."""

import math
from collections.abc import Callable
from dataclasses import replace
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
    reason: str = Field(
        default="RIDER_REQUESTED", min_length=1, max_length=100, pattern=r"^[A-Z0-9_]+$"
    )


class ManagerRequestReplacement(Command):
    operation: Literal["MANAGER_REPLACEMENT_REQUEST"] = "MANAGER_REPLACEMENT_REQUEST"
    request_id: UUID
    reason: Literal["VEHICLE_UNAVAILABLE", "RIDER_UNAVAILABLE", "EXECUTION_EXCEPTION"]
    include_route: bool = False
    expected_revision_id: UUID


class RecommendReplacement(Command):
    operation: Literal["REPLACEMENT_RECOMMEND"] = "REPLACEMENT_RECOMMEND"
    replacement_id: UUID
    expected_version: int = Field(strict=True, ge=1)


class DecideReplacement(Command):
    operation: Literal["REPLACEMENT_DECIDE"] = "REPLACEMENT_DECIDE"
    replacement_id: UUID
    expected_version: int = Field(strict=True, ge=1)
    decision: Literal["APPROVE", "REJECT"]
    selected_candidate_id: UUID | None = None
    expected_source_version: int | None = Field(default=None, strict=True, ge=1)
    expected_region_version: int | None = Field(default=None, strict=True, ge=1)
    expected_route_versions: dict[UUID, int] | None = None
    reason: str = Field(
        default="MANAGER_DECISION", min_length=1, max_length=100, pattern=r"^[A-Z0-9_]+$"
    )


class PrepareReplacementTransfer(Command):
    operation: Literal["REPLACEMENT_TRANSFER_PREPARE"] = "REPLACEMENT_TRANSFER_PREPARE"
    replacement_id: UUID
    expected_version: int = Field(strict=True, ge=1)


async def refreshed_pending_selection(session: AsyncSession, row: RiderReplacement) -> bool:
    from .replacement_models import ReplacementCandidate

    if row.status != "APPROVED" or row.transfer_intent_id or not row.selected_candidate_id:
        return False
    previous = await session.get(ReplacementCandidate, row.selected_candidate_id)
    return bool(previous and row.evaluation_id and previous.evaluation_id != row.evaluation_id)


async def replacement_command(
    session: AsyncSession,
    actor: UUID,
    command: RequestReplacement | RecommendReplacement | DecideReplacement,
    now: datetime,
    expires_at: datetime,
    clock: Callable[[], datetime] | None = None,
    *,
    verified_evaluation_id: UUID | None = None,
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
            requested_by=actor,
            reason=command.reason,
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
        from .replacement_models import ReplacementCandidate, ReplacementEvaluation

        option = None
        if isinstance(command, DecideReplacement) and command.decision == "APPROVE":
            option = (
                await session.get(ReplacementCandidate, command.selected_candidate_id)
                if command.selected_candidate_id
                else await session.scalar(
                    select(ReplacementCandidate)
                    .where(ReplacementCandidate.evaluation_id == row.evaluation_id)
                    .order_by(ReplacementCandidate.rank)
                    .limit(1)
                )
            )
            if option is None or option.evaluation_id != row.evaluation_id:
                raise RouteAuthorityError("VALIDATED_REPLACEMENT_CANDIDATE_REQUIRED")
        target_id = option.shift_id if option else row.recommended_shift_id
        target = await session.get(OperatingShift, target_id) if target_id else None
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
        reselect = (
            isinstance(command, DecideReplacement)
            and command.decision == "APPROVE"
            and await refreshed_pending_selection(session, row)
        )
        if row.version != command.expected_version or (
            row.status not in ("REQUESTED", "RECOMMENDED") and not renewable and not reselect
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
            from .recovery_work import enqueue_work

            row.recommendation_token = row.recommendation_until = None
            await enqueue_work(session, row.replacement_id, "RECOMMEND", now, replace=True)
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
                    or (row.status != "RECOMMENDED" and not reselect)
                    or target is None
                    or (
                        verified_evaluation_id != row.evaluation_id
                        and not reselect
                        and head.version != row.source_version
                    )
                ):
                    raise RouteAuthorityError("REPLACEMENT_RECOMMENDATION_STALE")
                assert option is not None
                if command.expected_route_versions is not None and {
                    str(k): v for k, v in command.expected_route_versions.items()
                } != {r["route_id"]: r["version"] for r in option.summary["route_versions"]}:
                    raise RouteAuthorityError("REPLACEMENT_ROUTE_VERSIONS_CHANGED")
                evaluation = await session.get(ReplacementEvaluation, option.evaluation_id)
                from .orchestration_models import OperatingRegion

                region_row = await session.get(OperatingRegion, row.region_id)
                if (
                    evaluation is None
                    or region_row is None
                    or now >= evaluation.expires_at
                    or (
                        verified_evaluation_id != evaluation.evaluation_id
                        and evaluation.source_version != head.version
                    )
                    or region_row.version != evaluation.region_version
                    or (
                        verified_evaluation_id != evaluation.evaluation_id
                        and command.expected_source_version is not None
                        and command.expected_source_version != head.version
                    )
                    or (
                        command.expected_region_version is not None
                        and command.expected_region_version != region_row.version
                    )
                ):
                    raise RouteAuthorityError("REPLACEMENT_CANDIDATE_STALE")
                from tirodhan.modules.planning.routing.validation import (
                    ProposalAssessment,
                    RoutingValidationError,
                    validate_proposal,
                )

                from .service import PROBLEM_CODEC, PROPOSAL_CODEC

                problem = PROBLEM_CODEC.validate_python(option.problem)
                proposal = PROPOSAL_CODEC.validate_python(option.proposal)
                original_assessment = validate_proposal(problem, proposal)
                elapsed = math.ceil((now - problem.epoch).total_seconds())
                try:
                    live_assessment = validate_proposal(
                        replace(
                            problem,
                            vehicles=tuple(
                                replace(v, available_at=max(v.available_at, elapsed))
                                for v in problem.vehicles
                            ),
                        ),
                        proposal,
                    )
                except RoutingValidationError:
                    raise RouteAuthorityError("REPLACEMENT_CANDIDATE_STALE") from None
                discharge_ids = {s.stop_id for s in problem.stops if s.kind.value == "DISCHARGE"}

                def visits(assessment: ProposalAssessment) -> list[tuple[UUID, int, int]]:
                    return [
                        (s.stop_id, s.service_start_at, s.service_end_at)
                        for r in assessment.routes
                        for s in r.stops
                        if s.stop_id in discharge_ids
                    ]

                if visits(original_assessment) != visits(live_assessment):
                    raise RouteAuthorityError("REPLACEMENT_FACILITY_TIMING_REQUIRES_REFRESH")
                row.recommended_shift_id = target.shift_id
                row.selected_candidate_id = option.candidate_id
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
                    try:
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
                    except RouteAuthorityError as error:
                        if str(error) != "FINISH_ACTIVE_SERVICE_OR_TRANSFER_FIRST":
                            raise
                        row.failure_code = "AWAITING_PHYSICAL_TRANSFER"
                    else:
                        await session.flush()
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
                    if root is None and not reselect:
                        raise RouteAuthorityError("REPLACEMENT_OWNERSHIP_CHANGED")
                    if reselect and await session.scalar(
                        select(RiderAssignmentItem.assignment_id).where(
                            RiderAssignmentItem.pickup_execution_id == pickup.pickup_execution_id,
                            RiderAssignmentItem.released_at.is_(None),
                        )
                    ):
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
                    if root is not None:
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
                    candidate_id=option.candidate_id if option else None,
                    reason=command.reason,
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


async def manager_replacement_request(
    session: AsyncSession,
    actor: UUID,
    command: ManagerRequestReplacement,
    now: datetime,
) -> UUID:
    """Record an actual manager incident; preserve every individual household obligation."""
    from tirodhan.db.values import new_uuid7

    from .dispatch_models import RouteRevision, RouteTask, VehicleRoute
    from .fleet_models import FleetException
    from .models import OperatingVehicle

    pickup = await session.scalar(
        select(PickupExecution).where(PickupExecution.request_id == command.request_id)
    )
    if pickup is None:
        raise RouteAuthorityError("ASSIGNED_PICKUP_REQUIRED")
    root = await session.scalar(
        select(VehicleRoute)
        .join(RouteTask)
        .where(
            RouteTask.pickup_execution_id == pickup.pickup_execution_id,
            RouteTask.state.in_(("PENDING", "STARTED")),
            VehicleRoute.status != "CLOSED",
        )
        .limit(1)
    )
    if root is None:
        raise RouteAuthorityError("CURRENT_OPERATIONAL_ROUTE_REQUIRED")
    pickups = (
        list(
            await session.scalars(
                select(PickupExecution)
                .where(
                    PickupExecution.pickup_execution_id.in_(
                        select(RouteTask.pickup_execution_id).where(
                            RouteTask.route_id == root.route_id,
                            RouteTask.state.in_(("PENDING", "STARTED")),
                        )
                    )
                )
                .order_by(PickupExecution.request_id)
            )
        )
        if command.include_route
        or command.reason in ("RIDER_UNAVAILABLE", "VEHICLE_UNAVAILABLE", "EXECUTION_EXCEPTION")
        else [pickup]
    )
    bookings = await lock_booking_population(session, [p.request_id for p in pickups])
    assignment = await session.get(RiderAssignment, root.assignment_id)
    assert assignment
    await lock_resources(session, [assignment.rider_id], [root.vehicle_id], authorize=False)
    await session.refresh(root)
    revision = await session.get(RouteRevision, command.expected_revision_id)
    if (
        revision is None
        or revision.route_id != root.route_id
        or revision.version != root.version
        or root.status == "CLOSED"
    ):
        raise RouteAuthorityError("ROUTE_REVISION_STALE")
    live_pickups = set(
        await session.scalars(
            select(RouteTask.pickup_execution_id).where(
                RouteTask.route_id == root.route_id, RouteTask.state.in_(("PENDING", "STARTED"))
            )
        )
    )
    if pickup.pickup_execution_id not in live_pickups:
        raise RouteAuthorityError("REPLACEMENT_OWNERSHIP_CHANGED")
    pickups = [item for item in pickups if item.pickup_execution_id in live_pickups]
    if await session.scalar(
        select(RouteTask.task_id)
        .where(RouteTask.route_id == root.route_id, RouteTask.state == "STARTED")
        .limit(1)
    ):
        raise RouteAuthorityError("FINISH_ACTIVE_SERVICE_BEFORE_REPLACEMENT")
    incident = FleetException(
        exception_id=command.client_command_id,
        route_id=root.route_id,
        reported_by=actor,
        reason=command.reason,
        created_at=now,
    )
    session.add(incident)
    await session.flush()
    primary = None
    for item in pickups:
        region = await session.scalar(
            select(RegionCell.region_id).where(
                RegionCell.cell_id == bookings[item.request_id].cell_id
            )
        )
        if region is None:
            raise RouteAuthorityError("REGIONAL_BOOKING_REQUIRED")
        existing = await session.scalar(
            select(RiderReplacement)
            .where(
                RiderReplacement.request_id == item.request_id,
                RiderReplacement.status.not_in(("COMPLETED", "REJECTED")),
            )
            .with_for_update()
        )
        if existing:
            if existing.status == "APPROVED":
                raise RouteAuthorityError("RESOLVE_APPROVED_REPLACEMENT_BEFORE_GROUPING")
            existing.incident_id, existing.reason = incident.exception_id, command.reason
            existing.version += 1
            existing.status, existing.failure_code = "REQUESTED", None
            existing.next_attempt_at = now
            current = existing
        else:
            current = RiderReplacement(
                replacement_id=command.client_command_id
                if item.request_id == command.request_id
                else new_uuid7(),
                request_id=item.request_id,
                original_rider_id=assignment.rider_id,
                region_id=region,
                requested_by=actor,
                reason=command.reason,
                incident_id=incident.exception_id,
                status="REQUESTED",
                requested_at=now,
                next_attempt_at=now,
            )
            session.add(current)
            await session.flush([current])
        if item.request_id == command.request_id:
            primary = current.replacement_id
        session.add(
            ReplacementDecision(
                decision_id=new_uuid7(),
                replacement_id=current.replacement_id,
                manager_id=actor,
                decision="INCIDENT",
                version=current.version or 1,
                decided_at=now,
                reason=command.reason,
            )
        )
        await session.flush()
        await append_outbox_event(
            session,
            event_key=f"routing.incident:{command.client_command_id}:{item.request_id}",
            event_type="routing.replacement.changed",
            aggregate_type="rider_replacement",
            aggregate_id=current.replacement_id,
            payload={"replacement_id": str(current.replacement_id)},
            available_at=now,
        )
    vehicle = await session.get(OperatingVehicle, root.vehicle_id)
    assert vehicle
    vehicle.blocked_reason = "BREAKDOWN" if command.reason == "VEHICLE_UNAVAILABLE" else "SAFETY"
    vehicle.version += 1
    vehicle.safety_version += 1
    root.status = "RECOVERY_REQUIRED"
    assert primary is not None
    return primary


async def decide_replacement_group(
    session: AsyncSession,
    actor: UUID,
    command: DecideReplacement,
    now: datetime,
    expires_at: datetime,
    clock: Callable[[], datetime] | None = None,
) -> UUID:
    """One candidate may cover a whole incident; approval is one atomic group decision.

    Own source invalidations within this transaction cannot invalidate its later children.
    The private verified-evaluation token is never part of a client command or API schema.
    """
    from tirodhan.db.values import new_uuid7

    from .admission_models import AdmissionSourceHead
    from .replacement_models import ReplacementCandidate, ReplacementEvaluation

    parent = await session.get(RiderReplacement, command.replacement_id)
    if parent is not None and parent.failure_incident_id is not None:
        raise RouteAuthorityError("USE_INCIDENT_RECOVERY_ALLOCATION")
    if parent is None or command.decision != "APPROVE" or parent.incident_id is None:
        return await replacement_command(session, actor, command, now, expires_at, clock)
    option = (
        await session.get(ReplacementCandidate, command.selected_candidate_id)
        if command.selected_candidate_id
        else await session.scalar(
            select(ReplacementCandidate)
            .where(ReplacementCandidate.evaluation_id == parent.evaluation_id)
            .order_by(ReplacementCandidate.rank)
            .limit(1)
        )
    )
    if option is None or option.evaluation_id != parent.evaluation_id:
        raise RouteAuthorityError("VALIDATED_REPLACEMENT_CANDIDATE_REQUIRED")
    evaluation = await session.get(ReplacementEvaluation, option.evaluation_id)
    assert evaluation
    group = list(
        await session.scalars(
            select(RiderReplacement).where(
                RiderReplacement.incident_id == parent.incident_id,
                RiderReplacement.status.not_in(("REJECTED", "COMPLETED")),
            )
        )
    )
    if {str(r.request_id) for r in group} != set(option.summary["affected_bookings"]):
        raise RouteAuthorityError("REPLACEMENT_INCIDENT_CHANGED")
    await lock_booking_population(session, [r.request_id for r in group])
    shifts = list(
        await session.scalars(
            select(OperatingShift).where(
                OperatingShift.rider_id.in_([r.original_rider_id for r in group]),
                OperatingShift.closed_at.is_(None),
            )
        )
    )
    target = await session.get(OperatingShift, option.shift_id)
    if target is None:
        raise RouteAuthorityError("REPLACEMENT_RESOURCES_CHANGED")
    shifts.append(target)
    await session.execute(
        select(RiderProfile)
        .where(RiderProfile.rider_id.in_([s.rider_id for s in shifts]))
        .order_by(RiderProfile.rider_id)
        .with_for_update(key_share=True)
    )
    await lock_resources(
        session, [s.rider_id for s in shifts], [s.vehicle_id for s in shifts], authorize=False
    )
    for item in sorted(group, key=lambda r: r.replacement_id):
        await session.refresh(item, with_for_update=True)
        if (
            item.evaluation_id != evaluation.evaluation_id
            or (
                item.status != "RECOMMENDED"
                and not await refreshed_pending_selection(session, item)
            )
            or item.version
            != evaluation.search["replacement_versions"].get(str(item.replacement_id), -2) + 1
        ):
            raise RouteAuthorityError("REPLACEMENT_INCIDENT_VERSION_CHANGED")
    if parent.version != command.expected_version:
        raise RouteAuthorityError("REPLACEMENT_VERSION_CHANGED")
    head = await session.get(AdmissionSourceHead, parent.region_id, with_for_update=True)
    if head is None or head.version != evaluation.source_version:
        raise RouteAuthorityError("REPLACEMENT_CANDIDATE_STALE")
    ordered = [parent] + sorted(
        [r for r in group if r.replacement_id != parent.replacement_id],
        key=lambda r: r.replacement_id,
    )
    for index, item in enumerate(ordered):
        child = (
            command
            if index == 0
            else command.model_copy(
                update={
                    "client_command_id": new_uuid7(),
                    "replacement_id": item.replacement_id,
                    "expected_version": item.version,
                    "selected_candidate_id": option.candidate_id,
                }
            )
        )
        await replacement_command(
            session,
            actor,
            child,
            now,
            expires_at,
            clock,
            verified_evaluation_id=evaluation.evaluation_id if index else None,
        )
    return parent.replacement_id


async def prepare_replacement_transfer(
    session: AsyncSession,
    actor: UUID,
    command: PrepareReplacementTransfer,
    now: datetime,
    expires_at: datetime,
) -> UUID:
    from .operational_commands import PrepareTransfer, _execute, custodian

    row = await session.get(RiderReplacement, command.replacement_id)
    if row is None:
        raise RouteAuthorityError("REPLACEMENT_UNAVAILABLE")
    await lock_booking_population(session, [row.request_id])
    vehicle, rider = await custodian(session, row.request_id)
    target = await session.get(OperatingShift, row.recommended_shift_id)
    if target is None:
        raise RouteAuthorityError("REPLACEMENT_RESOURCES_CHANGED")
    await lock_resources(
        session, [rider, target.rider_id], [vehicle, target.vehicle_id], authorize=False
    )
    await session.refresh(row, with_for_update=True)
    if (
        row.version != command.expected_version
        or row.status != "APPROVED"
        or row.transfer_intent_id is not None
    ):
        raise RouteAuthorityError("REPLACEMENT_VERSION_CHANGED")
    intent = await _execute(
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
    await session.flush()
    row.transfer_intent_id = intent
    row.failure_code = None
    row.version += 1
    return row.replacement_id
