"""Existing-worker recovery dispatch, bounded claims and periodic durable reconciliation."""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.db.values import new_uuid7
from tirodhan.modules.planning.routing.contracts import OptimizerEngine, RouteOptimizer

from .capacity_models import RiderReplacement
from .failure_commands import enqueue, recovery_revisions
from .failure_models import RecoveryAssignment
from .snapshot import MatrixSource


async def recommend_one(
    factory: async_sessionmaker[AsyncSession],
    identity: UUID,
    *,
    providers: dict[str, MatrixSource],
    optimizers: dict[OptimizerEngine, RouteOptimizer],
    now: datetime,
    generation: int | None = None,
    max_wall_seconds: float = 30,
) -> bool:
    async with factory() as session, session.begin():
        row = await session.scalar(
            select(RiderReplacement)
            .where(
                RiderReplacement.replacement_id == identity,
                or_(
                    RiderReplacement.status == "REQUESTED",
                    (RiderReplacement.status == "APPROVED")
                    & (RiderReplacement.failure_code == "RECOMMENDATION_REQUESTED"),
                ),
                RiderReplacement.next_attempt_at <= now,
                or_(
                    RiderReplacement.recommendation_until.is_(None),
                    RiderReplacement.recommendation_until <= now,
                ),
            )
            .with_for_update(skip_locked=True)
        )
        if row is None:
            if generation is not None:
                from .recovery_work import current_work

                pending = await current_work(session, identity, "RECOMMEND")
                if pending and pending.generation == generation:
                    raise RuntimeError("RECOVERY_WORK_NOT_CLAIMABLE")
            return False
        from .recovery_work import begin_work

        if not await begin_work(session, identity, "RECOMMEND", now, generation):
            return False
        token = new_uuid7()
        row.recommendation_token, row.recommendation_until = token, now + timedelta(seconds=60)
        row.attempt += 1
    from .replacements import recommend_replacement

    try:
        return await recommend_replacement(
            factory,
            identity,
            providers=providers,
            optimizers=optimizers,
            now=now,
            recommendation_token=token,
            max_wall_seconds=max_wall_seconds,
        )
    except Exception:
        async with factory() as session, session.begin():
            current = await session.get(RiderReplacement, identity, with_for_update=True)
            if current and current.recommendation_token == token:
                current.failure_code = (
                    "RECOMMENDATION_REQUESTED"
                    if current.status == "APPROVED"
                    else "RECOMMENDATION_EXECUTION_FAILED"
                )
        return False
    finally:
        async with factory() as session, session.begin():
            row = await session.get(RiderReplacement, identity, with_for_update=True)
            if row and row.recommendation_token == token:
                row.recommendation_token = row.recommendation_until = None
                row.next_attempt_at = max(row.next_attempt_at, now + timedelta(seconds=5))
                from .recovery_work import finish_work

                await finish_work(session, row, now)


async def recommend_due(
    factory: async_sessionmaker[AsyncSession],
    region_id: UUID,
    *,
    providers: dict[str, MatrixSource],
    optimizers: dict[OptimizerEngine, RouteOptimizer],
    now: datetime,
    limit: int = 8,
) -> None:
    async with factory() as session:
        ids = list(
            await session.scalars(
                select(RiderReplacement.replacement_id)
                .where(
                    RiderReplacement.region_id == region_id,
                    or_(
                        RiderReplacement.status == "REQUESTED",
                        (RiderReplacement.status == "APPROVED")
                        & (RiderReplacement.failure_code == "RECOMMENDATION_REQUESTED"),
                    ),
                    RiderReplacement.next_attempt_at <= now,
                    or_(
                        RiderReplacement.recommendation_until.is_(None),
                        RiderReplacement.recommendation_until <= now,
                    ),
                )
                .order_by(RiderReplacement.next_attempt_at, RiderReplacement.requested_at)
                .limit(limit)
            )
        )
    for identity in ids:
        await recommend_one(factory, identity, providers=providers, optimizers=optimizers, now=now)


