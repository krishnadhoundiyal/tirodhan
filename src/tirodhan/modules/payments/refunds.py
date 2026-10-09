from datetime import datetime
from uuid import UUID

from sqlalchemy import false, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.customers.service import command_fingerprint
from tirodhan.modules.payments.accounting import compensate_charge, ensure_obligation, status_event
from tirodhan.modules.payments.models import (
    CapturedCharge,
    Payment,
    PaymentAttempt,
    Refund,
    RefundObligation,
)
from tirodhan.modules.payments.ports import (
    PaymentProviderNotConfiguredError,
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


class RefundInputError(RuntimeError):
    pass


class RefundNotFoundError(RuntimeError):
    pass


class RefundConflictError(RuntimeError):
    pass


def cancellation_refund_amount(payment: Payment, refunds: list[Refund]) -> int:
    """Full compensation or an already reserved full refund; no invented partial policy."""
    if payment.status not in {"PENDING", "CANCELLED", "EXPIRED", "SUCCEEDED"}:
        raise RefundConflictError("Payment accounting is inconsistent")
    if payment.status != "SUCCEEDED":
        if refunds:
            raise RefundConflictError("Refund exists without a captured payment")
        return 0
    if payment.successful_attempt_id is None or payment.succeeded_at is None:
        raise RefundConflictError("Canonical captured payment is missing")
    if any(
        r.currency != payment.currency
        or r.amount_minor <= 0
        or (r.captured_charge_id is None and r.payment_attempt_id != payment.successful_attempt_id)
        or r.status
        not in {"PENDING", "PROCESSING", "SUBMITTED", "SUCCEEDED", "FAILED", "INITIATION_UNCERTAIN"}
        for r in refunds
    ):
        raise RefundConflictError("Refund accounting is inconsistent")
    if any(r.captured_charge_id is not None for r in refunds):
        groups: dict[UUID | None, int] = {}
        for refund in refunds:
            if refund.non_payable_verified_at is None:
                groups[refund.captured_charge_id] = (
                    groups.get(refund.captured_charge_id, 0) + refund.amount_minor
                )
        if any(amount != payment.amount_minor for amount in groups.values()):
            raise RefundConflictError("Partial charge compensation requires financial review")
        return 0
    reserved = sum(r.amount_minor for r in refunds if r.non_payable_verified_at is None)
    if reserved == payment.amount_minor:
        return 0
    if reserved != 0 or payment.amount_minor <= 0:
        raise RefundConflictError("Partial compensation requires financial review")
    return payment.amount_minor


async def ensure_cancellation_refund(
    session: AsyncSession,
    payment: Payment,
    *,
    customer_id: UUID,
    idempotency_expires_at: datetime,
) -> Refund | None:
    # Caller owns Payment FOR UPDATE. All callers use one business key, independent
    # of cancellation command IDs and provider delivery IDs.
    charges = list(
        await session.scalars(
            select(CapturedCharge).where(CapturedCharge.payment_id == payment.payment_id)
        )
    )
    if charges:
        last = None
        for charge in charges:
            last = (
                await compensate_charge(
                    session,
                    payment,
                    charge,
                    "CUSTOMER_CANCELLATION",
                    expires_at=idempotency_expires_at,
                    actor=customer_id,
                )
                or last
            )
        return last
    refunds = list(
        await session.scalars(select(Refund).where(Refund.payment_id == payment.payment_id))
    )
    amount = cancellation_refund_amount(payment, refunds)
    if payment.status == "SUCCEEDED":
        attempt = await session.scalar(
            select(PaymentAttempt).where(
                PaymentAttempt.payment_attempt_id == payment.successful_attempt_id,
                PaymentAttempt.payment_id == payment.payment_id,
            )
        )
        if attempt is None or attempt.status != "SUCCEEDED" or not attempt.provider_payment_id:
            raise RefundConflictError("Canonical captured charge reference is missing")
    if not amount:
        return None
    assert payment.successful_attempt_id is not None
    return await create_refund(
        session,
        payment_id=payment.payment_id,
        payment_attempt_id=payment.successful_attempt_id,
        amount_minor=amount,
        reason_code="CUSTOMER_CANCELLATION",
        idempotency_key=f"customer-cancellation:{payment.request_id}",
        idempotency_expires_at=idempotency_expires_at,
        requested_by_user_id=customer_id,
    )


async def create_refund(
    session: AsyncSession,
    *,
    payment_id: UUID,
    payment_attempt_id: UUID,
    amount_minor: int,
    reason_code: str,
    # Must be one of the explicitly controlled reason codes
    idempotency_key: str,
    idempotency_expires_at: datetime,
    requested_by_user_id: UUID | None = None,
    captured_charge_id: UUID | None = None,
    refund_obligation_id: UUID | None = None,
    replacement: bool = False,
) -> Refund:
    valid_reasons = {
        "CUSTOMER_CANCELLATION",
        "ADDITIONAL_SUCCESS",
        "LATE_SUCCESS",
        "OPERATIONS_ADJUSTMENT",
    }
    if reason_code not in valid_reasons:
        raise RefundInputError(f"Invalid reason code: {reason_code}")

    if amount_minor <= 0:
        raise RefundInputError("Refund amount must be positive")

    # Financial lock precedes the refund command key as well as any attempt/FK
    # lock. Cancellation/capture may already hold this same Payment lock.
    payment = await session.scalar(
        select(Payment)
        .where(Payment.payment_id == payment_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if payment is None:
        raise RefundNotFoundError("Payment not found")

    scope = f"refund.create:{payment_id}"
    try:
        claim = await claim_idempotency_record(
            session,
            scope=scope,
            idempotency_key=idempotency_key,
            request_fingerprint=command_fingerprint(
                {
                    "payment_id": str(payment_id),
                    "payment_attempt_id": str(payment_attempt_id),
                    "amount_minor": amount_minor,
                    "reason_code": reason_code,
                    "captured_charge_id": captured_charge_id,
                    "refund_obligation_id": refund_obligation_id,
                    "requested_by_user_id": str(requested_by_user_id)
                    if requested_by_user_id
                    else None,
                }
            ),
            expires_at=idempotency_expires_at,
        )
    except IdempotencyKeyConflictError as e:
        raise IdempotencyKeyConflictError("Idempotency conflict for refund creation") from e

    if not claim.created:
        result = get_completed_idempotency_result(claim.record)
        if result is not None:
            # Replay: load Refund by result_resource_id from completed idempotency result
            stmt = select(Refund).where(Refund.refund_id == result.resource_id)
            refund = await session.scalar(stmt)
            if not refund:
                raise RefundNotFoundError("Refund not found")
            return refund

    if payment.status != "SUCCEEDED":
        raise RefundConflictError("Payment has not succeeded")

    if captured_charge_id is None and payment.successful_attempt_id != payment_attempt_id:
        raise RefundConflictError("Refund target must be the canonical successful attempt")

    # Validate payment attempt
    attempt_stmt = select(PaymentAttempt).where(
        PaymentAttempt.payment_attempt_id == payment_attempt_id
    )
    attempt = await session.scalar(attempt_stmt)
    if (
        not attempt
        or attempt.payment_id != payment.payment_id
        or attempt.status != "SUCCEEDED"
        or not attempt.provider_payment_id
    ):
        raise RefundConflictError("Invalid successful payment attempt")

    charge = await session.get(CapturedCharge, captured_charge_id) if captured_charge_id else None
    if captured_charge_id is None:
        charge = await session.scalar(
            select(CapturedCharge).where(
                CapturedCharge.payment_id == payment_id,
                CapturedCharge.payment_attempt_id == payment_attempt_id,
                CapturedCharge.provider_payment_id == attempt.provider_payment_id,
            )
        )
        if charge is not None:
            captured_charge_id = charge.captured_charge_id
            bound_obligation = await ensure_obligation(session, charge, reason_code, amount_minor)
            refund_obligation_id = bound_obligation.refund_obligation_id
    if captured_charge_id and (
        charge is None
        or charge.payment_id != payment_id
        or charge.payment_attempt_id != payment_attempt_id
        or charge.provider != attempt.provider
        or charge.currency != payment.currency
    ):
        raise RefundConflictError("Refund charge ownership is invalid")
    obligation = (
        await session.get(RefundObligation, refund_obligation_id) if refund_obligation_id else None
    )
    if charge and (
        obligation is None
        or obligation.captured_charge_id != charge.captured_charge_id
        or obligation.payout_blocked
        or obligation.amount_minor > charge.amount_minor
    ):
        raise RefundConflictError("Refund obligation is invalid or blocked")

    # Calculate reserved balance
    # Only independently verified non-payable failures release reservations.
    reserved_stmt = select(func.coalesce(func.sum(Refund.amount_minor), 0)).where(
        Refund.payment_id == payment_id,
        or_(
            Refund.captured_charge_id == captured_charge_id,
            (
                Refund.captured_charge_id.is_(None)
                & (Refund.payment_attempt_id == payment_attempt_id)
            )
            if charge and charge.provider_payment_id == attempt.provider_payment_id
            else false(),
        )
        if charge
        else Refund.captured_charge_id.is_(None),
        Refund.non_payable_verified_at.is_(None),
    )
    reserved_amount = await session.scalar(reserved_stmt) or 0

    if amount_minor > (charge.amount_minor if charge else payment.amount_minor) - reserved_amount:
        raise RefundConflictError("Requested refund exceeds remaining refundable balance")
    failed = await session.scalar(
        select(Refund.refund_id)
        .where(
            Refund.payment_id == payment_id,
            Refund.payment_attempt_id == payment_attempt_id,
            Refund.status == "FAILED",
            Refund.captured_charge_id == captured_charge_id
            if charge
            else Refund.captured_charge_id.is_(None),
        )
        .limit(1)
    )
    if failed and not replacement:
        raise RefundConflictError("Failed refunds require manager replacement approval")

    refund_id_var = new_uuid7()
    refund = Refund(
        refund_id=refund_id_var,
        payment_id=payment_id,
        payment_attempt_id=payment_attempt_id,
        amount_minor=amount_minor,
        currency=payment.currency,
        reason_code=reason_code,
        status="PENDING",
        provider=attempt.provider,
        provider_refund_id=None,
        provider_idempotency_key=f"refund:{refund_id_var}",
        requested_by_user_id=requested_by_user_id,
        created_at=utc_now(),
        captured_charge_id=captured_charge_id,
        refund_obligation_id=refund_obligation_id,
    )
    session.add(refund)
    await session.flush([refund])

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
    await status_event(session, payment, f"refund-initiated:{refund.refund_id}", "REFUND_INITIATED")

    return refund


async def execute_refund_provider_call(
    session_factory: async_sessionmaker[AsyncSession],
    refund_id: UUID,
    provider: RefundProvider,
) -> None:
    # 1. Short DB transaction to claim and mark PROCESSING
    async with session_factory() as session:
        async with session.begin():
            identity = await session.get(Refund, refund_id)
            if identity is None:
                return
            await session.scalar(
                select(Payment).where(Payment.payment_id == identity.payment_id).with_for_update()
            )
            stmt = (
                select(Refund)
                .where(Refund.refund_id == refund_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            refund = await session.scalar(stmt)
            if not refund:
                return  # Not found
            if refund.status not in {"PENDING", "PROCESSING", "INITIATION_UNCERTAIN"}:
                return  # Provider initiation already durably established.

            # Need the successful attempt's provider_payment_id
            if provider.provider_code != refund.provider:
                raise PaymentProviderNotConfiguredError("Refund provider does not match intent")

            attempt_stmt = select(PaymentAttempt).where(
                PaymentAttempt.payment_attempt_id == refund.payment_attempt_id
            )
            attempt = await session.scalar(attempt_stmt)
            if (
                not attempt
                or attempt.payment_id != refund.payment_id
                or not attempt.provider_payment_id
            ):
                raise RefundConflictError("Refund canonical provider reference is missing")

            charge = (
                await session.get(CapturedCharge, refund.captured_charge_id)
                if refund.captured_charge_id
                else None
            )
            obligation = (
                await session.get(RefundObligation, refund.refund_obligation_id)
                if refund.refund_obligation_id
                else None
            )
            if obligation and obligation.payout_blocked:
                raise RefundConflictError("Refund obligation is blocked")
            if (
                charge
                and charge.provider_account_key
                and getattr(provider, "account_key", None) != charge.provider_account_key
            ):
                raise PaymentProviderNotConfiguredError(
                    "Refund account does not match captured charge"
                )
            provider_payment_id = (
                charge.provider_payment_id if charge else attempt.provider_payment_id
            )
            amount_minor = refund.amount_minor
            currency = refund.currency
            provider_idempotency_key = refund.provider_idempotency_key

            refund.status = "PROCESSING"
            refund.processing_started_at = refund.processing_started_at or utc_now()

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

    if result is not None:
        # Provider execution, webhook and API inquiry share the same outcome processor.
        from tirodhan.modules.payments.ports import AuthenticatedPaymentEvent, PaymentEventOutcome
        from tirodhan.modules.payments.service import process_authenticated_payment_event

        if result.outcome != RefundInitiationOutcome.INITIATION_UNCERTAIN:
            await process_authenticated_payment_event(
                session_factory,
                AuthenticatedPaymentEvent(
                    provider=provider.provider_code,
                    external_event_id=f"execution:{new_uuid7()}",
                    event_type="refund.execution",
                    outcome=PaymentEventOutcome(result.outcome.value),
                    payment_attempt_id=None,
                    refund_id=refund_id,
                    provider_refund_id=result.provider_refund_id,
                    provider_payment_id=provider_payment_id,
                    amount_minor=amount_minor,
                    currency=currency,
                    evidence_source="PROVIDER_RESPONSE",
                    provider_account_key=getattr(provider, "account_key", None),
                ),
                payload_hash=b"",
                planning_lead_time_minutes=None,
            )
            return
    # 3. New DB transaction to persist uncertainty only.
    async with session_factory() as session:
        async with session.begin():
            identity = await session.get(Refund, refund_id)
            if identity is None:
                return
            await session.scalar(
                select(Payment).where(Payment.payment_id == identity.payment_id).with_for_update()
            )
            stmt = (
                select(Refund)
                .where(Refund.refund_id == refund_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            refund = await session.scalar(stmt)
            if not refund or refund.status not in {"PROCESSING", "INITIATION_UNCERTAIN"}:
                return

            # Prevent contradiction
            if refund.provider != provider.provider_code:
                return

            # Update provider_refund_id ONLY if it doesn't overwrite an existing different one
            if result is not None and result.provider_refund_id:
                if not refund.provider_refund_id:
                    refund.provider_refund_id = result.provider_refund_id
                elif refund.provider_refund_id != result.provider_refund_id:
                    # Mismatch! External call has occurred and local/provider truth conflicts.
                    refund.status = "INITIATION_UNCERTAIN"
                    return

            refund.status = "INITIATION_UNCERTAIN"
