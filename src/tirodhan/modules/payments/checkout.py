from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.payments.models import Payment, PaymentAttempt
from tirodhan.modules.payments.ports import (
    CheckoutConfirmationError,
    CheckoutConfirmationVerifier,
)


async def confirm_checkout(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    customer_id: UUID,
    attempt_id: UUID,
    order_id: str,
    payment_id: str,
    signature: str,
    verifier: CheckoutConfirmationVerifier,
) -> PaymentAttempt:
    try:
        async with session_factory() as session, session.begin():
            attempt = await session.scalar(
                select(PaymentAttempt)
                .join(Payment, Payment.payment_id == PaymentAttempt.payment_id)
                .join(CollectionRequest, CollectionRequest.request_id == Payment.request_id)
                .where(
                    PaymentAttempt.payment_attempt_id == attempt_id,
                    CollectionRequest.customer_id == customer_id,
                    PaymentAttempt.provider == "RAZORPAY",
                )
                .with_for_update(of=PaymentAttempt)
            )
            if attempt is None:
                raise CheckoutConfirmationError("Owned payment attempt not found")
            if attempt.provider_order_id is None or order_id != attempt.provider_order_id:
                raise CheckoutConfirmationError("Checkout order does not match stored order")
            verifier.verify_checkout_signature(
                stored_order_id=attempt.provider_order_id,
                payment_id=payment_id,
                signature=signature,
            )
            if (
                attempt.provider_payment_id is not None
                and attempt.provider_payment_id != payment_id
            ):
                raise CheckoutConfirmationError("Checkout payment reference conflicts")
            # Natural row replay; no financial status transition or event publication.
            attempt.provider_payment_id = payment_id
            await session.flush()
            return attempt
    except IntegrityError:
        raise CheckoutConfirmationError("Checkout payment reference conflicts") from None
