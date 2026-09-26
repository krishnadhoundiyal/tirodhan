from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select, update
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
from tirodhan.modules.payments.models import Payment, PaymentAttempt, PaymentProviderEvent
from tirodhan.modules.payments.ports import (
    AuthenticatedPaymentEvent,
    PaymentEventOutcome,
    PaymentInitiationOutcome,
    PaymentProvider,
    PaymentProviderUncertainError,
)
from tirodhan.modules.planning.locking import acquire_work_unit_advisory_lock
from tirodhan.modules.planning.models import PlanningBatch
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
            attempt_id = new_uuid7()
            attempt = PaymentAttempt(
                payment_attempt_id=attempt_id,
                payment_id=payment.payment_id,
                provider=provider_code,
                provider_idempotency_key=f"payment-attempt:{attempt_id}",
                status=ATTEMPT_CREATED,
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
) -> PaymentProviderEvent:
    now = utc_now()
    async with session_factory() as session, session.begin():
        event_id = new_uuid7()
        inserted_id = await session.scalar(
            insert(PaymentProviderEvent)
            .values(
                payment_provider_event_id=event_id,
                provider=event.provider,
                external_event_id=event.external_event_id,
                event_type=event.event_type,
                payment_attempt_id=None,
                payload_hash=payload_hash,
                processing_status=EVENT_RECEIVED,
                received_at=now,
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
            return existing

        provider_event = await session.get(PaymentProviderEvent, inserted_id)
        if provider_event is None:
            raise RuntimeError("inserted provider event could not be loaded")
        if event.payment_attempt_id is None:
            provider_event.processing_status = EVENT_UNMATCHED
            provider_event.failure_code = "ATTEMPT_NOT_MAPPED"
            provider_event.processed_at = utc_now()
            return provider_event

        attempt = await session.scalar(
            select(PaymentAttempt)
            .where(PaymentAttempt.payment_attempt_id == event.payment_attempt_id)
            .with_for_update()
        )
        if attempt is None or attempt.provider != event.provider:
            provider_event.processing_status = EVENT_UNMATCHED
            provider_event.failure_code = "ATTEMPT_NOT_MAPPED"
            provider_event.processed_at = utc_now()
            return provider_event
        provider_event.payment_attempt_id = attempt.payment_attempt_id

        payment = await session.scalar(
            select(Payment).where(Payment.payment_id == attempt.payment_id).with_for_update()
        )
        if payment is None:
            raise RuntimeError("payment attempt references a missing payment")
        request = await session.get(CollectionRequest, payment.request_id)
        if request is None:
            raise RuntimeError("payment references a missing collection request")

        attempt.provider_order_id = event.provider_order_id or attempt.provider_order_id
        attempt.provider_payment_id = event.provider_payment_id or attempt.provider_payment_id
        if event.outcome == PaymentEventOutcome.FAILED:
            if attempt.status != ATTEMPT_SUCCEEDED:
                attempt.status = ATTEMPT_FAILED
                attempt.failure_code = event.failure_code or "PROVIDER_REPORTED_FAILURE"
                attempt.completed_at = utc_now()
            provider_event.processing_status = EVENT_PROCESSED
            provider_event.processed_at = utc_now()
            return provider_event

        attempt.status = ATTEMPT_SUCCEEDED
        attempt.failure_code = None
        attempt.completed_at = utc_now()

        await acquire_work_unit_advisory_lock(
            session,
            cell_id=request.cell_id,
            slot_start=request.slot_start,
            slot_end=request.slot_end,
        )
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
            provider_event.processing_status = EVENT_RECONCILIATION
            provider_event.failure_code = "WORK_UNIT_FROZEN"
            provider_event.processed_at = utc_now()
            return provider_event

        if request.payment_expires_at <= utc_now():
            provider_event.processing_status = EVENT_RECONCILIATION
            provider_event.failure_code = "PAYMENT_WINDOW_EXPIRED"
            provider_event.processed_at = utc_now()
            return provider_event

        if (
            payment.status == PAYMENT_PENDING
            and payment.successful_attempt_id is None
            and request.status == REQUEST_PENDING_PAYMENT
        ):
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
        return provider_event
