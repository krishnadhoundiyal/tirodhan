"""Manager incident visibility and short, idempotent asynchronous recovery decisions."""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError

from tirodhan.api.routes.routing_orchestration import Factory, Manager
from tirodhan.modules.reliability.primitives import IdempotencyKeyConflictError
from tirodhan.modules.routing_operations.capacity_models import RiderReplacement
from tirodhan.modules.routing_operations.commands import OperationalInputError
from tirodhan.modules.routing_operations.failure_commands import (
    ApproveRecovery,
    FailRider,
    ResolveFailedPickup,
    RetryRecovery,
    execute_failure,
    recovery_revisions,
    rider_routes,
)
from tirodhan.modules.routing_operations.failure_models import (
    RecoveryAssignment,
    RiderFailureIncident,
)

router = APIRouter(prefix="/v2", tags=["operational recovery"])


@router.post("/manager/rider-failures", status_code=202)
async def failure_command(
    body: FailRider | ApproveRecovery | RetryRecovery | ResolveFailedPickup,
    actor: Manager,
    factory: Factory,
) -> dict[str, UUID]:
    try:
        identity = await execute_failure(factory, actor=actor.user_id, command=body)
        return {"resource_id": identity}
    except (OperationalInputError, IdempotencyKeyConflictError, IntegrityError) as error:
        raise HTTPException(
            409, str(error) if isinstance(error, OperationalInputError) else "COMMAND_CONFLICT"
        ) from None
    except DBAPIError as error:
        if getattr(error.orig, "sqlstate", None) in {"40001", "40P01"}:
            raise HTTPException(409, "RETRYABLE_TRANSACTION_CONFLICT") from None
        raise


@router.get("/manager/riders/{rider_id}/recovery-state")
async def rider_state(rider_id: UUID, actor: Manager, factory: Factory) -> dict[str, Any]:
    async with factory() as session:
        roots = await rider_routes(session, rider_id)
        incidents = list(
            await session.scalars(
                select(RiderFailureIncident).where(
                    RiderFailureIncident.rider_id == rider_id, RiderFailureIncident.status == "OPEN"
                )
            )
        )
        return {
            "rider_id": rider_id,
            "routes": [
                {
                    "route_id": r.route_id,
                    "version": r.version,
                    "status": r.status,
                    "shift_id": r.shift_id,
                    "vehicle_id": r.vehicle_id,
                }
                for r in roots
            ],
            "open_incidents": [r.incident_id for r in incidents],
        }


@router.get("/manager/rider-failures/{incident_id}")
async def incident_view(
    incident_id: UUID,
    actor: Manager,
    factory: Factory,
    after_id: UUID | None = None,
    after_assignment_id: UUID | None = None,
    limit: int = Query(50, ge=1, le=100),
) -> dict[str, Any]:
    async with factory() as session:
        incident = await session.get(RiderFailureIncident, incident_id)
        if incident is None:
            raise HTTPException(404, "INCIDENT_NOT_FOUND")
        query = (
            select(RiderReplacement)
            .where(RiderReplacement.failure_incident_id == incident_id)
            .order_by(RiderReplacement.replacement_id)
            .limit(limit + 1)
        )
        if after_id:
            query = query.where(RiderReplacement.replacement_id > after_id)
        obligations = list(await session.scalars(query))
        assignments_query = (
            select(RecoveryAssignment)
            .where(RecoveryAssignment.incident_id == incident_id)
            .order_by(RecoveryAssignment.assignment_id)
            .limit(limit + 1)
        )
        if after_assignment_id:
            assignments_query = assignments_query.where(
                RecoveryAssignment.assignment_id > after_assignment_id
            )
        assignments = list(await session.scalars(assignments_query))
        from tirodhan.modules.routing_operations.dispatch_models import RouteRevision, VehicleRoute
        from tirodhan.modules.routing_operations.orchestration_models import (
            CargoTransferIntent,
            RouteAcknowledgment,
            RouteDelivery,
        )

        views = []
        for a in assignments[:limit]:
            activated = await recovery_revisions(session, a.activation_id, a.shift_id)
            activation_revision = activated[0] if activated else None
            revision = activation_revision
            if revision:
                root = await session.get(VehicleRoute, revision.route_id)
                if root:
                    revision = await session.scalar(
                        select(RouteRevision).where(
                            RouteRevision.route_id == root.route_id,
                            RouteRevision.version == root.version,
                        )
                    )
            delivery = await session.get(RouteDelivery, revision.revision_id) if revision else None
            acknowledged = bool(
                revision and await session.get(RouteAcknowledgment, revision.revision_id)
            )
            views.append(
                {
                    "assignment_id": a.assignment_id,
                    "shift_id": a.shift_id,
                    "status": a.status,
                    "version": a.version,
                    "failure_code": a.failure_code,
                    "activation_id": a.activation_id,
                    "decision": "RECORDED",
                    "rendezvous": "MANAGER_COORDINATED",
                    "activation_revision_id": activation_revision.revision_id
                    if activation_revision
                    else None,
                    "revision_id": revision.revision_id if revision else None,
                    "acknowledged": acknowledged,
                    "acknowledgment_due_at": delivery.acknowledgment_due_at if delivery else None,
                    "receipt_escalated_at": delivery.escalated_at if delivery else None,
                }
            )
        obligation_views = []
        for row in obligations[:limit]:
            intent = (
                await session.get(CargoTransferIntent, row.transfer_intent_id)
                if row.transfer_intent_id
                else None
            )
            obligation_views.append(
                {
                    "replacement_id": row.replacement_id,
                    "request_id": row.request_id,
                    "status": row.status,
                    "failure_code": row.failure_code,
                    "activation_id": row.activation_id,
                    "transfer_intent_id": row.transfer_intent_id,
                    "custody_transferred": bool(intent and intent.resolution == "TRANSFERRED"),
                }
            )
        return {
            "incident_id": incident_id,
            "rider_id": incident.rider_id,
            "version": incident.version,
            "status": incident.status,
            "obligations": obligation_views,
            "next_cursor": obligations[limit - 1].replacement_id
            if len(obligations) > limit
            else None,
            "assignments": views,
            "assignment_next_cursor": assignments[limit - 1].assignment_id
            if len(assignments) > limit
            else None,
        }
