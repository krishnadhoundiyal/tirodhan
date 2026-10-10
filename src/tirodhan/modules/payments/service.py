from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, false, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.collection_requests.service import (
    PAYMENT_PENDING,
    REQUEST_ACCEPTED,
    REQUEST_PENDING_PAYMENT,
)
from tirodhan.modules.customers.service import (
    IdempotencyCommandInProgressError,
    command_fingerprint,
)
from tirodhan.modules.payments.accounting import (
    compensate_charge,
    open_exception,
    record_charge,
    resolve_uncertainty,
    status_event,
)
from tirodhan.modules.payments.models import (
    CapturedCharge,
    Payment,
    PaymentAttempt,
    PaymentProviderEvent,
    Refund,
    RefundObligation,
)
from tirodhan.modules.payments.ports import (
    AuthenticatedPaymentEvent,
    PaymentEventOutcome,
    PaymentInitiationOutcome,
    PaymentProvider,
    PaymentProviderUncertainError,
)
from tirodhan.modules.payments.refunds import RefundConflictError, ensure_cancellation_refund
from tirodhan.modules.planning.locking import acquire_work_unit_advisory_lock
from tirodhan.modules.planning.models import PlanningBatch
from tirodhan.modules.planning.policy import planning_cutoff_reached
from tirodhan.modules.reliability.models import InboxMessage
from tirodhan.modules.reliability.primitives import (
    append_outbox_event,
    claim_idempotency_record,
    complete_idempotency_record,
    get_completed_idempotency_result,
)

PAYMENT_SUCCEEDED = "SUCCEEDED"
ATTEMPT_CREATED = "CREATED"
ATTEMPT_PENDING = "PENDING"
ATTEMPT_FAILED = "FAILED"
ATTEMPT_UNCERTAIN = "INITIATION_UNCERTAIN"
ATTEMPT_SUCCEEDED = "SUCCEEDED"
EVENT_RECEIVED = "RECEIVED"
EVENT_PROCESSED = "PROCESSED"
EVENT_RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
EVENT_UNMATCHED = "UNMATCHED"
EVENT_RECONCILIATION = "RECONCILIATION_REQUIRED"


class PaymentNotEligibleError(RuntimeError):
    pass


