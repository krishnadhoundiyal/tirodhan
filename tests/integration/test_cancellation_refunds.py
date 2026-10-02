from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest import mock

import pytest
from sqlalchemy import func, insert, select

from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.collection_requests.cancellation import (
    CancellationConflictError,
    CancellationNotAuthorizedError,
    cancel_collection_request_by_customer,
)
from tirodhan.modules.collection_requests.expiry import expire_pending_collection_requests
from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.identity.models import AppUser
from tirodhan.modules.payments.models import Payment, PaymentAttempt, PaymentProviderEvent, Refund
from tirodhan.modules.payments.ports import (
    AuthenticatedPaymentEvent,
    PaymentEventOutcome,
    RefundInitiationOutcome,
    RefundInitiationResult,
    RefundProvider,
)
from tirodhan.modules.payments.refunds import (
    RefundConflictError,
    create_refund,
    execute_refund_provider_call,
)
from tirodhan.modules.payments.service import (
    EVENT_PROCESSED,
    EVENT_RECONCILIATION_REQUIRED,
    process_authenticated_payment_event,
)
from tirodhan.modules.planning.service import PlanningWorkUnit, freeze_planning_batch
from tirodhan.modules.reliability.models import OutboxEvent
from tirodhan.modules.reliability.primitives import IdempotencyKeyConflictError
from tirodhan.modules.serviceability.models import ServiceabilityContext
from tirodhan.modules.serviceability.service import SERVICEABILITY_SERVICEABLE

pytestmark = pytest.mark.asyncio

RESERVING_REFUND_STATUSES = (
    "PENDING",
    "PROCESSING",
    "SUBMITTED",
    "SUCCEEDED",
    "INITIATION_UNCERTAIN",
)


class FakeRefundProvider(RefundProvider):
    def __init__(self, code: str = "fake") -> None:
        self._code = code
        self.calls: list[dict[str, object]] = []
        self.next_outcome = RefundInitiationOutcome.SUCCEEDED
        self.next_provider_refund_id = "pr_123"
        self.fail_uncertain = False

    @property
    def provider_code(self) -> str:
        return self._code

    async def initiate_refund(
        self,
        *,
        refund_id,
        provider_payment_id,
        amount_minor,
        currency,
        provider_idempotency_key,
    ) -> RefundInitiationResult:
        if self.fail_uncertain:
            from tirodhan.modules.payments.ports import PaymentProviderUncertainError

            raise PaymentProviderUncertainError()

        self.calls.append(
            {
                "refund_id": refund_id,
                "provider_payment_id": provider_payment_id,
                "amount_minor": amount_minor,
                "currency": currency,
                "provider_idempotency_key": provider_idempotency_key,
            }
        )
        return RefundInitiationResult(
            outcome=self.next_outcome,
            provider_refund_id=self.next_provider_refund_id,
        )


async def create_dummy_request(
    session,
    *,
    status: str = "ACCEPTED",
    payment_expires_in: timedelta = timedelta(hours=1),
):
    now = utc_now()
    customer_id = new_uuid7()
    request_id = new_uuid7()

    await session.execute(
        insert(AppUser).values(
            user_id=customer_id,
            status="ACTIVE",
            created_at=now,
            updated_at=now,
        )
    )

    context_id = new_uuid7()
    await session.execute(
        insert(ServiceabilityContext).values(
            serviceability_context_id=context_id,
            user_id=customer_id,
            address_snapshot_encrypted=b"enc",
            location=func.ST_SetSRID(func.ST_MakePoint(77.0, 28.0), 4326),
            cell_id="8a3cf13463a7fff",
            status=SERVICEABILITY_SERVICEABLE,
            expires_at=now + timedelta(days=1),
            created_at=now,
            resolved_at=now,
        )
    )

    request = CollectionRequest(
        request_id=request_id,
        client_request_id=new_uuid7(),
        customer_id=customer_id,
        serviceability_context_id=context_id,
        pickup_address_snapshot_encrypted=b"enc",
        pickup_location=func.ST_SetSRID(func.ST_MakePoint(77.0, 28.0), 4326),
        cell_id="8a3cf13463a7fff",
        slot_start=now + timedelta(hours=24),
        slot_end=now + timedelta(hours=28),
        quoted_amount_minor=1000,
        currency="INR",
        status=status,
        payment_expires_at=now + payment_expires_in,
        created_at=now,
    )
    session.add(request)
    await session.flush()
    return request


