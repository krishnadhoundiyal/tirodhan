"""Shared, bounded financial snapshots for standalone and embedded customer reads."""

from collections import defaultdict
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.customer_reads.errors import CustomerReadError
from tirodhan.modules.payments.models import (
    CapturedCharge,
    Payment,
    PaymentAttempt,
    PaymentProviderEvent,
    Refund,
)


@dataclass
class FinancialSnapshot:
    payment: Payment
    attempts: list[PaymentAttempt]
    refunds: list[Refund]
    reconciliation: bool


async def load_financials(
    session: AsyncSession, requests: list[CollectionRequest]
) -> dict[UUID, FinancialSnapshot]:
    if not requests:
        return {}
    payments = list(
        await session.scalars(
            select(Payment).where(Payment.request_id.in_([r.request_id for r in requests]))
        )
    )
    by_request = {p.request_id: p for p in payments}
    ids = [p.payment_id for p in payments]
    charges = {
        c.captured_charge_id: c
        for c in await session.scalars(
            select(CapturedCharge).where(CapturedCharge.payment_id.in_(ids))
        )
    }
    attempts: dict[UUID, list[PaymentAttempt]] = defaultdict(list)
    for attempt in await session.scalars(
        select(PaymentAttempt)
        .where(PaymentAttempt.payment_id.in_(ids))
        .order_by(PaymentAttempt.created_at, PaymentAttempt.payment_attempt_id)
    ):
        attempts[attempt.payment_id].append(attempt)
    refunds: dict[UUID, list[Refund]] = defaultdict(list)
    for refund in await session.scalars(
        select(Refund)
        .where(Refund.payment_id.in_(ids))
        .order_by(Refund.created_at, Refund.refund_id)
    ):
        refunds[refund.payment_id].append(refund)
    reconciliation = set(
        await session.scalars(
            select(PaymentAttempt.payment_id)
            .join(
                PaymentProviderEvent,
                PaymentProviderEvent.payment_attempt_id == PaymentAttempt.payment_attempt_id,
            )
            .where(
                PaymentAttempt.payment_id.in_(ids),
                PaymentProviderEvent.processing_status == "RECONCILIATION_REQUIRED",
            )
            .distinct()
        )
    )
    result = {}
    for request in requests:
        payment = by_request.get(request.request_id)
        if payment is None or (
            payment.amount_minor != request.quoted_amount_minor
            or payment.currency != request.currency
            or payment.currency != "INR"
            or payment.status not in {"PENDING", "SUCCEEDED", "CANCELLED", "EXPIRED"}
            or (
                request.status in {"ACCEPTED", "PRE_PLANNING", "PLANNED", "COMPLETED"}
                and payment.status != "SUCCEEDED"
            )
        ):
            raise CustomerReadError(503, "NOT_ELIGIBLE")
        current = attempts[payment.payment_id]
        if any(
            a.status not in {"CREATED", "PENDING", "FAILED", "SUCCEEDED", "INITIATION_UNCERTAIN"}
            for a in current
        ):
            raise CustomerReadError(503, "NOT_ELIGIBLE")
        canonical = next(
            (a for a in current if a.payment_attempt_id == payment.successful_attempt_id), None
        )
        if payment.status == "SUCCEEDED" and (
            canonical is None
            or canonical.status != "SUCCEEDED"
            or not canonical.provider_payment_id
            or payment.succeeded_at is None
        ):
            raise CustomerReadError(503, "NOT_ELIGIBLE")
        refunded = refunds[payment.payment_id]
        if (
            (bool(refunded) and payment.status != "SUCCEEDED")
            or any(
                r.currency != payment.currency
                or r.amount_minor <= 0
                or (
                    r.captured_charge_id is None
                    and r.payment_attempt_id != payment.successful_attempt_id
                )
                or (
                    r.captured_charge_id is not None
                    and (
                        r.captured_charge_id not in charges
                        or charges[r.captured_charge_id].payment_id != payment.payment_id
                        or charges[r.captured_charge_id].payment_attempt_id != r.payment_attempt_id
                        or charges[r.captured_charge_id].currency != r.currency
                    )
                )
                or not any(
                    a.payment_attempt_id == r.payment_attempt_id
                    and a.provider == r.provider
                    and a.status == "SUCCEEDED"
                    and bool(a.provider_payment_id)
                    for a in current
                )
                for r in refunded
            )
            or sum(
                r.amount_minor
                for r in refunded
                if r.captured_charge_id is None and r.non_payable_verified_at is None
            )
            > payment.amount_minor
            or any(
                sum(
                    r.amount_minor
                    for r in refunded
                    if r.captured_charge_id == c.captured_charge_id
                    and r.non_payable_verified_at is None
                )
                > c.amount_minor
                for c in charges.values()
                if c.payment_id == payment.payment_id
            )
        ):
            raise CustomerReadError(503, "NOT_ELIGIBLE")
        result[request.request_id] = FinancialSnapshot(
            payment, current, refunded, payment.payment_id in reconciliation
        )
    return result