async def reconcile_terminal_obligations(
    factory: async_sessionmaker[AsyncSession], now: datetime, limit: int
) -> None:
    from tirodhan.modules.collection_requests.models import CollectionRequest
    from tirodhan.modules.planning.models import PickupExecution

    from .authority import lock_booking_population
    from .failure_models import RecoveryAssignmentItem

    async with factory() as session:
        identities = list(
            await session.scalars(
                select(RiderReplacement.replacement_id)
                .join(
                    CollectionRequest, CollectionRequest.request_id == RiderReplacement.request_id
                )
                .where(
                    RiderReplacement.failure_incident_id.is_not(None),
                    RiderReplacement.status.in_(("REQUESTED", "RECOMMENDED", "APPROVED")),
                    CollectionRequest.status.in_(("CANCELLED", "COMPLETED")),
                )
                .order_by(RiderReplacement.replacement_id)
                .limit(limit)
            )
        )
    for identity in identities:
        async with factory() as session, session.begin():
            initial = await session.get(RiderReplacement, identity)
            assert initial
            requests = await lock_booking_population(session, [initial.request_id])
            row = await session.get(RiderReplacement, identity, with_for_update=True)
            assert row
            if requests[row.request_id].status not in ("CANCELLED", "COMPLETED") or row.status in (
                "COMPLETED",
                "REJECTED",
            ):
                continue
            pickup = await session.scalar(
                select(PickupExecution).where(PickupExecution.request_id == row.request_id)
            )
            if (
                requests[row.request_id].status == "CANCELLED"
                and pickup
                and pickup.status == "COLLECTED"
            ):
                row.failure_code = "CANCELLED_CUSTODY_REQUIRES_ATTENTION"
                continue
            row.status = "REJECTED"
            row.failure_code = (
                "BOOKING_CANCELLED"
                if requests[row.request_id].status == "CANCELLED"
                else "BOOKING_COMPLETED"
            )
            row.version += 1
            item = await session.scalar(
                select(RecoveryAssignmentItem).where(
                    RecoveryAssignmentItem.replacement_id == identity,
                    RecoveryAssignmentItem.released_at.is_(None),
                )
            )
            if item:
                item.released_at = now
                leg = await session.get(
                    RecoveryAssignment, item.assignment_id, with_for_update=True
                )
                assert leg
                remaining = await session.scalar(
                    select(RecoveryAssignmentItem.replacement_id)
                    .where(
                        RecoveryAssignmentItem.assignment_id == leg.assignment_id,
                        RecoveryAssignmentItem.released_at.is_(None),
                    )
                    .limit(1)
                )
                if remaining is None:
                    leg.status = "SUPERSEDED"
                    leg.failure_code = "TERMINAL_BOOKING_OUTCOME"
                    leg.lease_token = leg.lease_until = None
                    leg.version += 1
                    from .recovery_work import finish_work

                    await finish_work(session, leg, now)
            from .recovery_work import finish_work

            await finish_work(session, row, now)