async def create_successful_payment(session, request: CollectionRequest, amount_minor: int = 1000):
    payment_id = new_uuid7()
    attempt_id = new_uuid7()
    now = utc_now()

    payment = Payment(
        payment_id=payment_id,
        request_id=request.request_id,
        amount_minor=amount_minor,
        currency="INR",
        status="SUCCEEDED",
        successful_attempt_id=attempt_id,
        created_at=now,
        succeeded_at=now,
    )
    attempt = PaymentAttempt(
        payment_attempt_id=attempt_id,
        payment_id=payment_id,
        provider="fake",
        provider_payment_id=f"pp_{attempt_id}",
        provider_idempotency_key=f"payment-attempt:{attempt_id}",
        status="SUCCEEDED",
        created_at=now,
        completed_at=now,
    )
    session.add_all([payment, attempt])
    await session.flush()
    return payment, attempt


async def test_cancellation_own_accepted_request(database_session_factory):
    async with database_session_factory() as session, session.begin():
        request = await create_dummy_request(session)
        result = await cancel_collection_request_by_customer(
            session,
            request_id=request.request_id,
            customer_id=request.customer_id,
            idempotency_key="ik_cancel_1",
            planning_lead_time_minutes=120,
            idempotency_expires_at=utc_now() + timedelta(days=1),
        )

        assert result.status == "CANCELLED"
        assert result.cancelled_at is not None


async def test_cancellation_foreign_customer_forbidden(database_session_factory):
    async with database_session_factory() as session, session.begin():
        request = await create_dummy_request(session)
        with pytest.raises(CancellationNotAuthorizedError):
            await cancel_collection_request_by_customer(
                session,
                request_id=request.request_id,
                customer_id=new_uuid7(),
                idempotency_key="ik_cancel_2",
                planning_lead_time_minutes=120,
                idempotency_expires_at=utc_now() + timedelta(days=1),
            )


async def test_cancellation_concurrent_race(database_session_factory):
    async with database_session_factory() as session, session.begin():
        request = await create_dummy_request(session)
        request_id = request.request_id
        customer_id = request.customer_id
        work_unit = PlanningWorkUnit(
            cell_id=request.cell_id,
            slot_start=request.slot_start,
            slot_end=request.slot_end,
        )

    async def run_freeze():
        with mock.patch(
            "tirodhan.modules.planning.service.planning_cutoff_reached",
            return_value=True,
        ):
            return await freeze_planning_batch(
                database_session_factory,
                work_unit,
                lead_time_minutes=120,
                max_attempts=3,
                compaction_distance_m=500,
                max_group_requests=4,
            )

    async def run_cancel():
        async with database_session_factory() as session, session.begin():
            with mock.patch(
                "tirodhan.modules.collection_requests.cancellation.planning_cutoff_reached",
                return_value=False,
            ):
                try:
                    return await cancel_collection_request_by_customer(
                        session,
                        request_id=request_id,
                        customer_id=customer_id,
                        idempotency_key="ik_race_cancel",
                        planning_lead_time_minutes=120,
                        idempotency_expires_at=utc_now() + timedelta(days=1),
                    )
                except CancellationConflictError:
                    return None

    freeze_result, cancel_result = await asyncio.gather(run_freeze(), run_cancel())

    async with database_session_factory() as session, session.begin():
        final_request = await session.scalar(
            select(CollectionRequest).where(CollectionRequest.request_id == request_id)
        )
        assert final_request is not None

        if final_request.status == "CANCELLED":
            assert cancel_result is not None
            assert freeze_result.transitioned_request_count == 0
            assert final_request.planning_batch_id is None
        else:
            assert final_request.status == "PRE_PLANNING"
            assert cancel_result is None
            assert freeze_result.transitioned_request_count == 1
            assert freeze_result.batch is not None
            assert final_request.planning_batch_id == freeze_result.batch.planning_batch_id