class PaymentAttemptNotFoundError(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class InitiatePaymentAttemptCommand:
    customer_id: UUID
    request_id: UUID
    idempotency_key: str


async def _load_attempt(session: AsyncSession, attempt_id: UUID) -> PaymentAttempt:
    attempt = await session.get(PaymentAttempt, attempt_id)
    if attempt is None:
        raise PaymentAttemptNotFoundError("payment attempt not found")
    return attempt


async def initiate_payment_attempt(
    session_factory: async_sessionmaker[AsyncSession],
    command: InitiatePaymentAttemptCommand,
    provider: PaymentProvider,
    *,
    idempotency_expires_at: datetime,
) -> PaymentAttempt:
    provider_code = provider.provider_code
    fingerprint = command_fingerprint(
        {
            "customer_id": command.customer_id,
            "request_id": command.request_id,
            "provider": provider_code,
        }
    )

    # Transaction 1: establish the durable attempt and its stable provider key.
    async with session_factory() as session, session.begin():
        claim = await claim_idempotency_record(
            session,
            scope=f"payment-attempt.create:{command.request_id}",
            idempotency_key=command.idempotency_key,
            request_fingerprint=fingerprint,
            expires_at=idempotency_expires_at,
        )
        if not claim.created:
            replay = get_completed_idempotency_result(claim.record)
            if replay is None or replay.resource_id is None:
                raise IdempotencyCommandInProgressError(
                    "payment attempt creation is already in progress"
                )
            attempt = await _load_attempt(session, replay.resource_id)
        else:
            row = (
                await session.execute(
                    select(Payment, CollectionRequest)
                    .join(CollectionRequest, CollectionRequest.request_id == Payment.request_id)
                    .where(
                        CollectionRequest.request_id == command.request_id,
                        CollectionRequest.customer_id == command.customer_id,
                    )
                    .with_for_update(of=Payment)
                )
            ).one_or_none()
            if row is None:
                raise PaymentNotEligibleError("owned collection request payment not found")
            payment, request = row
            now = utc_now()
            if (
                payment.status != PAYMENT_PENDING
                or request.status != REQUEST_PENDING_PAYMENT
                or request.payment_expires_at <= now
            ):
                raise PaymentNotEligibleError("payment is not eligible for another attempt")
            unsettled = await session.scalar(
                select(PaymentAttempt.payment_attempt_id)
                .where(
                    PaymentAttempt.payment_id == payment.payment_id,
                    PaymentAttempt.status != ATTEMPT_FAILED,
                )
                .limit(1)
            )
            reconciliation = await session.scalar(
                select(PaymentProviderEvent.payment_provider_event_id)
                .join(
                    PaymentAttempt,
                    PaymentAttempt.payment_attempt_id == PaymentProviderEvent.payment_attempt_id,
                )
                .where(
                    PaymentAttempt.payment_id == payment.payment_id,
                    PaymentProviderEvent.processing_status == EVENT_RECONCILIATION,
                )
                .limit(1)
            )
            if unsettled is not None or reconciliation is not None:
                raise PaymentNotEligibleError(
                    "Payment is already processing or requires reconciliation"
                )
            attempt_id = new_uuid7()
            attempt = PaymentAttempt(
                payment_attempt_id=attempt_id,
                payment_id=payment.payment_id,
                provider=provider_code,
                provider_idempotency_key=f"payment-attempt:{attempt_id}",
                status=ATTEMPT_CREATED,
                provider_account_key=getattr(provider, "account_key", None),
                created_at=now,
            )
            session.add(attempt)
            await session.flush([attempt])
            await complete_idempotency_record(
                session,
                claim.record,
                result_resource_id=attempt.payment_attempt_id,
                result_status_code=201,
            )

        payment_id = attempt.payment_id
        attempt_id = attempt.payment_attempt_id
        stable_key = attempt.provider_idempotency_key
        attempt_status = attempt.status

    if attempt_status not in {ATTEMPT_CREATED, ATTEMPT_UNCERTAIN}:
        return attempt

    async with session_factory() as session:
        payment = await session.get(Payment, payment_id)
        if payment is None:
            raise RuntimeError("payment attempt references a missing payment")
        amount_minor = payment.amount_minor
        currency = payment.currency

    # No database session or transaction exists while the provider is called.
    try:
        result = await provider.initiate_payment(
            payment_attempt_id=attempt_id,
            amount_minor=amount_minor,
            currency=currency,
            provider_idempotency_key=stable_key,
        )
    except PaymentProviderUncertainError as error:
        async with session_factory() as session, session.begin():
            durable: PaymentAttempt | None = await session.scalar(
                select(PaymentAttempt)
                .where(PaymentAttempt.payment_attempt_id == attempt_id)
                .with_for_update()
            )
            if durable is None:
                raise RuntimeError("durable payment attempt disappeared") from error
            if durable.status in {ATTEMPT_CREATED, ATTEMPT_UNCERTAIN}:
                durable.status = ATTEMPT_UNCERTAIN
                durable.failure_code = error.failure_code
            return durable

    async with session_factory() as session, session.begin():
        durable = await session.scalar(
            select(PaymentAttempt)
            .where(PaymentAttempt.payment_attempt_id == attempt_id)
            .with_for_update()
        )
        if durable is None:
            raise RuntimeError("durable payment attempt disappeared")
        if durable.status not in {ATTEMPT_CREATED, ATTEMPT_UNCERTAIN}:
            return durable
        durable.provider_order_id = result.provider_order_id
        durable.provider_payment_id = result.provider_payment_id
        if result.outcome == PaymentInitiationOutcome.READY:
            if result.provider_order_id is None:
                raise ValueError("ready provider result requires a provider order ID")
            durable.status = ATTEMPT_PENDING
            durable.failure_code = None
        elif result.outcome == PaymentInitiationOutcome.FAILED:
            if not result.failure_code:
                raise ValueError("failed provider result requires a failure code")
            durable.status = ATTEMPT_FAILED
            durable.failure_code = result.failure_code
            durable.completed_at = utc_now()
        else:
            raise ValueError("unknown payment initiation outcome")
        return durable


async def process_authenticated_payment_event(
    session_factory: async_sessionmaker[AsyncSession],
    event: AuthenticatedPaymentEvent,
    *,
    payload_hash: bytes,
    planning_lead_time_minutes: int | None,
    idempotency_expires_at: datetime | None = None,
    inbox_message_id: str | None = None,
) -> PaymentProviderEvent:
    now = utc_now()
    async with session_factory() as session, session.begin():
        if inbox_message_id is not None:
            # Completion is visible only with the financial commit. Any exception rolls
            # back both. Business event uniqueness remains the economic replay boundary.
            await session.execute(
                insert(InboxMessage)
                .values(
                    consumer_name="financial-webhooks",
                    message_id=inbox_message_id,
                    message_type="FinancialWebhookAuthenticated",
                    business_key=event.external_event_id,
                    status="PROCESSED",
                    first_received_at=now,
                    processed_at=now,
                )
                .on_conflict_do_nothing(index_elements=["consumer_name", "message_id"])
            )
        event_id = new_uuid7()
        inserted_id = await session.scalar(
            insert(PaymentProviderEvent)
            .values(
                payment_provider_event_id=event_id,
                provider=event.provider,
                external_event_id=event.external_event_id,
                event_type=event.event_type,
                payment_attempt_id=None,
                refund_id=None,
                payload_hash=payload_hash,
                processing_status=EVENT_RECEIVED,
                received_at=now,
                evidence_source=event.evidence_source,
                provider_account_key=event.provider_account_key,
                definitive_non_payable=event.definitive_non_payable,
                observed_outcome=event.outcome.value,
                provider_payment_id=event.provider_payment_id,
                amount_minor=event.amount_minor
                if event.amount_minor and event.amount_minor > 0
                else None,
                currency=event.currency,
                provider_order_id=event.provider_order_id,
                provider_refund_id=event.provider_refund_id,
                provider_dispute_id=event.provider_dispute_id,
                dispute_status=event.dispute_status,
                amount_deducted_minor=event.amount_deducted_minor,
            )
            .on_conflict_do_nothing(index_elements=["provider", "external_event_id"])
            .returning(PaymentProviderEvent.payment_provider_event_id)
        )
        if inserted_id is None:
            existing = await session.scalar(
                select(PaymentProviderEvent).where(
                    PaymentProviderEvent.provider == event.provider,
                    PaymentProviderEvent.external_event_id == event.external_event_id,
                )
            )
            if existing is None:
                raise RuntimeError("provider-event conflict did not resolve to a row")
            if existing.payload_hash != payload_hash:
                # Preserve the first observation. A changed payload is independent
                # contradictory evidence, never an exact replay or a second capture.
                conflict_key = (
                    "conflict:"
                    + hashlib.sha256(event.external_event_id.encode() + payload_hash).hexdigest()
                )
                conflict_id = await session.scalar(
                    insert(PaymentProviderEvent)
                    .values(
                        payment_provider_event_id=new_uuid7(),
                        provider=event.provider,
                        external_event_id=conflict_key,
                        event_type="evidence.conflict",
                        payload_hash=payload_hash,
                        received_at=now,
                        processed_at=now,
                        processing_status=EVENT_RECONCILIATION_REQUIRED,
                        failure_code="EVENT_PAYLOAD_CONFLICT",
                        evidence_source=event.evidence_source,
                        provider_account_key=event.provider_account_key,
                        observed_outcome=event.outcome.value,
                        provider_payment_id=event.provider_payment_id,
                        provider_order_id=event.provider_order_id,
                        provider_refund_id=event.provider_refund_id,
                        amount_minor=event.amount_minor,
                        currency=event.currency,
                        contradicted_event_id=existing.payment_provider_event_id,
                    )
                    .on_conflict_do_nothing(index_elements=["provider", "external_event_id"])
                    .returning(PaymentProviderEvent.payment_provider_event_id)
                )
                conflict = (
                    await session.get(PaymentProviderEvent, conflict_id)
                    if conflict_id
                    else await session.scalar(
                        select(PaymentProviderEvent).where(
                            PaymentProviderEvent.provider == event.provider,
                            PaymentProviderEvent.external_event_id == conflict_key,
                        )
                    )
                )
                assert conflict is not None
                await _flag_evidence_charge(
                    session,
                    conflict,
                    replace(
                        event,
                        provider_payment_id=existing.provider_payment_id
                        or event.provider_payment_id,
                        amount_minor=existing.amount_minor,
                        currency=existing.currency,
                        provider_account_key=existing.provider_account_key,
                    ),
                    "EVENT_PAYLOAD_CONFLICT",
                )
                return conflict
            if (
                existing.processing_status != EVENT_UNMATCHED
                or existing.failure_code == "EVENT_IGNORED"
            ):
                return existing
            # A mapping may become available after authenticated evidence arrived.
            # Same evidence can then converge without creating a second operation.
            inserted_id = existing.payment_provider_event_id

        provider_event = await session.get(PaymentProviderEvent, inserted_id)
        if provider_event is None:
            raise RuntimeError("inserted provider event could not be loaded")
        record = provider_event
        if event.outcome == PaymentEventOutcome.DISPUTED:
            await _flag_evidence_charge(session, record, event, "PAYMENT_DISPUTE")
            return record
        if event.outcome == PaymentEventOutcome.IGNORED:
            record.processing_status = EVENT_UNMATCHED
            record.failure_code = "EVENT_IGNORED"
            record.processed_at = now
            return record
        # Route explicitly based on presence of IDs
        has_refund_id = event.refund_id is not None or event.provider_refund_id is not None
        has_payment_id = event.payment_attempt_id is not None or (
            not has_refund_id
            and (event.provider_order_id is not None or event.provider_payment_id is not None)
        )

        if has_refund_id and not has_payment_id:
            return await _process_refund_event(session, record, event, now)

        if has_payment_id and not has_refund_id:
            # Continues below to payment attempt path
            if (
                event.outcome == PaymentEventOutcome.SUCCEEDED
                and event.validation_failure_code is None
            ):
                record.provider_payment_id = event.provider_payment_id
                record.amount_minor = (
                    event.amount_minor
                    if event.amount_minor is not None and event.amount_minor > 0
                    else None
                )
                record.currency = event.currency

        elif has_refund_id and has_payment_id:
            record.processing_status = EVENT_RECONCILIATION_REQUIRED
            record.processed_at = now
            return record

        else:
            # Neither
            record.processing_status = EVENT_UNMATCHED
            record.processed_at = now
            return record

        # Resolve immutable payment ownership without an attempt lock, then lock
        # payments in stable order BEFORE attempts. Refund FK checks under a
        # Payment lock must never wait for an attempt whose owner waits on Payment.
        matching = (
            PaymentAttempt.provider == event.provider,
            or_(
                PaymentAttempt.payment_attempt_id == event.payment_attempt_id,
                PaymentAttempt.provider_order_id == event.provider_order_id
                if event.provider_order_id is not None
                else false(),
                PaymentAttempt.provider_payment_id == event.provider_payment_id
                if event.provider_payment_id is not None
                else false(),
            ),
        )
        candidate_ids = list(
            await session.scalars(select(PaymentAttempt.payment_id).where(*matching).distinct())
        )
        if candidate_ids:
            await session.scalars(
                select(Payment)
                .where(Payment.payment_id.in_(candidate_ids))
                .order_by(Payment.payment_id)
                .with_for_update()
            )
        attempts = list(
            await session.scalars(
                select(PaymentAttempt)
                .where(*matching)
                .order_by(PaymentAttempt.payment_attempt_id)
                .with_for_update()
            )
        )
        if len(attempts) > 1:
            record.processing_status = EVENT_RECONCILIATION
            record.failure_code = "PAYMENT_IDENTITY_CONFLICT"
            record.processed_at = now
            return record
        attempt = attempts[0] if attempts else None
        if attempt is None:
            provider_event.processing_status = EVENT_UNMATCHED
            provider_event.failure_code = "ATTEMPT_NOT_MAPPED"
            provider_event.processed_at = utc_now()
            return provider_event
        if attempt.payment_id not in candidate_ids:
            # Mapping became visible after the unlocked lookup. Redelivery can
            # resolve it in Payment-first order; never take Payment after Attempt.
            raise PaymentNotEligibleError("Payment mapping changed during reconciliation")
        provider_event.payment_attempt_id = attempt.payment_attempt_id

        payment = await session.scalar(
            select(Payment).where(Payment.payment_id == attempt.payment_id).with_for_update()
        )
        if payment is None:
            raise RuntimeError("payment attempt references a missing payment")

        failure = event.validation_failure_code
        if (
            event.payment_attempt_id is not None
            and event.payment_attempt_id != attempt.payment_attempt_id
        ):
            failure = "PAYMENT_IDENTITY_CONFLICT"
        elif event.provider_order_id is not None and attempt.provider_order_id not in {
            None,
            event.provider_order_id,
        }:
            failure = "PAYMENT_IDENTITY_CONFLICT"
        elif event.amount_minor is not None and event.amount_minor != payment.amount_minor:
            failure = "AMOUNT_MISMATCH"
        elif event.currency is not None and event.currency != payment.currency:
            failure = "CURRENCY_MISMATCH"
        elif (
            event.provider == "RAZORPAY"
            and event.outcome == PaymentEventOutcome.SUCCEEDED
            and (event.amount_minor is None or event.currency is None)
        ):
            failure = "PAYMENT_FACTS_MISSING"
        elif (
            attempt.provider_account_key is not None
            and event.provider_account_key is not None
            and attempt.provider_account_key != event.provider_account_key
        ):
            failure = "PROVIDER_ACCOUNT_MISMATCH"
        if failure:
            record.processing_status = EVENT_RECONCILIATION
            record.failure_code = failure
            record.processed_at = now
            await open_exception(
                session, payment, failure, evidence_id=record.payment_provider_event_id
            )
            return record
        if event.outcome == PaymentEventOutcome.SUBMITTED:
            if attempt.status != ATTEMPT_SUCCEEDED:
                # Current provider inquiry showing an unresolved instrument revokes a
                # previously observed failed-order retry permission.
                attempt.status = ATTEMPT_PENDING
                attempt.failure_code = None
                attempt.next_check_at = now
            record.processing_status = EVENT_PROCESSED
            record.processed_at = now
            return record
        if event.outcome not in {PaymentEventOutcome.SUCCEEDED, PaymentEventOutcome.FAILED}:
            record.processing_status = EVENT_UNMATCHED
            record.processed_at = now
            return record
        if event.outcome == PaymentEventOutcome.FAILED:
            if attempt.status != ATTEMPT_SUCCEEDED:
                attempt.provider_order_id = event.provider_order_id or attempt.provider_order_id
                attempt.provider_payment_id = (
                    event.provider_payment_id or attempt.provider_payment_id
                )
                attempt.status = ATTEMPT_FAILED
                attempt.failure_code = event.failure_code or "PROVIDER_REPORTED_FAILURE"
                attempt.completed_at = utc_now()
                attempt.next_check_at = None
                other_unresolved = await session.scalar(
                    select(PaymentAttempt.payment_attempt_id)
                    .where(
                        PaymentAttempt.payment_id == payment.payment_id,
                        PaymentAttempt.payment_attempt_id != attempt.payment_attempt_id,
                        PaymentAttempt.status.in_({"CREATED", "PENDING", "INITIATION_UNCERTAIN"}),
                    )
                    .limit(1)
                )
                if other_unresolved is None:
                    await resolve_uncertainty(session, payment)
                await status_event(
                    session,
                    payment,
                    f"payment-failed:{attempt.payment_attempt_id}",
                    "PAYMENT_FAILED",
                )
            provider_event.processing_status = EVENT_PROCESSED
            provider_event.processed_at = utc_now()
            return provider_event

        charge = await record_charge(session, payment, attempt, record, event)
        if event.amount_minor is not None and event.provider_payment_id and charge is None:
            record.processing_status = EVENT_RECONCILIATION
            record.failure_code = "CHARGE_IDENTITY_CONFLICT"
            record.processed_at = now
            return record
        additional = payment.successful_attempt_id is not None and (
            payment.successful_attempt_id != attempt.payment_attempt_id
            or (
                attempt.status == ATTEMPT_SUCCEEDED
                and attempt.provider_payment_id != event.provider_payment_id
            )
        )
        attempt.provider_order_id = event.provider_order_id or attempt.provider_order_id
        if attempt.status != ATTEMPT_SUCCEEDED:
            attempt.provider_payment_id = event.provider_payment_id or attempt.provider_payment_id
            attempt.completed_at = now
        attempt.status = ATTEMPT_SUCCEEDED
        attempt.failure_code = None
        attempt.next_check_at = None
        if event.provider_account_key:
            attempt.provider_account_key = event.provider_account_key
        await resolve_uncertainty(session, payment)
        if additional and charge is not None:
            if idempotency_expires_at is None:
                raise PaymentNotEligibleError("Compensation retention is not configured")
            try:
                await compensate_charge(
                    session,
                    payment,
                    charge,
                    "ADDITIONAL_SUCCESS",
                    expires_at=idempotency_expires_at,
                )
            except RefundConflictError:
                await open_exception(
                    session,
                    payment,
                    "REFUND_REVIEW_REQUIRED",
                    charge=charge,
                    evidence_id=record.payment_provider_event_id,
                )
                record.processing_status = EVENT_RECONCILIATION
                record.failure_code = "REFUND_REVIEW_REQUIRED"
                record.processed_at = now
                return record
            record.processing_status = EVENT_PROCESSED
            record.failure_code = "ADDITIONAL_CAPTURE_COMPENSATED"
            record.processed_at = now
            return record

        if (
            payment.status == PAYMENT_SUCCEEDED
            and payment.successful_attempt_id == attempt.payment_attempt_id
        ):
            provider_event.processing_status = EVENT_PROCESSED
            provider_event.processed_at = utc_now()
            return provider_event

        if (
            payment.successful_attempt_id is not None
            and payment.successful_attempt_id != attempt.payment_attempt_id
        ):
            provider_event.processing_status = EVENT_RECONCILIATION
            provider_event.failure_code = "ADDITIONAL_SUCCESS"
            provider_event.processed_at = utc_now()
            return provider_event

        # Captured money is authoritative even when the booking cannot be accepted.
        was_pending = payment.status == PAYMENT_PENDING
        payment.status = PAYMENT_SUCCEEDED
        payment.successful_attempt_id = attempt.payment_attempt_id
        payment.succeeded_at = payment.succeeded_at or now
        await status_event(
            session, payment, f"payment-confirmed:{payment.payment_id}", "PAYMENT_CONFIRMED"
        )

        request = await session.get(CollectionRequest, payment.request_id)
        if request is None:
            raise RuntimeError("payment references a missing collection request")

        await acquire_work_unit_advisory_lock(
            session,
            cell_id=request.cell_id,
            slot_start=request.slot_start,
            slot_end=request.slot_end,
        )
        request = await session.scalar(
            select(CollectionRequest)
            .where(CollectionRequest.request_id == payment.request_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if request is None:
            raise RuntimeError("payment references a missing collection request")
        if request.status == "CANCELLED":
            if not payment.cancellation_compensation_authorized:
                await open_exception(
                    session,
                    payment,
                    "HISTORICAL_CANCELLED_MISSING_REFUND",
                    charge=charge,
                    evidence_id=record.payment_provider_event_id,
                )
                record.processing_status = EVENT_RECONCILIATION
                record.failure_code = "HISTORICAL_CANCELLED_MISSING_REFUND"
                record.processed_at = now
                return record
            if idempotency_expires_at is None:
                raise PaymentNotEligibleError("Compensation retention is not configured")
            # The charge is confirmed, but the collection commitment stays
            # cancelled. Canonical financial truth and its full refund/outbox
            # share the event transaction; no second acceptance is possible.
            payment.status = PAYMENT_SUCCEEDED
            payment.successful_attempt_id = attempt.payment_attempt_id
            payment.succeeded_at = attempt.completed_at
            try:
                await ensure_cancellation_refund(
                    session,
                    payment,
                    customer_id=request.customer_id,
                    idempotency_expires_at=idempotency_expires_at,
                )
            except RefundConflictError:
                # Preserve verified money and its investigation hold even when
                # the cancelled booking's automatic payout is unsafe.
                await open_exception(
                    session,
                    payment,
                    "REFUND_REVIEW_REQUIRED",
                    charge=charge,
                    evidence_id=record.payment_provider_event_id,
                )
                record.processing_status = EVENT_RECONCILIATION
                record.failure_code = "REFUND_REVIEW_REQUIRED"
                record.processed_at = now
                return record
            record.processing_status = EVENT_PROCESSED
            record.failure_code = "CANCELLED_CAPTURE_COMPENSATED"
            record.processed_at = utc_now()
            return record
        batch_exists = (
            await session.scalar(
                select(PlanningBatch.planning_batch_id).where(
                    PlanningBatch.cell_id == request.cell_id,
                    PlanningBatch.slot_start == request.slot_start,
                    PlanningBatch.slot_end == request.slot_end,
                )
            )
            is not None
        )
        if batch_exists:
            if request.status == REQUEST_PENDING_PAYMENT:
                request.status = "EXPIRED"
                request.expired_at = now
            provider_event.processing_status = EVENT_RECONCILIATION
            provider_event.failure_code = "WORK_UNIT_FROZEN"
            provider_event.processed_at = utc_now()
            await open_exception(
                session,
                payment,
                "WORK_UNIT_FROZEN",
                charge=charge,
                evidence_id=record.payment_provider_event_id,
            )
            return provider_event

        if planning_cutoff_reached(
            request.slot_start,
            planning_lead_time_minutes,
            now=utc_now(),
        ):
            if request.status == REQUEST_PENDING_PAYMENT:
                request.status = "EXPIRED"
                request.expired_at = now
            provider_event.processing_status = EVENT_RECONCILIATION
            provider_event.failure_code = "PLANNING_CUTOFF_REACHED"
            provider_event.processed_at = utc_now()
            await open_exception(
                session,
                payment,
                "PLANNING_CUTOFF_REACHED",
                charge=charge,
                evidence_id=record.payment_provider_event_id,
            )
            return provider_event

        if request.payment_expires_at <= utc_now():
            provider_event.processing_status = EVENT_RECONCILIATION
            provider_event.failure_code = "PAYMENT_WINDOW_EXPIRED"
            provider_event.processed_at = utc_now()
            if request.status == REQUEST_PENDING_PAYMENT:
                request.status = "EXPIRED"
                request.expired_at = now
            await open_exception(
                session,
                payment,
                "PAYMENT_WINDOW_EXPIRED",
                charge=charge,
                evidence_id=record.payment_provider_event_id,
            )
            return provider_event

        if was_pending and request.status == REQUEST_PENDING_PAYMENT:
            accepted_at = utc_now()
            transitioned_id = await session.scalar(
                update(CollectionRequest)
                .where(
                    CollectionRequest.request_id == request.request_id,
                    CollectionRequest.status == REQUEST_PENDING_PAYMENT,
                )
                .values(status=REQUEST_ACCEPTED, accepted_at=accepted_at)
                .returning(CollectionRequest.request_id)
            )
            if transitioned_id is None:
                raise RuntimeError("request acceptance lost its conditional transition")
            payment.status = PAYMENT_SUCCEEDED
            payment.successful_attempt_id = attempt.payment_attempt_id
            payment.succeeded_at = accepted_at
            await append_outbox_event(
                session,
                event_key=f"collection-request-accepted:{request.request_id}",
                aggregate_type="collection_request",
                aggregate_id=request.request_id,
                event_type="CollectionRequestAccepted",
                payload={"request_id": str(request.request_id)},
            )
            provider_event.processing_status = EVENT_PROCESSED
            provider_event.processed_at = accepted_at
            return provider_event

        provider_event.processing_status = EVENT_RECONCILIATION
        provider_event.failure_code = "ADDITIONAL_SUCCESS"
        provider_event.processed_at = utc_now()
        await open_exception(
            session,
            payment,
            "BOOKING_INELIGIBLE",
            charge=charge,
            evidence_id=record.payment_provider_event_id,
        )
        return provider_event


async def _flag_evidence_charge(
    session: AsyncSession,
    record: PaymentProviderEvent,
    event: AuthenticatedPaymentEvent,
    reason: str,
) -> None:
    charge = await session.scalar(
        select(CapturedCharge).where(
            CapturedCharge.provider == event.provider,
            CapturedCharge.provider_payment_id == event.provider_payment_id,
        )
    )
    if charge is None:
        record.processing_status = EVENT_UNMATCHED
        record.failure_code = reason
        record.processed_at = utc_now()
        return
    payment = await session.scalar(
        select(Payment).where(Payment.payment_id == charge.payment_id).with_for_update()
    )
    assert payment is not None
    valid = (
        event.provider_account_key == charge.provider_account_key
        and event.currency == charge.currency
        and event.amount_minor is not None
        and event.amount_minor <= charge.amount_minor
    )
    classified = reason if valid else "PROVIDER_EVIDENCE_MISMATCH"
    obligation = await session.scalar(
        select(RefundObligation).where(
            RefundObligation.captured_charge_id == charge.captured_charge_id
        )
    )
    if obligation:
        obligation.payout_blocked = True
    record.payment_attempt_id = charge.payment_attempt_id
    record.processing_status = EVENT_RECONCILIATION_REQUIRED
    record.failure_code = classified
    record.processed_at = utc_now()
    await open_exception(
        session, payment, classified, charge=charge, evidence_id=record.payment_provider_event_id
    )
    if reason == "PAYMENT_DISPUTE" and await session.scalar(
        select(Refund.refund_id)
        .where(
            Refund.captured_charge_id == charge.captured_charge_id,
            Refund.status.in_(["PROCESSING", "SUBMITTED", "INITIATION_UNCERTAIN", "SUCCEEDED"]),
        )
        .limit(1)
    ):
        await open_exception(
            session,
            payment,
            "REFUND_DISPUTE_DOUBLE_CREDIT_RISK",
            charge=charge,
            evidence_id=record.payment_provider_event_id,
        )


async def _process_refund_event(
    session: AsyncSession,
    record: PaymentProviderEvent,
    event: AuthenticatedPaymentEvent,
    now: datetime,
) -> PaymentProviderEvent:
    # Both authenticated identities must resolve to the same refund; neither may win silently.
    matching = or_(
        Refund.refund_id == event.refund_id,
        and_(
            Refund.provider == event.provider, Refund.provider_refund_id == event.provider_refund_id
        )
        if event.provider_refund_id is not None
        else false(),
    )
    payment_ids = list(await session.scalars(select(Refund.payment_id).where(matching).distinct()))
    if payment_ids:
        await session.scalars(
            select(Payment)
            .where(Payment.payment_id.in_(payment_ids))
            .order_by(Payment.payment_id)
            .with_for_update()
        )
    refunds = list(
        await session.scalars(
            select(Refund)
            .where(
                or_(
                    Refund.refund_id == event.refund_id,
                    and_(
                        Refund.provider == event.provider,
                        Refund.provider_refund_id == event.provider_refund_id,
                    )
                    if event.provider_refund_id is not None
                    else false(),
                ),
            )
            .order_by(Refund.refund_id)
            .with_for_update()
        )
    )
    if len(refunds) > 1 or (
        len(refunds) == 1
        and event.refund_id is not None
        and refunds[0].refund_id != event.refund_id
    ):
        record.processing_status = EVENT_RECONCILIATION_REQUIRED
        record.failure_code = "REFUND_IDENTITY_CONFLICT"
        record.processed_at = now
        for conflicting in refunds:
            payment = await session.get(Payment, conflicting.payment_id)
            assert payment is not None
            obligation = (
                await session.get(RefundObligation, conflicting.refund_obligation_id)
                if conflicting.refund_obligation_id
                else None
            )
            if obligation:
                obligation.payout_blocked = True
            await open_exception(
                session,
                payment,
                "REFUND_IDENTITY_CONFLICT",
                refund=conflicting,
                evidence_id=record.payment_provider_event_id,
            )
        return record
    refund = refunds[0] if refunds else None

    if not refund:
        record.processing_status = EVENT_UNMATCHED
        record.processed_at = now
        await _flag_evidence_charge(session, record, event, "EXTERNAL_REFUND_UNMAPPED")
        return record
    if refund.payment_id not in payment_ids:
        raise PaymentNotEligibleError("Refund mapping changed during reconciliation")
    payment = await session.get(Payment, refund.payment_id)
    assert payment is not None

    if refund.provider != event.provider:
        record.processing_status = EVENT_RECONCILIATION_REQUIRED
        record.failure_code = "REFUND_PROVIDER_MISMATCH"
        record.processed_at = now
        await open_exception(
            session,
            payment,
            "REFUND_PROVIDER_MISMATCH",
            refund=refund,
            evidence_id=record.payment_provider_event_id,
        )
        return record

    record.refund_id = refund.refund_id

    attempt = await session.get(PaymentAttempt, refund.payment_attempt_id)
    charge = (
        await session.get(CapturedCharge, refund.captured_charge_id)
        if refund.captured_charge_id
        else None
    )
    failure = event.validation_failure_code
    if event.amount_minor is not None and event.amount_minor != refund.amount_minor:
        failure = "REFUND_AMOUNT_MISMATCH"
    elif event.currency is not None and event.currency != refund.currency:
        failure = "REFUND_CURRENCY_MISMATCH"
    elif event.provider_payment_id is not None and (
        event.provider_payment_id
        != (
            charge.provider_payment_id
            if charge
            else attempt.provider_payment_id
            if attempt
            else None
        )
    ):
        failure = "REFUND_PAYMENT_MISMATCH"
    elif (
        charge
        and event.provider_account_key
        and charge.provider_account_key
        and charge.provider_account_key != event.provider_account_key
    ):
        failure = "PROVIDER_ACCOUNT_MISMATCH"
    if failure:
        record.processing_status = EVENT_RECONCILIATION_REQUIRED
        record.failure_code = failure
        record.processed_at = now
        await open_exception(
            session,
            payment,
            failure,
            charge=charge,
            refund=refund,
            evidence_id=record.payment_provider_event_id,
        )
        return record

    if event.provider_refund_id is not None:
        mapped_id = await session.scalar(
            select(Refund.refund_id).where(
                Refund.provider == event.provider,
                Refund.provider_refund_id == event.provider_refund_id,
            )
        )
        if mapped_id is not None and mapped_id != refund.refund_id:
            record.processing_status = EVENT_RECONCILIATION_REQUIRED
            record.failure_code = "REFUND_IDENTITY_CONFLICT"
            record.processed_at = now
            await open_exception(
                session,
                payment,
                "REFUND_IDENTITY_CONFLICT",
                charge=charge,
                refund=refund,
                evidence_id=record.payment_provider_event_id,
            )
            return record

    # If provider_refund_id is known, and refund lacks it, set it (unless conflict)
    if event.provider_refund_id and not refund.provider_refund_id:
        refund.provider_refund_id = event.provider_refund_id
    elif event.provider_refund_id and refund.provider_refund_id != event.provider_refund_id:
        # Correlation conflict
        record.processing_status = EVENT_RECONCILIATION_REQUIRED
        record.failure_code = "REFUND_IDENTITY_CONFLICT"
        if event.evidence_source == "PROVIDER_RESPONSE" and refund.status not in {
            "SUCCEEDED",
            "FAILED",
        }:
            refund.status = "INITIATION_UNCERTAIN"
        await open_exception(
            session,
            payment,
            "REFUND_IDENTITY_CONFLICT",
            charge=charge,
            refund=refund,
            evidence_id=record.payment_provider_event_id,
        )
        record.processed_at = now
        return record

    # Terminal outcomes shouldn't be overwritten blindly

    if event.outcome == PaymentEventOutcome.SUCCEEDED:
        if refund.status == "SUCCEEDED":
            pass  # duplicate, idempotent
        elif refund.status == "FAILED" and refund.refund_obligation_id is None:
            # Conflict with local terminal state
            record.processing_status = EVENT_RECONCILIATION_REQUIRED
        else:
            if refund.status == "FAILED" and refund.non_payable_verified_at is not None:
                obligation = await session.get(RefundObligation, refund.refund_obligation_id)
                if obligation:
                    obligation.payout_blocked = True
                await open_exception(
                    session,
                    payment,
                    "REFUND_FINALITY_CONTRADICTION",
                    charge=charge,
                    refund=refund,
                    evidence_id=record.payment_provider_event_id,
                )
            refund.status = "SUCCEEDED"
            refund.non_payable_verified_at = None
            refund.next_check_at = None
            refund.completed_at = now
            record.processing_status = EVENT_PROCESSED
            await status_event(
                session, payment, f"refund-completed:{refund.refund_id}", "REFUND_COMPLETED"
            )
    elif event.outcome == PaymentEventOutcome.FAILED:
        if refund.status == "FAILED":
            pass  # duplicate, idempotent
        elif refund.status == "SUCCEEDED":
            record.processing_status = EVENT_RECONCILIATION_REQUIRED
        else:
            refund.status = "FAILED"
            refund.completed_at = now
            record.processing_status = EVENT_PROCESSED
        if refund.status == "FAILED":
            if event.evidence_source == "API_INQUIRY" and event.definitive_non_payable:
                refund.non_payable_verified_at = now
                refund.failure_evidence_id = record.payment_provider_event_id
            await open_exception(
                session,
                payment,
                "REFUND_FAILED",
                charge=charge,
                refund=refund,
                evidence_id=record.payment_provider_event_id,
            )
            await status_event(
                session, payment, f"refund-failed:{refund.refund_id}", "REFUND_FAILED"
            )
    elif event.outcome == PaymentEventOutcome.SUBMITTED:
        if refund.status == "FAILED" and refund.non_payable_verified_at is not None:
            obligation = await session.get(RefundObligation, refund.refund_obligation_id)
            if obligation:
                obligation.payout_blocked = True
            refund.non_payable_verified_at = None
            record.processing_status = EVENT_RECONCILIATION_REQUIRED
            record.failure_code = "REFUND_FINALITY_CONTRADICTION"
            await open_exception(
                session,
                payment,
                "REFUND_FINALITY_CONTRADICTION",
                charge=charge,
                refund=refund,
                evidence_id=record.payment_provider_event_id,
            )
        if refund.status not in {"SUCCEEDED", "FAILED"}:
            refund.status = "SUBMITTED"

    if record.processing_status == EVENT_RECEIVED:
        record.processing_status = EVENT_PROCESSED

    if record.processing_status == EVENT_PROCESSED and refund.status in {"SUCCEEDED", "FAILED"}:
        await resolve_uncertainty(session, payment, refund=refund)

    record.processed_at = now
    return record
