"""Customer actions derived from persisted evidence; no financial transitions."""

from datetime import datetime
from enum import Enum

from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.payments.models import Payment, PaymentAttempt


class PaymentAction(str, Enum):
    START = "START"
    RESUME = "RESUME"
    WAIT = "WAIT"
    UNAVAILABLE = "UNAVAILABLE"


class PaymentActionReason(str, Enum):
    NEW_PAYMENT = "NEW_PAYMENT"
    CHECKOUT_READY = "CHECKOUT_READY"
    VERIFICATION_REQUIRED = "VERIFICATION_REQUIRED"
    PREPARATION_IN_PROGRESS = "PREPARATION_IN_PROGRESS"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    BOOKING_INELIGIBLE = "BOOKING_INELIGIBLE"
    ACCOUNT_MISMATCH = "ACCOUNT_MISMATCH"
    PAYMENT_RECEIVED = "PAYMENT_RECEIVED"
    SUPERSEDED_ATTEMPT = "SUPERSEDED_ATTEMPT"


def initial_order_rejected(attempt: PaymentAttempt) -> bool:
    return (
        attempt.status == "FAILED"
        and attempt.provider_order_id is None
        and (attempt.provider != "RAZORPAY" or attempt.failure_code == "RAZORPAY_ORDER_REJECTED")
    )


def payment_action(
    request: CollectionRequest,
    payment: Payment,
    attempts: list[PaymentAttempt],
    *,
    now: datetime,
    booking_blocked: bool,
    reconciliation: bool,
    account_key: str | None = None,
) -> tuple[PaymentAction, PaymentActionReason]:
    if payment.status == "SUCCEEDED":
        return PaymentAction.UNAVAILABLE, PaymentActionReason.PAYMENT_RECEIVED
    if (
        booking_blocked
        or payment.status != "PENDING"
        or request.status != "PENDING_PAYMENT"
        or request.payment_expires_at <= now
    ):
        return PaymentAction.UNAVAILABLE, PaymentActionReason.BOOKING_INELIGIBLE
    exposed = [a for a in attempts if not initial_order_rejected(a)]
    if reconciliation or len(exposed) > 1:
        return PaymentAction.WAIT, PaymentActionReason.RECONCILIATION_REQUIRED
    if not exposed:
        return PaymentAction.START, PaymentActionReason.NEW_PAYMENT
    attempt = exposed[0]
    if attempt.provider == "RAZORPAY" and (
        not account_key or account_key != attempt.provider_account_key
    ):
        return PaymentAction.UNAVAILABLE, PaymentActionReason.ACCOUNT_MISMATCH
    if attempt.claim_until and attempt.claim_until > now:
        return PaymentAction.WAIT, PaymentActionReason.PREPARATION_IN_PROGRESS
    if (
        attempt.status == "PENDING"
        and attempt.provider_order_id
        and attempt.checkout_ready_until
        and attempt.checkout_ready_until > now
    ):
        action = PaymentAction.RESUME if attempt.observation_version else PaymentAction.START
        return action, PaymentActionReason.CHECKOUT_READY
    return PaymentAction.WAIT, PaymentActionReason.VERIFICATION_REQUIRED