async def test_expiry_exact_evaluation_time(database_session_factory):
    async with database_session_factory() as session, session.begin():
        request = await create_dummy_request(
            session,
            status="PENDING_PAYMENT",
            payment_expires_in=timedelta(seconds=1),
        )
        payment = Payment(
            payment_id=new_uuid7(),
            request_id=request.request_id,
            amount_minor=1000,
            currency="INR",
            status="PENDING",
            created_at=utc_now(),
        )
        session.add(payment)
        evaluation_time = request.payment_expires_at

    async with database_session_factory() as session, session.begin():
        expired_count = await expire_pending_collection_requests(session, evaluation_time)
        assert expired_count == 1

    async with database_session_factory() as session, session.begin():
        request_check = await session.get(CollectionRequest, request.request_id)
        payment_check = await session.get(Payment, payment.payment_id)
        assert request_check is not None
        assert payment_check is not None
        assert request_check.status == "EXPIRED"
        assert payment_check.status == "EXPIRED"
        assert request_check.expired_at == evaluation_time
        assert payment_check.expired_at == evaluation_time


async def test_expiry_vs_provider_success_concurrency(database_session_factory):
    async with database_session_factory() as session, session.begin():
        request = await create_dummy_request(
            session,
            status="PENDING_PAYMENT",
            payment_expires_in=timedelta(hours=1),
        )
        payment_id = new_uuid7()
        attempt_id = new_uuid7()
        now = utc_now()
        payment = Payment(
            payment_id=payment_id,
            request_id=request.request_id,
            amount_minor=1000,
            currency="INR",
            status="PENDING",
            created_at=now,
        )
        attempt = PaymentAttempt(
            payment_attempt_id=attempt_id,
            payment_id=payment_id,
            provider="fake",
            provider_idempotency_key=f"payment-attempt:{attempt_id}",
            status="PENDING",
            created_at=now,
        )
        session.add_all([payment, attempt])
        request_id = request.request_id
        evaluation_time = request.payment_expires_at + timedelta(seconds=1)

    async def run_expire():
        async with database_session_factory() as session, session.begin():
            return await expire_pending_collection_requests(session, evaluation_time)

    async def run_success():
        event = AuthenticatedPaymentEvent(
            provider="fake",
            external_event_id="ext_race_1",
            event_type="payment.succeeded",
            outcome=PaymentEventOutcome.SUCCEEDED,
            payment_attempt_id=attempt_id,
        )
        return await process_authenticated_payment_event(
            database_session_factory,
            event=event,
            payload_hash=b"hash",
            planning_lead_time_minutes=120,
        )

    await asyncio.gather(run_expire(), run_success())

    async with database_session_factory() as session, session.begin():
        request_check = await session.get(CollectionRequest, request_id)
        payment_check = await session.get(Payment, payment_id)
        event_check = await session.scalar(
            select(PaymentProviderEvent).where(
                PaymentProviderEvent.external_event_id == "ext_race_1"
            )
        )
        assert request_check is not None
        assert payment_check is not None
        assert event_check is not None

        if request_check.status == "ACCEPTED":
            assert payment_check.status == "SUCCEEDED"
            assert event_check.processing_status == EVENT_PROCESSED
        else:
            assert request_check.status == "EXPIRED"
            assert payment_check.status == "EXPIRED"
            assert event_check.processing_status == EVENT_RECONCILIATION_REQUIRED

    async with database_session_factory() as session, session.begin():
        second_expiry = await expire_pending_collection_requests(session, evaluation_time)
        assert second_expiry == 0


async def test_refund_creation_and_execution(database_session_factory):
    async with database_session_factory() as session, session.begin():
        request = await create_dummy_request(session)
        payment, attempt = await create_successful_payment(session, request)
        refund = await create_refund(
            session,
            payment_id=payment.payment_id,
            payment_attempt_id=attempt.payment_attempt_id,
            amount_minor=1000,
            reason_code="CUSTOMER_CANCELLATION",
            idempotency_key="rk_1",
            idempotency_expires_at=utc_now() + timedelta(days=1),
            requested_by_user_id=request.customer_id,
        )
        refund_id = refund.refund_id

    provider = FakeRefundProvider()
    await execute_refund_provider_call(database_session_factory, refund_id, provider)

    async with database_session_factory() as session, session.begin():
        refund_check = await session.get(Refund, refund_id)
        assert refund_check is not None
        assert refund_check.status == "SUCCEEDED"
        assert refund_check.provider_refund_id == "pr_123"
        assert len(provider.calls) == 1


