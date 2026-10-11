from datetime import datetime
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tirodhan.db.values import utc_now
from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.payments.accounting import status_event
from tirodhan.modules.payments.models import Payment
from tirodhan.modules.payments.refunds import RefundConflictError, ensure_cancellation_refund
from tirodhan.modules.planning.locking import acquire_work_unit_advisory_lock
from tirodhan.modules.planning.models import PlanningBatch
from tirodhan.modules.planning.policy import planning_cutoff_reached
from tirodhan.modules.reliability.primitives import (
    IdempotencyKeyConflictError,
    claim_idempotency_record,
    complete_idempotency_record,
    get_completed_idempotency_result,
)


class CancellationNotAuthorizedError(RuntimeError):
    pass


class CancellationNotFoundError(RuntimeError):
    pass


class CancellationConflictError(RuntimeError):
    def __init__(self, message: str, code: str = "NOT_ELIGIBLE") -> None:
        super().__init__(message)
        self.code = code


async def cancel_collection_request_by_customer(
    session: AsyncSession,
    *,
    request_id: UUID,
    customer_id: UUID,
    idempotency_key: str,
    planning_lead_time_minutes: int | None,
    idempotency_expires_at: datetime,
) -> CollectionRequest:
    # 1. Short read to get the stable work-unit identity used for lock acquisition.
    stmt = select(CollectionRequest).where(CollectionRequest.request_id == request_id)
    request_identity = await session.scalar(stmt)

    if not request_identity:
        raise CancellationNotFoundError("Collection request not found")

    if request_identity.customer_id != customer_id:
        raise CancellationNotAuthorizedError("Not authorized to cancel this request")

    identity_cell_id = request_identity.cell_id
    identity_slot_start = request_identity.slot_start
    identity_slot_end = request_identity.slot_end

    # 2. Claim idempotency record
    scope = f"collection-request.cancel:{request_id}"
    fingerprint = str(customer_id)

    try:
        claim = await claim_idempotency_record(
            session,
            scope=scope,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint.encode(),
            expires_at=idempotency_expires_at,
        )
    except IdempotencyKeyConflictError as e:
        raise IdempotencyKeyConflictError("Idempotency conflict") from e

    if not claim.created:
        result = get_completed_idempotency_result(claim.record)
        if result is not None:
            # Exact replay, return established CANCELLED request.
            request = await session.scalar(
                select(CollectionRequest)
                .where(CollectionRequest.request_id == request_id)
                .execution_options(populate_existing=True)
            )
            if not request:
                raise CancellationNotFoundError("Collection request not found")
            return request

    # 3. Payment precedes work-unit/request locks, matching capture and expiry.
    payment = await session.scalar(
        select(Payment)
        .where(Payment.request_id == request_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if payment is None:
        raise CancellationConflictError("Collection payment is missing")

    # 4. Acquire the same work-unit advisory lock used by planning freeze.
    await acquire_work_unit_advisory_lock(
        session,
        cell_id=identity_cell_id,
        slot_start=identity_slot_start,
        slot_end=identity_slot_end,
    )

    # 5. Reload CollectionRequest FOR UPDATE from database truth. The short read above
    # already placed this entity in the SQLAlchemy identity map, so populate_existing is
    # required after waiting on the advisory lock; otherwise a planning winner can leave
    # this session holding stale ACCEPTED state and cancellation could overwrite it.
    reload_stmt = (
        select(CollectionRequest)
        .where(CollectionRequest.request_id == request_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    request = cast(CollectionRequest | None, await session.scalar(reload_stmt))

    if not request:
        raise CancellationNotFoundError("Collection request not found")

    if request.customer_id != customer_id:
        raise CancellationNotAuthorizedError("Not authorized to cancel this request")

    if (
        request.cell_id != identity_cell_id
        or request.slot_start != identity_slot_start
        or request.slot_end != identity_slot_end
    ):
        raise CancellationConflictError("Collection request work unit changed during cancellation")

    # Idempotency: a different fresh cancellation command against an already-CANCELLED request
    # must produce no second business effect. We still mark idempotency complete.
    if request.status == "CANCELLED":
        await complete_idempotency_record(
            session, claim.record, result_resource_id=request.request_id, result_status_code=200
        )
        return request

    # 6. Verify cutoff not reached using planning_cutoff_reached.
    time_now = utc_now()
    if planning_cutoff_reached(
        slot_start=request.slot_start,
        now=time_now,
        lead_time_minutes=planning_lead_time_minutes,
    ):
        raise CancellationConflictError(
            "Planning cutoff has been reached; request cannot be cancelled",
            "PLANNING_CUTOFF_REACHED",
        )

    # 7. Verify cancellable status and that the work unit has not been frozen.
    frozen = await session.scalar(
        select(PlanningBatch.planning_batch_id).where(
            PlanningBatch.cell_id == request.cell_id,
            PlanningBatch.slot_start == request.slot_start,
            PlanningBatch.slot_end == request.slot_end,
        )
    )
    if request.planning_batch_id is not None or frozen is not None:
        raise CancellationConflictError("Planning has started", "PLANNING_STARTED")
    if request.status not in {"ACCEPTED", "PENDING_PAYMENT"}:
        raise CancellationConflictError("Request cannot be cancelled in its current state")

    if payment.amount_minor != request.quoted_amount_minor or payment.currency != request.currency:
        raise CancellationConflictError("Collection financial accounting is inconsistent")
    if request.status == "ACCEPTED" and payment.status != "SUCCEEDED":
        raise CancellationConflictError("Accepted collection has no confirmed payment")
    try:
        await ensure_cancellation_refund(
            session,
            payment,
            customer_id=customer_id,
            idempotency_expires_at=idempotency_expires_at,
        )
    except RefundConflictError as error:
        raise CancellationConflictError("Compensation requires financial review") from error

    # 8. Row is locked and freshly revalidated, so this transition cannot overwrite a
    # concurrent planning winner.
    request.status = "CANCELLED"
    request.cancelled_at = time_now
    payment.cancellation_compensation_authorized = True
    await status_event(
        session, payment, f"collection-cancelled:{request_id}", "COLLECTION_CANCELLED"
    )
    if payment.status == "PENDING":
        payment.status = "CANCELLED"
        payment.cancelled_at = time_now

    # 9. Complete idempotency in the same transaction.
    await complete_idempotency_record(
        session, claim.record, result_resource_id=request.request_id, result_status_code=200
    )

    return request
