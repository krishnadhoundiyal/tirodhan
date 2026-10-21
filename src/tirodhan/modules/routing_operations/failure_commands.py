"""Manager-authorized incident and allocation commands; no routing I/O."""

from __future__ import annotations

import hashlib
import math
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.dispatch.models import RiderAssignment, RiderAssignmentItem
from tirodhan.modules.handovers.models import HandoverEventItem
from tirodhan.modules.planning.models import PickupExecution
from tirodhan.modules.reliability.primitives import (
    claim_idempotency_record,
    complete_idempotency_record,
)

from .authority import RouteAuthorityError, lock_booking_population, lock_resources
from .capacity_models import ReplacementDecision, RiderReplacement
from .commands import require_live_role
from .dispatch_models import RouteRecovery, RouteRevision, RouteTask, VehicleRoute
from .failure_models import (
    FailureRoute,
    RecoveryAssignment,
    RecoveryAssignmentItem,
    RiderFailureIncident,
)
from .models import OperatingShift, OperatingVehicle
from .orchestration_models import CargoTransfer, RegionVehicle
from .replacement_models import ReplacementCandidate, ReplacementEvaluation


class FailRider(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    operation: Literal["FAIL_RIDER"] = "FAIL_RIDER"
    client_command_id: UUID
    rider_id: UUID
    reason: Literal["RIDER_UNAVAILABLE", "VEHICLE_UNAVAILABLE", "EXECUTION_EXCEPTION"]
    expected_route_versions: dict[UUID, int]


class RecoverySelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    candidate_id: UUID
    replacement_ids: list[UUID] = Field(min_length=1, max_length=100)


class ApproveRecovery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    operation: Literal["APPROVE_RECOVERY"] = "APPROVE_RECOVERY"
    client_command_id: UUID
    incident_id: UUID
    expected_version: int = Field(ge=1)
    selections: list[RecoverySelection] = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=100, pattern=r"^[A-Z0-9_]+$")


class RetryRecovery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    operation: Literal["RETRY_RECOVERY"] = "RETRY_RECOVERY"
    client_command_id: UUID
    assignment_id: UUID
    expected_version: int = Field(ge=1)
    request_new_options: bool = False