async def test_refund_over_refund_rejection(database_session_factory):
    async with database_session_factory() as session, session.begin():
        request = await create_dummy_request(session)
        payment, attempt = await create_successful_payment(session, request)
        payment_id = payment.payment_id
        attempt_id = attempt.payment_attempt_id
        customer_id = request.customer_id

    async def create_amount(amount: int, key: str):
        async with database_session_factory() as session, session.begin():
            return await create_refund(
                session,
                payment_id=payment_id,
                payment_attempt_id=attempt_id,
                amount_minor=amount,
                reason_code="CUSTOMER_CANCELLATION",
                idempotency_key=key,
                idempotency_expires_at=utc_now() + timedelta(days=1),
                requested_by_user_id=customer_id,
            )

    results = await asyncio.gather(
        create_amount(700, "refund-700-a"),
        create_amount(700, "refund-700-b"),
        return_exceptions=True,
    )
    assert sum(isinstance(result, Refund) for result in results) == 1
    assert sum(isinstance(result, RefundConflictError) for result in results) == 1

    async with database_session_factory() as session, session.begin():
        reserved = await session.scalar(
            select(func.coalesce(func.sum(Refund.amount_minor), 0)).where(
                Refund.payment_id == payment_id,
                Refund.status.in_(RESERVING_REFUND_STATUSES),
            )
        )
        assert reserved <= 1000


async def test_concurrent_refunds_that_fit_both_succeed(database_session_factory):
    async with database_session_factory() as session, session.begin():
        request = await create_dummy_request(session)
        payment, attempt = await create_successful_payment(session, request)
        payment_id = payment.payment_id
        attempt_id = attempt.payment_attempt_id

    async def create_amount(amount: int, key: str):
        async with database_session_factory() as session, session.begin():
            return await create_refund(
                session,
                payment_id=payment_id,
                payment_attempt_id=attempt_id,
                amount_minor=amount,
                reason_code="OPERATIONS_ADJUSTMENT",
                idempotency_key=key,
                idempotency_expires_at=utc_now() + timedelta(days=1),
            )

    first, second = await asyncio.gather(
        create_amount(400, "refund-400"),
        create_amount(600, "refund-600"),
    )
    assert isinstance(first, Refund)
    assert isinstance(second, Refund)

    async with database_session_factory() as session, session.begin():
        reserved = await session.scalar(
            select(func.coalesce(func.sum(Refund.amount_minor), 0)).where(
                Refund.payment_id == payment_id,
                Refund.status.in_(RESERVING_REFUND_STATUSES),
            )
        )
        assert reserved == 1000


async def test_failed_refund_releases_balance_but_uncertain_reserves(database_session_factory):
    async with database_session_factory() as session, session.begin():
        request = await create_dummy_request(session)
        payment, attempt = await create_successful_payment(session, request)
        failed_refund = await create_refund(
            session,
            payment_id=payment.payment_id,
            payment_attempt_id=attempt.payment_attempt_id,
            amount_minor=700,
            reason_code="OPERATIONS_ADJUSTMENT",
            idempotency_key="failed-reservation",
            idempotency_expires_at=utc_now() + timedelta(days=1),
        )
        failed_refund.status = "FAILED"
        payment_id = payment.payment_id
        attempt_id = attempt.payment_attempt_id

    async with database_session_factory() as session, session.begin():
        replacement = await create_refund(
            session,
            payment_id=payment_id,
            payment_attempt_id=attempt_id,
            amount_minor=700,
            reason_code="OPERATIONS_ADJUSTMENT",
            idempotency_key="replacement-after-failed",
            idempotency_expires_at=utc_now() + timedelta(days=1),
        )
        replacement.status = "INITIATION_UNCERTAIN"

    async with database_session_factory() as session, session.begin():
        with pytest.raises(RefundConflictError):
            await create_refund(
                session,
                payment_id=payment_id,
                payment_attempt_id=attempt_id,
                amount_minor=400,
                reason_code="OPERATIONS_ADJUSTMENT",
                idempotency_key="blocked-by-uncertain",
                idempotency_expires_at=utc_now() + timedelta(days=1),
            )


