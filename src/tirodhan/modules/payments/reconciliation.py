"""Bounded read-only provider reconciliation and audited manager recovery."""

import hashlib
import random
from dataclasses import dataclass
from datetime import timedelta
from typing import TypedDict, cast
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.customers.service import command_fingerprint
from tirodhan.modules.identity.models import AppUser, UserRole
from tirodhan.modules.payments.accounting import ensure_obligation, open_exception, status_event
from tirodhan.modules.payments.models import (
    CapturedCharge,
    FinancialAudit,
    FinancialException,
    Payment,
    PaymentAttempt,
    PaymentProviderEvent,
    Refund,
    RefundObligation,
)
from tirodhan.modules.payments.ports import FinancialInquiryProvider, PaymentProviderUncertainError
from tirodhan.modules.payments.refunds import RefundConflictError, create_refund
from tirodhan.modules.payments.service import process_authenticated_payment_event
from tirodhan.modules.reliability.primitives import (
    claim_idempotency_record,
    complete_idempotency_record,
    get_completed_idempotency_result,
)


class PaymentInquiryArguments(TypedDict):
    attempt_id: UUID
    order_id: str | None
    amount_minor: int
    currency: str
    reported_payment_id: str | None


class RefundInquiryArguments(TypedDict):
    refund_id: UUID
    provider_refund_id: str | None
    provider_payment_id: str
    amount_minor: int
    currency: str


@dataclass(frozen=True)
class ReconciliationPolicy:
    batch_size: int
    interval_seconds: int
    max_backoff_seconds: int
    lease_seconds: int
    unresolved_threshold_seconds: int
    command_ttl_seconds: int
    planning_lead_time_minutes: int

    def __post_init__(self) -> None:
        if (
            min(
                self.batch_size,
                self.interval_seconds,
                self.max_backoff_seconds,
                self.lease_seconds,
                self.unresolved_threshold_seconds,
                self.command_ttl_seconds,
                self.planning_lead_time_minutes,
            )
            <= 0
            or self.batch_size > 1000
        ):
            raise ValueError("Financial reconciliation configuration is invalid")
        if self.max_backoff_seconds < self.interval_seconds:
            raise ValueError("Maximum reconciliation backoff must cover the initial interval")


async def inquire_target(
    factory: async_sessionmaker[AsyncSession],
    provider: FinancialInquiryProvider,
    target_id: UUID,
    *,
    refund: bool,
    policy: ReconciliationPolicy,
    reported_reference: str | None = None,
) -> list[PaymentProviderEvent]:
    async with factory() as session:
        if refund:
            operation = await session.get(Refund, target_id)
            if operation is None:
                raise RefundConflictError("Refund target is missing")
            attempt = await session.get(PaymentAttempt, operation.payment_attempt_id)
            charge = (
                await session.get(CapturedCharge, operation.captured_charge_id)
                if operation.captured_charge_id
                else None
            )
            if attempt is None:
                raise RefundConflictError("Refund attempt is missing")
            reference = charge.provider_payment_id if charge else attempt.provider_payment_id
            account_key = charge.provider_account_key if charge else attempt.provider_account_key
            if reference is None:
                raise RefundConflictError("Refund charge reference is missing")
            provider_code = operation.provider
            refund_arguments: RefundInquiryArguments = dict(
                refund_id=operation.refund_id,
                provider_refund_id=operation.provider_refund_id,
                provider_payment_id=reference,
                amount_minor=operation.amount_minor,
                currency=operation.currency,
            )
        else:
            attempt = await session.get(PaymentAttempt, target_id)
            if attempt is None:
                raise RefundConflictError("Payment target is missing")
            payment = await session.get(Payment, attempt.payment_id)
            assert payment is not None
            provider_code = attempt.provider
            account_key = attempt.provider_account_key
            payment_arguments: PaymentInquiryArguments = dict(
                attempt_id=target_id,
                order_id=attempt.provider_order_id,
                amount_minor=payment.amount_minor,
                currency=payment.currency,
                reported_payment_id=reported_reference,
            )
    if provider_code != provider.provider_code or (
        account_key and account_key != provider.account_key
    ):
        raise RefundConflictError("Provider account does not match financial target")
    observations = (
        await provider.inquire_refund(**refund_arguments)
        if refund
        else await provider.inquire_payment(**payment_arguments)
    )
    records = []
    for observation in observations:
        if (
            observation.evidence_source != "API_INQUIRY"
            or observation.provider_account_key != provider.account_key
        ):
            raise RefundConflictError("Inquiry evidence provenance is invalid")
        records.append(
            await process_authenticated_payment_event(
                factory,
                observation,
                payload_hash=hashlib.sha256(observation.external_event_id.encode()).digest(),
                planning_lead_time_minutes=policy.planning_lead_time_minutes,
                idempotency_expires_at=utc_now() + timedelta(seconds=policy.command_ttl_seconds),
            )
        )
    return records