class ResolveFailedPickup(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    operation: Literal["RESOLVE_FAILED_PICKUP"] = "RESOLVE_FAILED_PICKUP"
    client_command_id: UUID
    replacement_id: UUID
    expected_version: int = Field(ge=1)
    no_collection_confirmed: Literal[True]


async def enqueue(
    session: AsyncSession, identity: UUID, kind: str, now: datetime, *, replace: bool = False
) -> None:
    from .recovery_work import enqueue_work

    await enqueue_work(session, identity, kind, now, replace=replace)


async def rider_routes(session: AsyncSession, rider: UUID) -> list[VehicleRoute]:
    return list(
        await session.scalars(
            select(VehicleRoute)
            .join(RiderAssignment)
            .where(RiderAssignment.rider_id == rider, VehicleRoute.status != "CLOSED")
            .order_by(VehicleRoute.route_id)
        )
    )


async def recovery_revisions(
    session: AsyncSession, activation_id: UUID | None, shift_id: UUID | None
) -> list[RouteRevision]:
    """One allocation's routes, even when its activation revises other resources."""
    if activation_id is None or shift_id is None:
        return []
    return list(
        await session.scalars(
            select(RouteRevision)
            .join(VehicleRoute)
            .where(RouteRevision.activation_id == activation_id, VehicleRoute.shift_id == shift_id)
            .order_by(RouteRevision.version)
        )
    )


async def _fail(session: AsyncSession, actor: UUID, command: FailRider, now: datetime) -> UUID:
    from .operational_commands import custodian

    roots = await rider_routes(session, command.rider_id)
    shifts = list(
        await session.scalars(
            select(OperatingShift).where(
                OperatingShift.rider_id == command.rider_id, OperatingShift.closed_at.is_(None)
            )
        )
    )
    vehicles = {s.vehicle_id for s in shifts} | {r.vehicle_id for r in roots}
    owned = (
        select(RiderAssignmentItem.pickup_execution_id)
        .join(RiderAssignment)
        .where(
            RiderAssignment.rider_id == command.rider_id, RiderAssignmentItem.released_at.is_(None)
        )
    )
    transferred = select(CargoTransfer.request_id).where(
        CargoTransfer.to_rider_id == command.rider_id
    )
    pickups = list(
        await session.scalars(
            select(PickupExecution).where(
                or_(
                    PickupExecution.pickup_execution_id.in_(owned),
                    PickupExecution.request_id.in_(transferred),
                ),
                ~exists().where(
                    HandoverEventItem.pickup_execution_id == PickupExecution.pickup_execution_id,
                    HandoverEventItem.status == "VALIDATED",
                ),
            )
        )
    )
    selected = []
    for p in pickups:
        if p.status == "COLLECTED":
            vehicle, rider = await custodian(session, p.request_id)
            if rider != command.rider_id:
                continue
            vehicles.add(vehicle)
        selected.append(p)
    await lock_booking_population(session, [p.request_id for p in selected])
    await lock_resources(session, [command.rider_id], sorted(vehicles, key=str), authorize=False)
    roots = await rider_routes(session, command.rider_id)
    for root in roots:
        await session.refresh(root)
    if {r.route_id: r.version for r in roots} != command.expected_route_versions:
        raise RouteAuthorityError("FAILURE_ROUTES_CHANGED")
    current = await session.scalar(
        select(RiderFailureIncident)
        .where(
            RiderFailureIncident.rider_id == command.rider_id, RiderFailureIncident.status == "OPEN"
        )
        .with_for_update()
    )
    if current:
        return current.incident_id
    memberships = list(
        await session.scalars(select(RegionVehicle).where(RegionVehicle.vehicle_id.in_(vehicles)))
    )
    regions = {m.region_id for m in memberships}
    if len(regions) != 1:
        raise RouteAuthorityError("SINGLE_REGIONAL_OWNER_REQUIRED")
    incident = RiderFailureIncident(
        incident_id=command.client_command_id,
        rider_id=command.rider_id,
        region_id=regions.pop(),
        reported_by=actor,
        reason=command.reason,
        status="OPEN",
        version=1,
        created_at=now,
    )
    session.add(incident)
    await session.flush()
    for root in roots:
        revision = await session.scalar(
            select(RouteRevision).where(
                RouteRevision.route_id == root.route_id, RouteRevision.version == root.version
            )
        )
        assert revision is not None
        session.add(
            FailureRoute(
                incident_id=incident.incident_id,
                route_id=root.route_id,
                revision_id=revision.revision_id,
            )
        )
        root.status = "RECOVERY_REQUIRED"
    for vehicle_id in vehicles:
        v = await session.get(OperatingVehicle, vehicle_id)
        assert v
        v.blocked_reason = "BREAKDOWN" if command.reason == "VEHICLE_UNAVAILABLE" else "SAFETY"
        v.version += 1
        v.safety_version += 1
    for pickup in selected:
        previous_items = list(
            await session.scalars(
                select(RecoveryAssignmentItem)
                .join(
                    RiderReplacement,
                    RecoveryAssignmentItem.replacement_id == RiderReplacement.replacement_id,
                )
                .join(
                    RecoveryAssignment,
                    RecoveryAssignment.assignment_id == RecoveryAssignmentItem.assignment_id,
                )
                .where(
                    RiderReplacement.request_id == pickup.request_id,
                    RecoveryAssignment.status == "ACTIVATED",
                    RecoveryAssignmentItem.released_at.is_(None),
                )
            )
        )
        for previous in previous_items:
            previous.released_at = now
        existing = await session.scalar(
            select(RiderReplacement).where(
                RiderReplacement.request_id == pickup.request_id,
                RiderReplacement.status.not_in(("REJECTED", "COMPLETED")),
            )
        )
        if existing and existing.original_rider_id != command.rider_id:
            raise RouteAuthorityError("REPLACEMENT_OWNERSHIP_CHANGED")
        row = existing or RiderReplacement(
            replacement_id=new_uuid7(),
            request_id=pickup.request_id,
            original_rider_id=command.rider_id,
            region_id=incident.region_id,
            status="REQUESTED",
            version=1,
            requested_at=now,
            next_attempt_at=now,
        )
        if existing and existing.status == "APPROVED":
            raise RouteAuthorityError("EXISTING_APPROVAL_REQUIRES_REVIEW")
        row.failure_incident_id = incident.incident_id
        if existing is None:
            row.requested_by = actor
            row.reason = command.reason
        else:
            row.version += 1
        row.status = "REQUESTED"
        session.add(row)
        await session.flush()
        await enqueue(session, row.replacement_id, "RECOMMEND", now)
    from .orchestration import wake

    await wake(session, "RIDER_FAILED", now, urgent=True, region_id=incident.region_id)
    return incident.incident_id


async def _approve(
    session: AsyncSession, actor: UUID, command: ApproveRecovery, now: datetime
) -> UUID:
    from .admission_models import AdmissionSourceHead

    ids = [r for choice in command.selections for r in choice.replacement_ids]
    if len(ids) != len(set(ids)):
        raise RouteAuthorityError("DUPLICATE_RECOVERY_OBLIGATION")
    rows = list(
        await session.scalars(
            select(RiderReplacement).where(RiderReplacement.replacement_id.in_(ids))
        )
    )
    if len(rows) != len(ids) or any(r.failure_incident_id != command.incident_id for r in rows):
        raise RouteAuthorityError("INCIDENT_OBLIGATION_MISMATCH")
    options = [await session.get(ReplacementCandidate, c.candidate_id) for c in command.selections]
    if any(o is None for o in options):
        raise RouteAuthorityError("VALIDATED_REPLACEMENT_CANDIDATE_REQUIRED")
    shifts = [await session.get(OperatingShift, o.shift_id) for o in options if o]
    if any(s is None for s in shifts) or len({s.vehicle_id for s in shifts if s}) != len(shifts):
        raise RouteAuthorityError("DISTINCT_RECOVERY_RESOURCES_REQUIRED")
    await lock_booking_population(session, [r.request_id for r in rows])
    await lock_resources(
        session,
        sorted({r.original_rider_id for r in rows} | {s.rider_id for s in shifts if s}, key=str),
        sorted({s.vehicle_id for s in shifts if s}, key=str),
        authorize=True,
        authorize_rider_ids=[s.rider_id for s in shifts if s],
    )
    for obligation in sorted(rows, key=lambda r: str(r.replacement_id)):
        await session.refresh(obligation, with_for_update=True)
    incident = await session.get(RiderFailureIncident, command.incident_id, with_for_update=True)
    if not incident or incident.status != "OPEN" or incident.version != command.expected_version:
        raise RouteAuthorityError("FAILURE_INCIDENT_VERSION_CHANGED")
    head = await session.get(AdmissionSourceHead, incident.region_id, with_for_update=True)
    for selection, option, shift in zip(command.selections, options, shifts, strict=True):
        assert option and shift
        evaluation = await session.get(ReplacementEvaluation, option.evaluation_id)
        chosen = [r for r in rows if r.replacement_id in selection.replacement_ids]
        for row in chosen:
            await session.refresh(row, with_for_update=True)
        if (
            not evaluation
            or evaluation.expires_at <= now
            or not head
            or evaluation.source_version != head.version
            or any(
                r.evaluation_id != evaluation.evaluation_id or r.status != "RECOMMENDED"
                for r in chosen
            )
        ):
            raise RouteAuthorityError("REPLACEMENT_CANDIDATE_STALE")
        if set(option.summary["affected_bookings"]) != {str(r.request_id) for r in chosen}:
            raise RouteAuthorityError("REPLACEMENT_COVERAGE_MISMATCH")
        if await session.scalar(
            select(RecoveryAssignment.assignment_id)
            .where(
                RecoveryAssignment.shift_id == shift.shift_id,
                RecoveryAssignment.status.in_(
                    ("PENDING", "RUNNING", "AWAITING_TRANSFER", "ATTENTION")
                ),
            )
            .limit(1)
        ):
            raise RouteAuthorityError("RESOURCE_RECOVERY_PENDING")
        from .regional_projection import effective_shift_end

        if shift.closed_at or not shift.starts_at <= now < await effective_shift_end(
            session, shift.shift_id, shift.ends_at
        ):
            raise RouteAuthorityError("ACTIVE_SHIFT_REQUIRED")
        from tirodhan.modules.planning.routing.validation import validate_proposal

        from .recovery_planning import donor_positions

        observed = await donor_positions(
            session, {UUID(v) for v in option.summary.get("donor_position_ids", {})}
        )
        if any(
            k not in observed or str(observed[k]["position_id"]) != value
            for k, value in option.summary.get("donor_position_ids", {}).items()
        ):
            raise RouteAuthorityError("REPLACEMENT_DONOR_POSITION_CHANGED")
        from .service import PROBLEM_CODEC, PROPOSAL_CODEC

        proof = PROBLEM_CODEC.validate_python(option.problem)
        elapsed = max(0, math.ceil((now - proof.epoch).total_seconds()))
        from tirodhan.modules.planning.routing.validation import RoutingValidationError

        try:
            validate_proposal(
                replace(
                    proof,
                    vehicles=tuple(
                        replace(v, available_at=max(v.available_at, elapsed))
                        for v in proof.vehicles
                    ),
                ),
                PROPOSAL_CODEC.validate_python(option.proposal),
            )
        except RoutingValidationError:
            raise RouteAuthorityError("REPLACEMENT_CANDIDATE_STALE") from None
        assignment = RecoveryAssignment(
            assignment_id=new_uuid7(),
            incident_id=incident.incident_id,
            candidate_id=option.candidate_id,
            shift_id=shift.shift_id,
            approved_by=actor,
            approved_at=now,
            status="PENDING",
            version=1,
            attempt=0,
            due_at=now,
        )
        session.add(assignment)
        await session.flush()
        for row in chosen:
            session.add(
                RecoveryAssignmentItem(
                    assignment_id=assignment.assignment_id, replacement_id=row.replacement_id
                )
            )
            row.status = "APPROVED"
            row.recommended_shift_id = shift.shift_id
            row.selected_candidate_id = option.candidate_id
            row.decided_by, row.decided_at = actor, now
            row.version += 1
            session.add(
                ReplacementDecision(
                    decision_id=new_uuid7(),
                    replacement_id=row.replacement_id,
                    manager_id=actor,
                    decision="APPROVE",
                    version=row.version,
                    decided_at=now,
                    candidate_id=option.candidate_id,
                    reason=command.reason,
                )
            )
        await enqueue(session, assignment.assignment_id, "ACTIVATE", now)
    incident.version += 1
    return incident.incident_id


async def execute_failure(
    factory: async_sessionmaker[AsyncSession],
    *,
    actor: UUID,
    command: FailRider | ApproveRecovery | RetryRecovery | ResolveFailedPickup,
    now: datetime | None = None,
) -> UUID:
    now = now or utc_now()
    async with factory() as session:
        await session.connection(execution_options={"isolation_level": "SERIALIZABLE"})
        await require_live_role(session, actor, "MANAGER")
        claim = await claim_idempotency_record(
            session,
            scope=f"routing.failure:{actor}",
            idempotency_key=str(command.client_command_id),
            request_fingerprint=hashlib.sha256(command.model_dump_json().encode()).digest(),
            expires_at=now + timedelta(days=1),
            now=now,
        )
        if claim.record.status == "COMPLETED":
            assert claim.record.result_resource_id
            return claim.record.result_resource_id
        if not claim.created:
            raise RouteAuthorityError("COMMAND_IN_PROGRESS")
        if isinstance(command, FailRider):
            result = await _fail(session, actor, command, now)
        elif isinstance(command, ApproveRecovery):
            result = await _approve(session, actor, command, now)
        elif isinstance(command, ResolveFailedPickup):
            replacement = await session.get(RiderReplacement, command.replacement_id)
            if not replacement or not replacement.failure_incident_id:
                raise RouteAuthorityError("FAILURE_OBLIGATION_REQUIRED")
            await lock_booking_population(session, [replacement.request_id])
            roots = await rider_routes(session, replacement.original_rider_id)
            await lock_resources(
                session,
                [replacement.original_rider_id],
                [r.vehicle_id for r in roots],
                authorize=False,
            )
            await session.refresh(replacement, with_for_update=True)
            pickup = await session.scalar(
                select(PickupExecution).where(PickupExecution.request_id == replacement.request_id)
            )
            if (
                replacement.version != command.expected_version
                or not pickup
                or pickup.status == "COLLECTED"
            ):
                raise RouteAuthorityError("FAILED_SERVICE_FACTS_CHANGED")
            from .recovery import _release

            for root in roots:
                if await session.scalar(
                    select(RouteTask.task_id)
                    .where(
                        RouteTask.route_id == root.route_id,
                        RouteTask.pickup_execution_id == pickup.pickup_execution_id,
                        RouteTask.state.in_(("PENDING", "STARTED")),
                    )
                    .limit(1)
                ):
                    await _release(session, root, pickup, now, "REASSIGNED")
                    session.add(
                        RouteRecovery(
                            route_id=root.route_id,
                            manager_id=actor,
                            reason_code="EXECUTION_EXCEPTION",
                            created_at=now,
                        )
                    )
            replacement.version += 1
            replacement.failure_code = None
            replacement.next_attempt_at = now
            await enqueue(session, replacement.replacement_id, "RECOMMEND", now, replace=True)
            result = replacement.replacement_id
        else:
            initial = await session.get(RecoveryAssignment, command.assignment_id)
            if initial is None:
                raise RouteAuthorityError("RECOVERY_ASSIGNMENT_CHANGED")
            from .failure_execution import assignment_rows

            initial_rows = await assignment_rows(session, command.assignment_id)
            await lock_booking_population(session, [r.request_id for r in initial_rows])
            row = await session.get(RecoveryAssignment, command.assignment_id, with_for_update=True)
            if (
                not row
                or row.version != command.expected_version
                or row.status not in ("ATTENTION", "AWAITING_TRANSFER")
            ):
                raise RouteAuthorityError("RECOVERY_ASSIGNMENT_CHANGED")
            from .orchestration_models import CargoTransferIntent

            obligations = await assignment_rows(session, row.assignment_id)
            for obligation in obligations:
                if not obligation.transfer_intent_id:
                    continue
                intent = await session.get(
                    CargoTransferIntent, obligation.transfer_intent_id, with_for_update=True
                )
                if intent and intent.resolution == "ABORTED":
                    # Existing abort evidence proves custody did not move. Retain
                    # its history while authorizing a fresh physical-transfer attempt.
                    obligation.transfer_intent_id = None
                    obligation.version += 1
                elif command.request_new_options:
                    raise RouteAuthorityError("RESOLVE_PHYSICAL_TRANSFER_FIRST")
            if command.request_new_options:
                items = list(
                    await session.scalars(
                        select(RecoveryAssignmentItem).where(
                            RecoveryAssignmentItem.assignment_id == row.assignment_id,
                            RecoveryAssignmentItem.released_at.is_(None),
                        )
                    )
                )
                for item in items:
                    item.released_at = now
                for obligation in obligations:
                    session.add(
                        ReplacementDecision(
                            decision_id=new_uuid7(),
                            replacement_id=obligation.replacement_id,
                            manager_id=actor,
                            decision="WITHDRAW",
                            version=obligation.version + 1,
                            decided_at=now,
                            candidate_id=obligation.selected_candidate_id,
                            reason="MANAGER_REQUESTED_NEW_OPTIONS",
                        )
                    )
                    obligation.selected_candidate_id = None
                    obligation.status = "REQUESTED"
                    obligation.recommended_shift_id = None
                    obligation.version += 1
                    obligation.next_attempt_at = now
                    await enqueue(
                        session, obligation.replacement_id, "RECOMMEND", now, replace=True
                    )
                row.status = "SUPERSEDED"
                from .recovery_work import close_work

                await close_work(session, row.assignment_id, "ACTIVATE", now, "MANAGER_SUPERSEDED")
            else:
                row.status = "PENDING"
            row.due_at, row.failure_code = now, None
            row.version += 1
            if not command.request_new_options:
                await enqueue(session, row.assignment_id, "ACTIVATE", now, replace=True)
            parent = await session.get(RiderFailureIncident, row.incident_id, with_for_update=True)
            assert parent
            parent.version += 1
            result = row.assignment_id
        await complete_idempotency_record(
            session,
            claim.record,
            result_resource_id=result,
            result_status_code=202,
            completed_at=now,
        )
        await session.commit()
        return result
