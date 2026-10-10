"""Charge accounting and liabilities; callers own the Payment transaction lock."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import false, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.payments.models import (
    CapturedCharge,
    FinancialException,
    Payment,
    PaymentAttempt,
    PaymentProviderEvent,
    Refund,
    RefundObligation,
    SettlementEvidence,
)
from tirodhan.modules.payments.ports import AuthenticatedPaymentEvent
from tirodhan.modules.reliability.models import OutboxEvent
from tirodhan.modules.reliability.primitives import append_outbox_event


async def status_event(session: AsyncSession, payment: Payment, key: str, status: str) -> None:
    if await session.scalar(
        select(OutboxEvent.outbox_event_id).where(
            OutboxEvent.event_key == f"customer-financial:{key}"
        )
    ):
        return
    await append_outbox_event(
        session,
        event_key=f"customer-financial:{key}",
        aggregate_type="collection_request",
        aggregate_id=payment.request_id,
        event_type="CustomerFinancialStatusChanged",
        payload={"request_id": str(payment.request_id), "change": status},
    )


async def open_exception(
    session: AsyncSession,
    payment: Payment,
    reason: str,
    *,
    charge: CapturedCharge | None = None,
    refund: Refund | None = None,
    evidence_id: UUID | None = None,
) -> FinancialException:
    target_id = (
        refund.refund_id if refund else charge.captured_charge_id if charge else payment.payment_id
    )
    key = f"{reason}:{target_id}"
    await session.execute(
        insert(FinancialException)
        .values(
            financial_exception_id=new_uuid7(),
            payment_id=payment.payment_id,
            captured_charge_id=charge.captured_charge_id if charge else None,
            refund_id=refund.refund_id if refund else None,
            evidence_id=evidence_id,
            case_key=key,
            reason_code=reason,
            status="OPEN",
            created_at=utc_now(),
        )
        .on_conflict_do_nothing(index_elements=["case_key"])
    )
    case = await session.scalar(
        select(FinancialException).where(FinancialException.case_key == key)
    )
    assert case is not None
    if await session.scalar(
        select(OutboxEvent.outbox_event_id).where(
            OutboxEvent.event_key == f"financial-exception:{case.financial_exception_id}"
        )
    ):
        return case
    await append_outbox_event(
        session,
        event_key=f"financial-exception:{case.financial_exception_id}",
        aggregate_type="payment",
        aggregate_id=payment.payment_id,
        event_type="FinancialExceptionRaised",
        payload={"financial_exception_id": str(case.financial_exception_id)},
    )
    return case


async def record_charge(
    session: AsyncSession,
    payment: Payment,
    attempt: PaymentAttempt,
    record: PaymentProviderEvent,
    event: AuthenticatedPaymentEvent,
) -> CapturedCharge | None:
    if not event.provider_payment_id or event.amount_minor is None or event.currency is None:
        return None  # Legacy provider-neutral tests/history have no verified capture facts.
    await session.flush([record])
    await session.execute(
        insert(CapturedCharge)
        .values(
            captured_charge_id=new_uuid7(),
            payment_id=payment.payment_id,
            payment_attempt_id=attempt.payment_attempt_id,
            provider=event.provider,
            provider_payment_id=event.provider_payment_id,
            provider_order_id=event.provider_order_id or attempt.provider_order_id,
            provider_account_key=event.provider_account_key,
            amount_minor=event.amount_minor,
            currency=event.currency,
            evidence_id=record.payment_provider_event_id,
            captured_at=utc_now(),
        )
        .on_conflict_do_nothing(index_elements=["provider", "provider_payment_id"])
    )
    charge = await session.scalar(
        select(CapturedCharge).where(
            CapturedCharge.provider == event.provider,
            CapturedCharge.provider_payment_id == event.provider_payment_id,
        )
    )
    assert charge is not None
    if (
        charge.payment_id != payment.payment_id
        or charge.payment_attempt_id != attempt.payment_attempt_id
        or charge.amount_minor != event.amount_minor
        or charge.currency != event.currency
        or (
            event.provider_order_id is not None
            and charge.provider_order_id != event.provider_order_id
        )
    ):
        await open_exception(
            session,
            payment,
            "CHARGE_IDENTITY_CONFLICT",
            evidence_id=record.payment_provider_event_id,
        )
        return None
    # Refund/dispute observations can precede the capture webhook. An earlier
    # unmatched money movement must not disappear when an auto-compensation
    # obligation is first created. Provider payment identity is the match key.
    prior = list(
        await session.scalars(
            select(PaymentProviderEvent)
            .where(
                PaymentProviderEvent.provider == charge.provider,
                PaymentProviderEvent.provider_payment_id == charge.provider_payment_id,
                PaymentProviderEvent.processing_status == "UNMATCHED",
                or_(
                    PaymentProviderEvent.provider_refund_id.is_not(None),
                    PaymentProviderEvent.provider_dispute_id.is_not(None),
                ),
            )
            .order_by(PaymentProviderEvent.payment_provider_event_id)
            .limit(100)
        )
    )
    for evidence in prior:
        reason = "PAYMENT_DISPUTE" if evidence.provider_dispute_id else "EXTERNAL_REFUND_UNMAPPED"
        if (
            evidence.provider_account_key != charge.provider_account_key
            or evidence.currency != charge.currency
            or evidence.amount_minor is None
            or evidence.amount_minor > charge.amount_minor
        ):
            reason = "PROVIDER_EVIDENCE_MISMATCH"
        evidence.payment_attempt_id = charge.payment_attempt_id
        evidence.processing_status = "RECONCILIATION_REQUIRED"
        evidence.failure_code = reason
        await open_exception(
            session, payment, reason, charge=charge, evidence_id=evidence.payment_provider_event_id
        )
        obligation = await session.scalar(
            select(RefundObligation).where(
                RefundObligation.captured_charge_id == charge.captured_charge_id
            )
        )
        if obligation:
            obligation.payout_blocked = True
    reports = list(
        await session.scalars(
            select(SettlementEvidence)
            .where(
                SettlementEvidence.provider_payment_id == charge.provider_payment_id,
                SettlementEvidence.refund_id.is_(None),
                or_(
                    SettlementEvidence.movement_type.in_(["refund", "adjustment"]),
                    SettlementEvidence.provider_dispute_id.is_not(None),
                ),
            )
            .order_by(SettlementEvidence.settlement_evidence_id)
            .limit(100)
        )
    )
    for report in reports:
        reason = (
            "SETTLEMENT_UNKNOWN_REFUND"
            if report.movement_type == "refund"
            else "SETTLEMENT_ADJUSTMENT_REVIEW"
        )
        if report.provider_account_key != charge.provider_account_key:
            reason = "PROVIDER_EVIDENCE_MISMATCH"
        case = await open_exception(session, payment, reason, charge=charge)
        case.settlement_evidence_id = report.settlement_evidence_id
        obligation = await session.scalar(
            select(RefundObligation).where(
                RefundObligation.captured_charge_id == charge.captured_charge_id
            )
        )
        if obligation:
            obligation.payout_blocked = True
    return charge


async def resolve_uncertainty(
    session: AsyncSession, payment: Payment, *, refund: Refund | None = None
) -> None:
    """Close settled outcome cases; commercial/identity cases still need review."""
    reasons = {"REFUND_UNRESOLVED" if refund else "PAYMENT_UNRESOLVED"}
    if refund and refund.status == "SUCCEEDED":
        reasons.add("REFUND_FAILED")
    await session.execute(
        update(FinancialException)
        .where(
            FinancialException.payment_id == payment.payment_id,
            FinancialException.reason_code.in_(reasons),
            FinancialException.refund_id == refund.refund_id
            if refund
            else FinancialException.refund_id.is_(None),
            FinancialException.status == "OPEN",
        )
        .values(status="RESOLVED", resolved_at=utc_now())
    )


async def ensure_obligation(
    session: AsyncSession, charge: CapturedCharge, reason: str, amount_minor: int | None = None
) -> RefundObligation:
    obligation = await session.scalar(
        select(RefundObligation).where(
            RefundObligation.captured_charge_id == charge.captured_charge_id
        )
    )
    if obligation is None:
        blocked = await session.scalar(
            select(FinancialException.financial_exception_id)
            .where(
                FinancialException.captured_charge_id == charge.captured_charge_id,
                FinancialException.status == "OPEN",
                FinancialException.reason_code.in_(
                    [
                        "PAYMENT_DISPUTE",
                        "REFUND_DISPUTE_DOUBLE_CREDIT_RISK",
                        "EXTERNAL_REFUND_UNMAPPED",
                        "EVENT_PAYLOAD_CONFLICT",
                        "PROVIDER_EVIDENCE_MISMATCH",
                        "SETTLEMENT_MISMATCH",
                        "SETTLEMENT_UNKNOWN_REFUND",
                        "SETTLEMENT_DISPUTE_REVIEW",
                        "SETTLEMENT_EXPECTED_MISSING",
                        "SETTLEMENT_ADJUSTMENT_REVIEW",
                        "REFUND_IDENTITY_CONFLICT",
                    ]
                ),
            )
            .limit(1)
        )
        obligation = RefundObligation(
            captured_charge_id=charge.captured_charge_id,
            amount_minor=amount_minor or charge.amount_minor,
            reason_code=reason,
            payout_blocked=blocked is not None,
        )
        session.add(obligation)
        await session.flush([obligation])
    return obligation


async def compensate_charge(
    session: AsyncSession,
    payment: Payment,
    charge: CapturedCharge,
    reason: str,
    *,
    expires_at: datetime,
    actor: UUID | None = None,
) -> Refund | None:
    from tirodhan.modules.payments.refunds import RefundConflictError, create_refund

    obligation = await ensure_obligation(session, charge, reason)
    attempt = await session.get(PaymentAttempt, charge.payment_attempt_id)
    existing = list(
        await session.scalars(
            select(Refund).where(
                Refund.payment_id == payment.payment_id,
                or_(
                    Refund.captured_charge_id == charge.captured_charge_id,
                    (
                        Refund.captured_charge_id.is_(None)
                        & (Refund.payment_attempt_id == charge.payment_attempt_id)
                    )
                    if attempt and attempt.provider_payment_id == charge.provider_payment_id
                    else false(),
                ),
            )
        )
    )
    # A failed operation is historical debt, not an invitation for automatic replacement.
    if existing:
        reserved = sum(r.amount_minor for r in existing if r.non_payable_verified_at is None)
        if (
            reserved not in {0, charge.amount_minor}
            or obligation.amount_minor != charge.amount_minor
        ):
            await open_exception(session, payment, "REFUND_REVIEW_REQUIRED", charge=charge)
            raise RefundConflictError("Partial compensation requires financial review")
        return None
    if obligation.payout_blocked:
        raise RefundConflictError("Refund obligation is blocked")
    return await create_refund(
        session,
        payment_id=payment.payment_id,
        payment_attempt_id=charge.payment_attempt_id,
        captured_charge_id=charge.captured_charge_id,
        refund_obligation_id=obligation.refund_obligation_id,
        amount_minor=charge.amount_minor,
        reason_code=reason,
        idempotency_key=f"charge-compensation:{charge.captured_charge_id}",
        idempotency_expires_at=expires_at,
        requested_by_user_id=actor,
    )