async def _release_claim(
    factory: async_sessionmaker[AsyncSession],
    target: UUID,
    is_refund: bool,
    token: UUID,
    policy: ReconciliationPolicy,
) -> None:
    async with factory() as session, session.begin():
        model = Refund if is_refund else PaymentAttempt
        identity = cast(PaymentAttempt | Refund | None, await session.get(model, target))
        if identity is None:
            return
        payment = await session.scalar(
            select(Payment).where(Payment.payment_id == identity.payment_id).with_for_update()
        )
        current = cast(
            PaymentAttempt | Refund | None,
            await session.scalar(
                select(model)
                .where(
                    Refund.refund_id == target
                    if is_refund
                    else PaymentAttempt.payment_attempt_id == target
                )
                .execution_options(populate_existing=True)
                .with_for_update()
            ),
        )
        if current is None or current.claim_token != token:
            return
        current.check_count += 1
        current.claim_token = None
        current.claim_until = None
        terminal = current.status == "SUCCEEDED" or (not is_refund and current.status == "FAILED")
        delay = min(
            policy.max_backoff_seconds,
            policy.interval_seconds * 2 ** min(current.check_count, 20),
        )
        current.next_check_at = (
            None
            if terminal
            else utc_now()
            + timedelta(seconds=min(policy.max_backoff_seconds, delay * random.uniform(0.8, 1.2)))
        )
        if (
            not terminal
            and (utc_now() - current.created_at).total_seconds()
            >= policy.unresolved_threshold_seconds
        ):
            assert payment is not None
            await open_exception(
                session,
                payment,
                "REFUND_UNRESOLVED" if is_refund else "PAYMENT_UNRESOLVED",
                refund=current if isinstance(current, Refund) else None,
            )
            await status_event(session, payment, f"unresolved:{target}", "FINANCIAL_UNRESOLVED")


