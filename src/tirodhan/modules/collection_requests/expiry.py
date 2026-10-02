from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.payments.models import Payment


async def expire_pending_collection_requests(
    session: AsyncSession, evaluation_time: datetime
) -> int:
    """
    Expire collection requests and their associated payments that have passed their
    payment expiry window. Uses row-level locking to avoid race conditions.
    """
    # Find candidates
    stmt = (
        select(CollectionRequest.request_id, Payment.payment_id)
        .join(Payment, Payment.request_id == CollectionRequest.request_id)
        .where(
            CollectionRequest.status == "PENDING_PAYMENT",
            Payment.status == "PENDING",
            CollectionRequest.payment_expires_at <= evaluation_time,
        )
    )
    result = await session.execute(stmt)
    candidates = result.all()

    expired_count = 0
    for req_id, pay_id in candidates:
        # Lock payment first, then collection request to avoid deadlocks
        payment_stmt = select(Payment).where(Payment.payment_id == pay_id).with_for_update()
        payment = await session.scalar(payment_stmt)
        if not payment:
            continue

        request_stmt = (
            select(CollectionRequest)
            .where(CollectionRequest.request_id == req_id)
            .with_for_update()
        )
        request = await session.scalar(request_stmt)
        if not request:
            continue

        # Revalidate after locking
        if (
            payment.status == "PENDING"
            and payment.successful_attempt_id is None
            and request.status == "PENDING_PAYMENT"
            and request.payment_expires_at <= evaluation_time
        ):
            request.status = "EXPIRED"
            request.expired_at = evaluation_time
            payment.status = "EXPIRED"
            payment.expired_at = evaluation_time
            expired_count += 1

    return expired_count
