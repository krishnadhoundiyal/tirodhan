from datetime import timedelta
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.payments.models import Payment, PaymentAttempt, Refund
from tirodhan.modules.payments.ports import (
    PaymentProviderUncertainError,
    RefundInitiationOutcome,
    RefundProvider,
)
from tirodhan.modules.reliability.primitives import (
    IdempotencyKeyConflictError,
    append_outbox_event,
    claim_idempotency_record,
    complete_idempotency_record,
    get_completed_idempotency_result,
)


async def create_refund(
    session: AsyncSession,
    *,
    payment_id: UUID,
    payment_attempt_id: UUID,
    amount_minor: int,
    reason_code: str,
    idempotency_key: str,
    requested_by_user_id: UUID | None = None,
) -> Refund:
    if amount_minor <= 0:
        raise HTTPException(status_code=400, detail="Refund amount must be positive")

    scope = f"refund.create:{payment_id}"
    try:
        claim = await claim_idempotency_record(
            session,
            scope=scope,
            idempotency_key=idempotency_key,
            request_fingerprint=(str(payment_attempt_id) + str(amount_minor)).encode(),
            expires_at=utc_now() + timedelta(days=1),  # placeholder
        )
    except IdempotencyKeyConflictError as e:
        raise HTTPException(status_code=409, detail="Idempotency conflict") from e

    if not claim.created:
        result = get_completed_idempotency_result(claim.record)
        if result is not None:
            # Replay: find the established Refund
            stmt = select(Refund).where(
                Refund.payment_id == payment_id, Refund.provider_idempotency_key == idempotency_key
            )
            refund = await session.scalar(stmt)
            if not refund:
                raise HTTPException(status_code=404, detail="Refund not found")
            return refund

    # Serialize through Payment
    payment_stmt = select(Payment).where(Payment.payment_id == payment_id).with_for_update()
    payment = await session.scalar(payment_stmt)

    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")

    if payment.status != "SUCCEEDED":
        raise HTTPException(status_code=409, detail="Payment has not succeeded")

    if payment.successful_attempt_id != payment_attempt_id:
        raise HTTPException(
            status_code=409, detail="Refund target must be the canonical successful attempt"
        )

    # Validate payment attempt
    attempt_stmt = select(PaymentAttempt).where(
        PaymentAttempt.payment_attempt_id == payment_attempt_id
    )
    attempt = await session.scalar(attempt_stmt)
    if not attempt or attempt.payment_id != payment.payment_id or attempt.status != "SUCCEEDED":
        raise HTTPException(status_code=409, detail="Invalid successful payment attempt")

    # Calculate reserved balance
    # Do not count definitively FAILED
    reserved_stmt = select(func.coalesce(func.sum(Refund.amount_minor), 0)).where(
        Refund.payment_id == payment_id,
        Refund.status.in_(
            ["PENDING", "PROCESSING", "SUBMITTED", "SUCCEEDED", "INITIATION_UNCERTAIN"]
        ),
    )
    reserved_amount = await session.scalar(reserved_stmt) or 0

    if amount_minor > payment.amount_minor - reserved_amount:
        raise HTTPException(
            status_code=409, detail="Requested refund exceeds remaining refundable balance"
        )

    refund = Refund(
        refund_id=new_uuid7(),
        payment_id=payment_id,
        payment_attempt_id=payment_attempt_id,
        amount_minor=amount_minor,
        currency=payment.currency,
        reason_code=reason_code,
        status="PENDING",
        provider=attempt.provider,
        provider_refund_id=None,
        provider_idempotency_key=idempotency_key,
        requested_by_user_id=requested_by_user_id,
        created_at=utc_now(),
    )
    session.add(refund)

    # Append outbox event
    await append_outbox_event(
        session,
        event_key=f"refund-requested:{refund.refund_id}",
        aggregate_type="refund",
        aggregate_id=refund.refund_id,
        event_type="RefundRequested",
        payload={"refund_id": str(refund.refund_id)},
    )

    await complete_idempotency_record(
        session, claim.record, result_resource_id=refund.refund_id, result_status_code=201
    )

    return refund


async def execute_refund_provider_call(
    session_factory: async_sessionmaker[AsyncSession],
    refund_id: UUID,
    provider: RefundProvider,
) -> None:
    # 1. Short DB transaction to claim and mark PROCESSING
    async with session_factory() as session:
        async with session.begin():
            stmt = select(Refund).where(Refund.refund_id == refund_id).with_for_update()
            refund = await session.scalar(stmt)
            if not refund:
                return  # Not found
            if refund.status != "PENDING":
                return  # Already processed or processing

            # Need the successful attempt's provider_payment_id
            attempt_stmt = select(PaymentAttempt).where(
                PaymentAttempt.payment_attempt_id == refund.payment_attempt_id
            )
            attempt = await session.scalar(attempt_stmt)
            if not attempt or not attempt.provider_payment_id:
                # Missing provider payment reference fails closed
                refund.status = "INITIATION_UNCERTAIN"
                return

            provider_payment_id = attempt.provider_payment_id
            amount_minor = refund.amount_minor
            currency = refund.currency
            provider_idempotency_key = refund.provider_idempotency_key

            refund.status = "PROCESSING"
            refund.processing_started_at = utc_now()

    # 2. Call provider outside DB transaction
    try:
        result = await provider.initiate_refund(
            refund_id=refund_id,
            provider_payment_id=provider_payment_id,
            amount_minor=amount_minor,
            currency=currency,
            provider_idempotency_key=provider_idempotency_key,
        )
    except PaymentProviderUncertainError:
        result = None
    except Exception:
        result = None

    # 3. New DB transaction to persist result
    async with session_factory() as session:
        async with session.begin():
            stmt = select(Refund).where(Refund.refund_id == refund_id).with_for_update()
            refund = await session.scalar(stmt)
            if not refund or refund.status != "PROCESSING":
                return

            if result is None or result.outcome == RefundInitiationOutcome.INITIATION_UNCERTAIN:
                refund.status = "INITIATION_UNCERTAIN"
            elif result.outcome == RefundInitiationOutcome.SUCCEEDED:
                refund.status = "SUCCEEDED"
                refund.provider_refund_id = result.provider_refund_id
                refund.completed_at = utc_now()
            elif result.outcome == RefundInitiationOutcome.SUBMITTED:
                refund.status = "SUBMITTED"
                if result.provider_refund_id:
                    refund.provider_refund_id = result.provider_refund_id
            elif result.outcome == RefundInitiationOutcome.FAILED:
                refund.status = "FAILED"
                refund.completed_at = utc_now()