async def reconcile_batch(
    factory: async_sessionmaker[AsyncSession],
    provider: FinancialInquiryProvider,
    policy: ReconciliationPolicy,
) -> int:
    """Oldest-due fairness; claim each target immediately before its GET.

    Leases distribute work. Financial event uniqueness/Payment locks arbitrate truth.
    Supersession is an expected scheduling outcome, not a financial failure.
    """
    processed = 0
    for _ in range(policy.batch_size):
        now = utc_now()
        async with factory() as session, session.begin():
            candidates: list[PaymentAttempt | Refund] = []
            for model in (Refund, PaymentAttempt):
                candidate = await session.scalar(
                    select(model)
                    .where(
                        model.provider == provider.provider_code,
                        model.next_check_at <= now,
                        or_(model.claim_until.is_(None), model.claim_until <= now),
                    )
                    .order_by(model.next_check_at, model.created_at)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
                if candidate is not None:
                    candidates.append(cast(PaymentAttempt | Refund, candidate))
            if not candidates:
                break
            row = min(candidates, key=lambda r: (r.next_check_at or now, r.created_at))
            is_refund = isinstance(row, Refund)
            target = row.refund_id if isinstance(row, Refund) else row.payment_attempt_id
            token = new_uuid7()
            row.claim_token = token
            row.claim_until = now + timedelta(seconds=policy.lease_seconds)
            row.next_check_at = row.claim_until
        try:
            await inquire_target(factory, provider, target, refund=is_refund, policy=policy)
        except (PaymentProviderUncertainError, RefundConflictError):
            pass  # Unavailable/missing/mismatched provider facts never become failure.
        finally:
            await _release_claim(factory, target, is_refund, token, policy)
        processed += 1
    return processed


async def _manager_lock(session: AsyncSession, actor: UUID) -> None:
    user = await session.scalar(select(AppUser).where(AppUser.user_id == actor).with_for_update())
    role = await session.scalar(
        select(UserRole)
        .where(
            UserRole.user_id == actor,
            UserRole.role_code == "MANAGER",
            UserRole.revoked_at.is_(None),
        )
        .with_for_update()
    )
    if user is None or user.status != "ACTIVE" or role is None:
        raise RefundConflictError("Active manager authorization is required")


async def manager_inquiry(
    factory: async_sessionmaker[AsyncSession],
    provider: FinancialInquiryProvider,
    *,
    command_id: UUID,
    actor: UUID,
    target_id: UUID,
    refund: bool,
    policy: ReconciliationPolicy,
    reported_reference: str | None = None,
) -> FinancialAudit:
    expires = utc_now() + timedelta(seconds=policy.command_ttl_seconds)
    fingerprint = command_fingerprint(
        dict(actor=actor, target=target_id, refund=refund, reference=reported_reference)
    )
    async with factory() as session, session.begin():
        target = cast(
            Refund | PaymentAttempt | None,
            await session.get(Refund if refund else PaymentAttempt, target_id),
        )
        if target is None:
            raise RefundConflictError("Financial target is missing")
        payment_id = target.payment_id
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
        if not claim.created:
            result = get_completed_idempotency_result(claim.record)
            if result and result.resource_id:
                audit = await session.get(FinancialAudit, result.resource_id)
                assert audit is not None
                return audit
    try:
        records = await inquire_target(
            factory,
            provider,
            target_id,
            refund=refund,
            policy=policy,
            reported_reference=reported_reference,
        )
        decision = records[-1].processing_status if records else "UNRESOLVED"
        evidence_id = records[-1].payment_provider_event_id if records else None
    except (PaymentProviderUncertainError, RefundConflictError):
        decision, evidence_id = "UNRESOLVED", None
    async with factory() as session, session.begin():
        payment = await session.scalar(
            select(Payment).where(Payment.payment_id == payment_id).with_for_update()
        )
        assert payment is not None
        claim = await claim_idempotency_record(
            session,
            scope="financial.manager",
            idempotency_key=str(command_id),
            request_fingerprint=fingerprint,
            expires_at=expires,
        )
        result = get_completed_idempotency_result(claim.record)
        if result and result.resource_id:
            audit = await session.get(FinancialAudit, result.resource_id)
            assert audit is not None
            return audit
        audit = FinancialAudit(
            command_id=command_id,
            actor_user_id=actor,
            payment_id=payment_id,
            refund_id=target_id if refund else None,
            evidence_id=evidence_id,
            submitted_reference=reported_reference,
            action="REFUND_INQUIRY" if refund else "PAYMENT_INQUIRY",
            result_code=decision,
        )
        session.add(audit)
        await session.flush([audit])
        if not refund:
            request = await session.get(CollectionRequest, payment.request_id)
            if request and request.status == "CANCELLED":
                charges = list(
                    await session.scalars(
                        select(CapturedCharge).where(CapturedCharge.payment_id == payment_id)
                    )
                )
                for charge in charges:
                    existing = await session.scalar(
                        select(Refund.refund_id)
                        .where(
                            Refund.payment_id == payment_id,
                            or_(
                                Refund.captured_charge_id == charge.captured_charge_id,
                                Refund.payment_attempt_id == charge.payment_attempt_id,
                            ),
                        )
                        .limit(1)
                    )
                    if existing is None:
                        await open_exception(
                            session,
                            payment,
                            "HISTORICAL_CANCELLED_MISSING_REFUND",
                            charge=charge,
                            evidence_id=evidence_id,
                        )
        if decision == "UNRESOLVED":
            operation = await session.get(Refund, target_id) if refund else None
            await open_exception(
                session,
                payment,
                "REFUND_UNRESOLVED" if refund else "PAYMENT_UNRESOLVED",
                refund=operation,
                evidence_id=evidence_id,
            )
        await complete_idempotency_record(
            session,
            claim.record,
            result_resource_id=audit.financial_audit_id,
            result_status_code=200,
        )
        return audit


async def _approve_refund(
    factory: async_sessionmaker[AsyncSession],
    provider: FinancialInquiryProvider,
    *,
    command_id: UUID,
    actor: UUID,
    charge_id: UUID,
    policy: ReconciliationPolicy,
    failed_refund_id: UUID | None = None,
) -> FinancialAudit:
    # Refresh original outcome before replacement; never authorize from a stale screenshot/status.
    expires = utc_now() + timedelta(seconds=policy.command_ttl_seconds)
    fingerprint = command_fingerprint(dict(actor=actor, charge=charge_id, failed=failed_refund_id))
    async with factory() as session, session.begin():
        charge = await session.get(CapturedCharge, charge_id)
        if charge is None:
            raise RefundConflictError("Captured charge is missing")
        await session.scalar(
            select(Payment).where(Payment.payment_id == charge.payment_id).with_for_update()
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
        if result and result.resource_id:
            audit = await session.get(FinancialAudit, result.resource_id)
            assert audit is not None
            return audit
    if failed_refund_id:
        fresh_records = await inquire_target(
            factory, provider, failed_refund_id, refund=True, policy=policy
        )
        if not fresh_records or not fresh_records[-1].definitive_non_payable:
            raise RefundConflictError("Current inquiry does not verify non-payable failure")
    else:
        async with factory() as session:
            charge = await session.get(CapturedCharge, charge_id)
            if charge is None:
                raise RefundConflictError("Captured charge is missing")
            attempt_id = charge.payment_attempt_id
        await inquire_target(factory, provider, attempt_id, refund=False, policy=policy)
    async with factory() as session:
        charge = await session.get(CapturedCharge, charge_id)
        if (
            charge is None
            or charge.provider != provider.provider_code
            or (charge.provider_account_key and charge.provider_account_key != provider.account_key)
        ):
            raise RefundConflictError("Captured charge account is invalid")
        payment_id = charge.payment_id
        reference = charge.provider_payment_id
    # Existing external refunds must be completely understood. Historical full recovery is
    # deliberately refused when any provider operation exists but is not locally mapped.
    external_operations = await provider.inspect_charge_refunds(reference)
    expires = utc_now() + timedelta(seconds=policy.command_ttl_seconds)
    async with factory() as session, session.begin():
        payment = await session.scalar(
            select(Payment).where(Payment.payment_id == payment_id).with_for_update()
        )
        assert payment is not None
        await _manager_lock(session, actor)
        claim = await claim_idempotency_record(
            session,
            scope="financial.manager",
            idempotency_key=str(command_id),
            request_fingerprint=command_fingerprint(
                dict(actor=actor, charge=charge_id, failed=failed_refund_id)
            ),
            expires_at=expires,
        )
        if not claim.created:
            result = get_completed_idempotency_result(claim.record)
            if result and result.resource_id:
                audit = await session.get(FinancialAudit, result.resource_id)
                assert audit is not None
                return audit
        charge = await session.get(CapturedCharge, charge_id)
        assert charge is not None
        operations = list(
            await session.scalars(
                select(Refund)
                .where(
                    Refund.payment_id == payment_id,
                    or_(
                        Refund.captured_charge_id == charge_id, Refund.captured_charge_id.is_(None)
                    ),
                )
                .order_by(Refund.refund_id)
            )
        )
        if any(r.captured_charge_id is None for r in operations):
            raise RefundConflictError("Legacy refund accounting requires verified mapping")
        if failed_refund_id:
            failed = next((r for r in operations if r.refund_id == failed_refund_id), None)
            if (
                failed is None
                or failed.status != "FAILED"
                or failed.non_payable_verified_at is None
                or not failed.failure_evidence_id
            ):
                raise RefundConflictError("Original refund is not verified as non-payable")
            evidence = await session.get(PaymentProviderEvent, failed.failure_evidence_id)
            if (
                evidence is None
                or evidence.evidence_source != "API_INQUIRY"
                or not evidence.definitive_non_payable
                or evidence.provider_account_key != provider.account_key
            ):
                raise RefundConflictError("Definitive failure proof is missing")
            if external_operations != {
                r.provider_refund_id: (
                    r.amount_minor,
                    r.currency,
                    {"SUCCEEDED": "processed", "FAILED": "failed"}.get(r.status, "pending"),
                )
                for r in operations
            }:
                raise RefundConflictError("External refund inventory is not completely mapped")
            obligation = await session.get(RefundObligation, failed.refund_obligation_id)
            if obligation is None or obligation.payout_blocked:
                raise RefundConflictError("Refund obligation is blocked or missing")
            reason = failed.reason_code
        else:
            if operations or external_operations:
                raise RefundConflictError(
                    "Historical compensation requires an empty verified refund inventory"
                )
            request = await session.get(CollectionRequest, payment.request_id)
            cases = list(
                await session.scalars(
                    select(FinancialException).where(
                        FinancialException.payment_id == payment_id,
                        FinancialException.captured_charge_id == charge_id,
                        FinancialException.status == "OPEN",
                    )
                )
            )
            if (
                request is None
                or request.status not in {"EXPIRED", "CANCELLED", "PENDING_PAYMENT"}
                or not cases
            ):
                raise RefundConflictError("No eligible historical/late financial exception")
            reason = "CUSTOMER_CANCELLATION" if request.status == "CANCELLED" else "LATE_SUCCESS"
            obligation = await ensure_obligation(session, charge, reason)
        reserved = sum(r.amount_minor for r in operations if r.non_payable_verified_at is None)
        paid = sum(r.amount_minor for r in operations if r.status == "SUCCEEDED")
        amount = obligation.amount_minor - paid
        if amount <= 0 or reserved != paid or amount > charge.amount_minor - reserved:
            raise RefundConflictError(
                "Refund has a payable operation or inconsistent outstanding balance"
            )
        operation = await create_refund(
            session,
            payment_id=payment_id,
            payment_attempt_id=charge.payment_attempt_id,
            captured_charge_id=charge_id,
            refund_obligation_id=obligation.refund_obligation_id,
            amount_minor=amount,
            reason_code=reason,
            idempotency_key=f"manager-approval:{command_id}",
            idempotency_expires_at=expires,
            requested_by_user_id=actor,
            replacement=bool(failed_refund_id),
        )
        audit = FinancialAudit(
            command_id=command_id,
            actor_user_id=actor,
            payment_id=payment_id,
            refund_id=operation.refund_id,
            evidence_id=failed.failure_evidence_id
            if failed_refund_id and failed is not None
            else charge.evidence_id,
            action="REFUND_REPLACEMENT" if failed_refund_id else "HISTORICAL_REFUND",
            result_code="REFUND_AUTHORIZED",
        )
        session.add(audit)
        await session.flush([audit])
        cases = list(
            await session.scalars(
                select(FinancialException).where(
                    FinancialException.payment_id == payment_id,
                    FinancialException.captured_charge_id == charge_id,
                    FinancialException.status == "OPEN",
                )
            )
        )
        for case in cases:
            if case.reason_code == "REFUND_FINALITY_CONTRADICTION":
                raise RefundConflictError("Contradictory refund outcomes block payout")
            case.status = "RESOLVED"
            case.resolved_at = utc_now()
        await status_event(
            session, payment, f"manager-resolved:{command_id}", "MANAGER_EXCEPTION_RESOLVED"
        )
        await complete_idempotency_record(
            session,
            claim.record,
            result_resource_id=audit.financial_audit_id,
            result_status_code=201,
        )
        return audit


async def approve_refund(
    factory: async_sessionmaker[AsyncSession],
    provider: FinancialInquiryProvider,
    *,
    command_id: UUID,
    actor: UUID,
    charge_id: UUID,
    policy: ReconciliationPolicy,
    failed_refund_id: UUID | None = None,
) -> FinancialAudit:
    """Persist manager refusal as well as authorization; exact rejected replay stays rejected."""
    try:
        audit = await _approve_refund(
            factory,
            provider,
            command_id=command_id,
            actor=actor,
            charge_id=charge_id,
            policy=policy,
            failed_refund_id=failed_refund_id,
        )
        if audit.result_code == "REFUND_APPROVAL_REFUSED":
            raise RefundConflictError("Refund approval was refused")
        return audit
    except RefundConflictError:
        async with factory() as session, session.begin():
            charge = await session.get(CapturedCharge, charge_id)
            if charge is None:
                raise
            await session.scalar(
                select(Payment).where(Payment.payment_id == charge.payment_id).with_for_update()
            )
            await _manager_lock(session, actor)
            claim = await claim_idempotency_record(
                session,
                scope="financial.manager",
                idempotency_key=str(command_id),
                request_fingerprint=command_fingerprint(
                    dict(actor=actor, charge=charge_id, failed=failed_refund_id)
                ),
                expires_at=utc_now() + timedelta(seconds=policy.command_ttl_seconds),
            )
            if get_completed_idempotency_result(claim.record) is None:
                audit = FinancialAudit(
                    command_id=command_id,
                    actor_user_id=actor,
                    payment_id=charge.payment_id,
                    refund_id=failed_refund_id,
                    action="REFUND_REPLACEMENT" if failed_refund_id else "HISTORICAL_REFUND",
                    result_code="REFUND_APPROVAL_REFUSED",
                )
                session.add(audit)
                await session.flush([audit])
                await complete_idempotency_record(
                    session,
                    claim.record,
                    result_resource_id=audit.financial_audit_id,
                    result_status_code=409,
                )
        raise
