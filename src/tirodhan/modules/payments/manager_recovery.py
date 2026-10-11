"""Audited investigation and bounded recovery of existing financial records."""

import hashlib
from datetime import date, timedelta
from typing import Literal
from uuid import UUID

from sqlalchemy import false, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.db.values import utc_now
from tirodhan.modules.customers.service import command_fingerprint
from tirodhan.modules.payments.accounting import PAYOUT_HOLD_REASONS, resolve_uncertainty
from tirodhan.modules.payments.models import (
    CapturedCharge,
    FinancialAudit,
    FinancialException,
    Payment,
    PaymentAttempt,
    PaymentProviderEvent,
    Refund,
    RefundObligation,
    SettlementEvidence,
)
from tirodhan.modules.payments.ports import FinancialInquiryProvider, PaymentProviderUncertainError
from tirodhan.modules.payments.razorpay import RazorpayProvider
from tirodhan.modules.payments.reconciliation import (
    ReconciliationPolicy,
    _manager_lock,
    inquire_target,
)
from tirodhan.modules.payments.refunds import RefundConflictError
from tirodhan.modules.payments.service import process_authenticated_payment_event
from tirodhan.modules.payments.settlement import SettlementMovement, record_settlement
from tirodhan.modules.reliability.primitives import (
    IdempotencyClaim,
    append_outbox_event,
    claim_idempotency_record,
    complete_idempotency_record,
    get_completed_idempotency_result,
)

RecoveryAction = Literal[
    "RESOLVE_EXCEPTION", "INVESTIGATE_PROVIDER", "INVESTIGATE_SETTLEMENT", "RECOVER_ORIGINAL"
]