async def reconcile_recovery(
    factory: async_sessionmaker[AsyncSession], now: datetime, limit: int = 100
) -> int:
    """Finite indexed rediscovery. Publish debt only advances with its outbox record."""
    from tirodhan.modules.collection_requests.models import CollectionRequest
    from tirodhan.modules.planning.models import PickupExecution

    from .capacity_models import RiderReplacement
    from .dispatch_models import RouteTask
    from .reconciliation import reconcile_cancellations

    async with factory() as session:
        cancelled_regions = list(
            await session.scalars(
                select(RiderReplacement.region_id)
                .join(
                    CollectionRequest, CollectionRequest.request_id == RiderReplacement.request_id
                )
                .join(PickupExecution, PickupExecution.request_id == CollectionRequest.request_id)
                .join(
                    RouteTask, RouteTask.pickup_execution_id == PickupExecution.pickup_execution_id
                )
                .where(
                    RiderReplacement.failure_incident_id.is_not(None),
                    CollectionRequest.status == "CANCELLED",
                    RouteTask.state.in_(("PENDING", "STARTED")),
                )
                .distinct()
                .limit(limit)
            )
        )
    for region in cancelled_regions:
        await reconcile_cancellations(factory, now, region_id=region)
    await reconcile_terminal_obligations(factory, now, limit)
    from sqlalchemy import exists, func

    from .dispatch_models import RouteRevision, VehicleRoute
    from .failure_models import RecoveryWorkRequest, RiderFailureIncident
    from .fleet_models import FleetControl
    from .orchestration import prepare_due
    from .orchestration_models import RegionVehicle, RouteAcknowledgment, RouteDelivery

    async with factory() as session:
        scopes = list(
            await session.scalars(
                select(FleetControl.scope)
                .join(RegionVehicle, RegionVehicle.region_id == FleetControl.region_id)
                .join(VehicleRoute, VehicleRoute.vehicle_id == RegionVehicle.vehicle_id)
                .join(RouteRevision, RouteRevision.route_id == VehicleRoute.route_id)
                .join(RouteDelivery, RouteDelivery.revision_id == RouteRevision.revision_id)
                .where(
                    RouteRevision.version == VehicleRoute.version,
                    RouteDelivery.due_at <= now,
                    RouteDelivery.escalated_at.is_(None),
                    ~exists().where(RouteAcknowledgment.revision_id == RouteDelivery.revision_id),
                )
                .group_by(FleetControl.scope)
                .order_by(func.min(RouteDelivery.due_at))
                .limit(limit)
            )
        )
    for control_scope in scopes:
        await prepare_due(factory, now, control_scope=control_scope)
    async with factory() as session, session.begin():
        jobs = list(
            await session.scalars(
                select(RecoveryAssignment)
                .where(
                    RecoveryAssignment.status.in_(("PENDING", "RUNNING", "AWAITING_TRANSFER")),
                    RecoveryAssignment.due_at <= now,
                    or_(
                        RecoveryAssignment.lease_until.is_(None),
                        RecoveryAssignment.lease_until <= now,
                    ),
                    or_(
                        ~exists().where(
                            RecoveryWorkRequest.assignment_id == RecoveryAssignment.assignment_id,
                            RecoveryWorkRequest.state != "CLOSED",
                        ),
                        RecoveryAssignment.lease_until <= now,
                    ),
                )
                .order_by(RecoveryAssignment.due_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )
        for row in jobs:
            if row.lease_until is not None:
                from .recovery_work import close_work

                await close_work(
                    session, row.assignment_id, "ACTIVATE", now, "EXECUTION_LEASE_EXPIRED"
                )
                row.lease_token = row.lease_until = None
                row.status, row.failure_code = "PENDING", "EXECUTION_LEASE_EXPIRED"
                row.version += 1
            await enqueue(session, row.assignment_id, "ACTIVATE", now)
        requests = list(
            await session.scalars(
                select(RiderReplacement)
                .where(
                    or_(
                        RiderReplacement.status == "REQUESTED",
                        (RiderReplacement.status == "APPROVED")
                        & (RiderReplacement.failure_code == "RECOMMENDATION_REQUESTED"),
                    ),
                    RiderReplacement.failure_incident_id.is_not(None),
                    RiderReplacement.next_attempt_at <= now,
                    or_(
                        RiderReplacement.recommendation_until.is_(None),
                        RiderReplacement.recommendation_until <= now,
                    ),
                    or_(
                        ~exists().where(
                            RecoveryWorkRequest.replacement_id == RiderReplacement.replacement_id,
                            RecoveryWorkRequest.state != "CLOSED",
                        ),
                        RiderReplacement.recommendation_until <= now,
                    ),
                )
                .order_by(RiderReplacement.next_attempt_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )
        for request in requests:
            if request.recommendation_until is not None:
                from .recovery_work import close_work

                await close_work(
                    session, request.replacement_id, "RECOMMEND", now, "EXECUTION_LEASE_EXPIRED"
                )
                request.recommendation_token = request.recommendation_until = None
            await enqueue(session, request.replacement_id, "RECOMMEND", now)
        # An incident's resolution is independent of facility completion. Require
        # every recovery leg and the actual revision receipt, plus no unresolved work.
        incidents = list(
            await session.scalars(
                select(RiderFailureIncident)
                .where(
                    RiderFailureIncident.status == "OPEN",
                    ~exists().where(
                        RiderReplacement.failure_incident_id == RiderFailureIncident.incident_id,
                        RiderReplacement.status != "COMPLETED",
                        or_(
                            RiderReplacement.failure_code.is_(None),
                            RiderReplacement.failure_code.not_in(
                                ("BOOKING_CANCELLED", "BOOKING_COMPLETED")
                            ),
                        ),
                    ),
                )
                .order_by(RiderFailureIncident.created_at)
                .limit(limit)
            )
        )
        for incident in incidents:
            obligations = list(
                await session.scalars(
                    select(RiderReplacement).where(
                        RiderReplacement.failure_incident_id == incident.incident_id
                    )
                )
            )
            if not obligations or any(
                r.status != "COMPLETED"
                and r.failure_code not in ("BOOKING_CANCELLED", "BOOKING_COMPLETED")
                for r in obligations
            ):
                continue
            pending = False
            for obligation in obligations:
                revisions = await recovery_revisions(
                    session, obligation.activation_id, obligation.recommended_shift_id
                )
                for revision in revisions:
                    root = await session.get(VehicleRoute, revision.route_id)
                    if root and root.status != "CLOSED":
                        current = await session.scalar(
                            select(RouteRevision.revision_id).where(
                                RouteRevision.route_id == root.route_id,
                                RouteRevision.version == root.version,
                            )
                        )
                        if current and await session.get(RouteAcknowledgment, current) is None:
                            pending = True
            if not pending:
                incident.status = "RESOLVED"
                incident.version += 1
        return len(jobs) + len(requests)


async def activate_due(
    factory: async_sessionmaker[AsyncSession],
    region_id: UUID,
    *,
    providers: dict[str, MatrixSource],
    optimizers: dict[OptimizerEngine, RouteOptimizer],
    now: datetime,
    limit: int = 2,
) -> None:
    from .failure_execution import activate_recovery
    from .failure_models import RiderFailureIncident

    async with factory() as session:
        identities = list(
            await session.scalars(
                select(RecoveryAssignment.assignment_id)
                .join(RiderFailureIncident)
                .where(
                    RiderFailureIncident.region_id == region_id,
                    RecoveryAssignment.status.in_(("PENDING", "AWAITING_TRANSFER", "RUNNING")),
                    RecoveryAssignment.due_at <= now,
                    or_(
                        RecoveryAssignment.lease_until.is_(None),
                        RecoveryAssignment.lease_until <= now,
                    ),
                )
                .order_by(RecoveryAssignment.due_at)
                .limit(limit)
            )
        )
    for identity in identities:
        await activate_recovery(
            factory, identity, providers=providers, optimizers=optimizers, now=now
        )