async def test_refund_webhook_correlation_by_refund_id(database_session_factory):
    async with database_session_factory() as session, session.begin():
        request = await create_dummy_request(session)
        payment, attempt = await create_successful_payment(session, request)
        refund = await create_refund(
            session,
            payment_id=payment.payment_id,
            payment_attempt_id=attempt.payment_attempt_id,
            amount_minor=1000,
            reason_code="CUSTOMER_CANCELLATION",
            idempotency_key="rk_webhook",
            idempotency_expires_at=utc_now() + timedelta(days=1),
            requested_by_user_id=request.customer_id,
        )
        refund_id = refund.refund_id

    event = AuthenticatedPaymentEvent(
        provider="fake",
        external_event_id="ext_evt_1",
        event_type="refund.succeeded",
        outcome=PaymentEventOutcome.SUCCEEDED,
        payment_attempt_id=None,
        refund_id=refund_id,
        provider_refund_id="pr_456",
    )
    provider_event = await process_authenticated_payment_event(
        database_session_factory,
        event=event,
        payload_hash=b"hash",
        planning_lead_time_minutes=120,
    )

    assert provider_event.processing_status == EVENT_PROCESSED
    assert provider_event.refund_id == refund_id

    async with database_session_factory() as session, session.begin():
        refund_check = await session.get(Refund, refund_id)
        assert refund_check is not None
        assert refund_check.status == "SUCCEEDED"
        assert refund_check.provider_refund_id == "pr_456"


async def test_refund_exact_command_replay(database_session_factory):
    async with database_session_factory() as session, session.begin():
        request = await create_dummy_request(session)
        payment, attempt = await create_successful_payment(session, request)
        payment_id = payment.payment_id
        attempt_id = attempt.payment_attempt_id

    async with database_session_factory() as session, session.begin():
        first = await create_refund(
            session,
            payment_id=payment_id,
            payment_attempt_id=attempt_id,
            amount_minor=100,
            reason_code="CUSTOMER_CANCELLATION",
            idempotency_key="ik_replay",
            idempotency_expires_at=utc_now() + timedelta(days=1),
        )
        first_id = first.refund_id

    async with database_session_factory() as session, session.begin():
        second = await create_refund(
            session,
            payment_id=payment_id,
            payment_attempt_id=attempt_id,
            amount_minor=100,
            reason_code="CUSTOMER_CANCELLATION",
            idempotency_key="ik_replay",
            idempotency_expires_at=utc_now() + timedelta(days=1),
        )
        assert second.refund_id == first_id

    async with database_session_factory() as session, session.begin():
        refund_count = await session.scalar(
            select(func.count()).select_from(Refund).where(Refund.payment_id == payment_id)
        )
        outbox_count = await session.scalar(
            select(func.count())
            .select_from(OutboxEvent)
            .where(OutboxEvent.event_key == f"refund-requested:{first_id}")
        )
        persisted = await session.get(Refund, first_id)
        assert refund_count == 1
        assert outbox_count == 1
        assert persisted is not None
        assert persisted.provider_idempotency_key == f"refund:{first_id}"


async def test_refund_fingerprint_conflict(database_session_factory):
    async with database_session_factory() as session, session.begin():
        request = await create_dummy_request(session)
        payment, attempt = await create_successful_payment(session, request)
        payment_id = payment.payment_id
        attempt_id = attempt.payment_attempt_id

    async with database_session_factory() as session, session.begin():
        await create_refund(
            session,
            payment_id=payment_id,
            payment_attempt_id=attempt_id,
            amount_minor=100,
            reason_code="CUSTOMER_CANCELLATION",
            idempotency_key="ik_conflict",
            idempotency_expires_at=utc_now() + timedelta(days=1),
        )

    async with database_session_factory() as session, session.begin():
        with pytest.raises(IdempotencyKeyConflictError):
            await create_refund(
                session,
                payment_id=payment_id,
                payment_attempt_id=attempt_id,
                amount_minor=200,
                reason_code="CUSTOMER_CANCELLATION",
                idempotency_key="ik_conflict",
                idempotency_expires_at=utc_now() + timedelta(days=1),
            )