async def recover_financial_record(
    factory: async_sessionmaker[AsyncSession],
    provider: FinancialInquiryProvider,
    *,
    actor: UUID,
    command_id: UUID,
    target_id: UUID,
    action: RecoveryAction,
    policy: ReconciliationPolicy,
    report_day: date | None = None,
    fee_includes_tax: bool | None = None,
    page_budget: int = 1,
) -> FinancialAudit:
    """GETs precede a Payment-first final transaction. No new payout identity."""
    model = {
        "RESOLVE_EXCEPTION": FinancialException,
        "INVESTIGATE_PROVIDER": PaymentProviderEvent,
        "INVESTIGATE_SETTLEMENT": SettlementEvidence,
        "RECOVER_ORIGINAL": Refund,
    }[action]
    async with factory() as session:
        original = await session.get(model, target_id)
        if original is None:
            raise RefundConflictError("Financial target is missing")
        case = original if isinstance(original, FinancialException) else None
        operation = original if isinstance(original, Refund) else None
        if case and case.refund_id:
            operation = await session.get(Refund, case.refund_id)
        evidence = original if isinstance(original, PaymentProviderEvent) else None
        settlement = original if isinstance(original, SettlementEvidence) else None
        if case and case.settlement_evidence_id:
            settlement = await session.get(SettlementEvidence, case.settlement_evidence_id)
        payment_id = case.payment_id if case else operation.payment_id if operation else None
        charge_id = (
            case.captured_charge_id if case else operation.captured_charge_id if operation else None
        )
        if charge_id is None and operation:
            charge_id = operation.captured_charge_id
        if settlement and settlement.captured_charge_id:
            charge_id = settlement.captured_charge_id
        charge = await session.get(CapturedCharge, charge_id) if charge_id else None
        if charge:
            payment_id = charge.payment_id
        if evidence and evidence.payment_attempt_id:
            attempt = await session.get(PaymentAttempt, evidence.payment_attempt_id)
            if attempt:
                payment_id = attempt.payment_id
        case_version = (
            (
                case.occurrence_count,
                case.evidence_id,
                case.settlement_evidence_id,
                case.expected_settlement_id,
                case.expected_settlement_due_at,
            )
            if case
            else None
        )
    fingerprint = command_fingerprint(
        dict(actor=actor, target=target_id, action=action, day=report_day)
    )
    expires = utc_now() + timedelta(seconds=policy.command_ttl_seconds)

    async def claim_command(
        session: AsyncSession,
    ) -> tuple[IdempotencyClaim, FinancialAudit | None]:
        if payment_id:
            await session.scalar(
                select(Payment).where(Payment.payment_id == payment_id).with_for_update()
            )
        await _manager_lock(session, actor)
        claim = await claim_idempotency_record(
            session,
            scope="financial.manager",
            idempotency_key=str(command_id),
            request_fingerprint=fingerprint,
            expires_at=expires,
        )
        result = get_completed_idempotency_result(claim.record)
        audit = (
            await session.get(FinancialAudit, result.resource_id)
            if result and result.resource_id
            else None
        )
        return claim, audit

    async with factory() as session, session.begin():
        _, completed = await claim_command(session)
        if completed:
            return completed
    decision = "PROVIDER_BLOCKED"
    fresh_event = None
    fresh_settlement = None
    inventory = None
    verified = False
    try:
        if operation and settlement is None:
            records = await inquire_target(
                factory, provider, operation.refund_id, refund=True, policy=policy
            )
            fresh_event = records[-1] if records else None
            verified = bool(fresh_event and fresh_event.processing_status == "PROCESSED")
            if charge and verified:
                inventory = await provider.inspect_charge_refunds(charge.provider_payment_id)
        elif evidence:
            if (
                not isinstance(provider, RazorpayProvider)
                or evidence.provider_account_key != provider.account_key
            ):
                raise RefundConflictError(
                    "Evidence account cannot be investigated with this credential"
                )
            reference = evidence.provider_refund_id or evidence.provider_payment_id
            if not reference:
                raise RefundConflictError("Provider reference is absent")
            observation = await provider.inquire_external_reference(
                reference, refund=bool(evidence.provider_refund_id)
            )
            fresh_event = await process_authenticated_payment_event(
                factory,
                observation,
                payload_hash=hashlib.sha256(observation.external_event_id.encode()).digest(),
                planning_lead_time_minutes=policy.planning_lead_time_minutes,
                idempotency_expires_at=expires,
            )
            verified = fresh_event.processing_status == "PROCESSED"
        elif settlement or (case and case.reason_code == "SETTLEMENT_EXPECTED_MISSING" and charge):
            if (
                not isinstance(provider, RazorpayProvider)
                or (
                    settlement.provider_account_key
                    if settlement
                    else charge.provider_account_key
                    if charge
                    else None
                )
                != provider.account_key
            ):
                raise RefundConflictError(
                    "Settlement account cannot be investigated with this credential"
                )
            if report_day is None or not 1 <= page_budget <= 100:
                raise RefundConflictError("A bounded report date is required")
            matches = []
            entity_id = (
                settlement.provider_entity_id
                if settlement
                else charge.provider_payment_id
                if charge
                else None
            )
            settlement_id = (
                settlement.provider_settlement_id
                if settlement
                else case.expected_settlement_id
                if case
                else None
            )
            exhausted = False
            for page in range(page_budget):
                items = await provider.settlement_page(report_day, page * 1000)
                for item in items:
                    movement = SettlementMovement.parse(item)
                    if (
                        movement.provider_entity_id == entity_id
                        and movement.provider_settlement_id == settlement_id
                    ):
                        matches.append(movement)
                if len(items) < 1000:
                    exhausted = True
                    break
            if not exhausted or len(matches) != 1:
                raise PaymentProviderUncertainError("SETTLEMENT_COVERAGE_INCOMPLETE")
            fresh_settlement = await record_settlement(
                factory, provider.account_key, matches[0], fee_includes_tax=fee_includes_tax
            )
            verified = (
                fresh_settlement.classification == "MATCHED"
                and fresh_settlement.settled
                and not fresh_settlement.on_hold
            )
            if fresh_settlement.classification in {
                "SETTLEMENT_ADJUSTMENT_REVIEW",
                "SETTLEMENT_DISPUTE_REVIEW",
            }:
                decision = "OWNER_DECISION_REQUIRED"
            if charge and verified:
                inventory = await provider.inspect_charge_refunds(charge.provider_payment_id)
        if decision != "OWNER_DECISION_REQUIRED":
            decision = "VERIFIED_MATCH" if verified else "PROVIDER_BLOCKED"
    except PaymentProviderUncertainError:
        decision = "PROVIDER_BLOCKED"
    # The provider cannot atomically join this transaction. Recheck retained evidence,
    # current local inventory and other holds before any release or recovery command.
    async with factory() as session, session.begin():
        claim, completed = await claim_command(session)
        if completed:
            return completed
        current_case = (
            await session.scalar(
                select(FinancialException)
                .where(FinancialException.financial_exception_id == target_id)
                .with_for_update()
            )
            if case
            else None
        )
        current_refund = (
            await session.scalar(
                select(Refund).where(Refund.refund_id == operation.refund_id).with_for_update()
            )
            if operation
            else None
        )
        if action == "RESOLVE_EXCEPTION" and current_case:
            allowed = {
                "REFUND_AMOUNT_MISMATCH",
                "REFUND_CURRENCY_MISMATCH",
                "REFUND_PAYMENT_MISMATCH",
                "REFUND_IDENTITY_CONFLICT",
                "SETTLEMENT_MISMATCH",
                "SETTLEMENT_EXPECTED_MISSING",
                "SETTLEMENT_UNKNOWN_REFUND",
            }
            unchanged = (
                current_case.occurrence_count,
                current_case.evidence_id,
                current_case.settlement_evidence_id,
                current_case.expected_settlement_id,
                current_case.expected_settlement_due_at,
            ) == case_version
            rows = (
                list(
                    await session.scalars(
                        select(Refund).where(
                            Refund.payment_id == payment_id,
                            or_(
                                Refund.captured_charge_id == charge_id,
                                (
                                    Refund.captured_charge_id.is_(None)
                                    & (Refund.payment_attempt_id == charge.payment_attempt_id)
                                )
                                if charge
                                else false(),
                            ),
                        )
                    )
                )
                if charge_id
                else []
            )
            # A locally unclaimed intent cannot have been POSTed by the worker:
            # first submission is durably claimed before external I/O. It remains
            # unpaid debt, but need not invent a provider refund to clear a
            # corrected settlement hold. Claimed/uncertain originals still need
            # actual provider identity; an empty GET does not prove their absence.
            submitted_rows = [
                r
                for r in rows
                if not (
                    r.status == "PENDING"
                    and r.processing_started_at is None
                    and r.provider_refund_id is None
                )
            ]
            expected = {
                r.provider_refund_id: (
                    r.amount_minor,
                    r.currency,
                    {"SUCCEEDED": "processed", "SUBMITTED": "pending", "FAILED": "failed"}.get(
                        r.status
                    ),
                )
                for r in submitted_rows
            }
            complete = (
                inventory is not None
                and all(r.provider_refund_id for r in submitted_rows)
                and inventory == expected
            )
            latest = (
                await session.scalar(
                    select(SettlementEvidence)
                    .where(
                        SettlementEvidence.provider_account_key
                        == fresh_settlement.provider_account_key,
                        SettlementEvidence.provider_entity_id
                        == fresh_settlement.provider_entity_id,
                        SettlementEvidence.provider_settlement_id.is_not_distinct_from(
                            fresh_settlement.provider_settlement_id
                        ),
                    )
                    .order_by(
                        SettlementEvidence.observed_at.desc(),
                        SettlementEvidence.settlement_evidence_id.desc(),
                    )
                    .limit(1)
                )
                if fresh_settlement
                else None
            )
            if (
                latest
                and fresh_settlement
                and latest.settlement_evidence_id != fresh_settlement.settlement_evidence_id
            ):
                verified = False
            if current_case.status == "RESOLVED":
                decision = "ALREADY_RESOLVED"
            elif current_case.reason_code in allowed and verified and unchanged and complete:
                current_case.status, current_case.resolved_at = "RESOLVED", utc_now()
                await session.flush()
                other = await session.scalar(
                    select(FinancialException.financial_exception_id)
                    .where(
                        FinancialException.payment_id == payment_id,
                        FinancialException.status == "OPEN",
                        FinancialException.reason_code.in_(PAYOUT_HOLD_REASONS),
                        or_(
                            FinancialException.captured_charge_id == charge_id,
                            FinancialException.refund_id.in_([r.refund_id for r in rows]),
                            FinancialException.captured_charge_id.is_(None)
                            & FinancialException.refund_id.is_(None),
                        ),
                    )
                    .limit(1)
                )
                obligation = await session.scalar(
                    select(RefundObligation).where(RefundObligation.captured_charge_id == charge_id)
                )
                if obligation and not other:
                    obligation.payout_blocked = False
                    payment = await session.get(Payment, payment_id)
                    assert payment is not None
                    for row in rows:
                        if row.status == "SUCCEEDED":
                            await resolve_uncertainty(session, payment, refund=row)
                decision = "VERIFIED_CASE_RESOLVED_HELD" if other else "VERIFIED_HOLD_RELEASED"
            else:
                decision = (
                    "OWNER_DECISION_REQUIRED"
                    if current_case.reason_code
                    in {
                        "PAYMENT_DISPUTE",
                        "SETTLEMENT_DISPUTE_REVIEW",
                        "SETTLEMENT_ADJUSTMENT_REVIEW",
                        "EXTERNAL_REFUND_UNMAPPED",
                    }
                    else "PROVIDER_BLOCKED"
                )
        if action == "RECOVER_ORIGINAL" and current_refund:
            obligation = (
                await session.get(RefundObligation, current_refund.refund_obligation_id)
                if current_refund.refund_obligation_id
                else None
            )
            held = await session.scalar(
                select(FinancialException.financial_exception_id)
                .where(
                    FinancialException.payment_id == payment_id,
                    FinancialException.status == "OPEN",
                    FinancialException.reason_code.in_(PAYOUT_HOLD_REASONS),
                )
                .limit(1)
            )
            window = getattr(provider, "refund_replay_window_seconds", None)
            first = current_refund.processing_started_at
            safe = (
                first is None
                and current_refund.status == "PENDING"
                or first is not None
                and window is not None
                and (utc_now() - first).total_seconds() + (getattr(provider, "_timeout", 0) or 0)
                < window
            )
            if current_refund.status in {"SUCCEEDED", "SUBMITTED", "FAILED"}:
                decision = "VERIFIED_OUTCOME" if verified else "PROVIDER_BLOCKED"
            elif safe and not held and not (obligation and obligation.payout_blocked):
                await append_outbox_event(
                    session,
                    event_key=f"refund-recovery:{command_id}",
                    aggregate_type="refund",
                    aggregate_id=current_refund.refund_id,
                    event_type="RefundRequested",
                    payload={"refund_id": str(current_refund.refund_id)},
                )
                decision = "ORIGINAL_RECOVERY_QUEUED"
            else:
                decision = "PROVIDER_BLOCKED"
        audit = FinancialAudit(
            command_id=command_id,
            actor_user_id=actor,
            payment_id=payment_id,
            refund_id=operation.refund_id if operation else None,
            evidence_id=fresh_event.payment_provider_event_id
            if fresh_event
            else evidence.payment_provider_event_id
            if evidence
            else None,
            settlement_evidence_id=fresh_settlement.settlement_evidence_id
            if fresh_settlement
            else settlement.settlement_evidence_id
            if settlement
            else None,
            financial_exception_id=case.financial_exception_id if case else None,
            action=action,
            result_code=decision,
        )
        session.add(audit)
        await session.flush([audit])
        await complete_idempotency_record(
            session,
            claim.record,
            result_resource_id=audit.financial_audit_id,
            result_status_code=200,
        )
        return audit
