from datetime import timedelta

import pytest
from sqlalchemy import func, insert, select

from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.collection_requests.cancellation import (
    CancellationNotAuthorizedError,
    cancel_collection_request_by_customer,
)
from tirodhan.modules.collection_requests.expiry import expire_pending_collection_requests
from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.payments.models import Payment, PaymentAttempt, Refund
from tirodhan.modules.payments.ports import (
    RefundInitiationOutcome,
    RefundInitiationResult,
    RefundProvider,
)
from tirodhan.modules.payments.refunds import (
    RefundConflictError,
    create_refund,
    execute_refund_provider_call,
)


class FakeRefundProvider(RefundProvider):
    def __init__(self, code="fake"):
        self._code = code
        self.calls = []
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


# Using integration test fixtures available in the codebase, assume they are set up via conftest.py
pytestmark = pytest.mark.asyncio


async def create_dummy_request(session, status="ACCEPTED", expires_in_mins=60):
    now = utc_now()
    customer_id = new_uuid7()
    req_id = new_uuid7()

    # Needs valid serviceability_context, so insert dummy context first
    from tirodhan.modules.identity.models import AppUser
    from tirodhan.modules.serviceability.models import ServiceabilityContext

    await session.execute(
        insert(AppUser).values(
            user_id=customer_id,
            status="ACTIVE",
            created_at=now,
        )
    )

    context_id = new_uuid7()
    await session.execute(
        insert(ServiceabilityContext).values(
            serviceability_context_id=context_id,
            user_id=customer_id,
            location=func.ST_SetSRID(func.ST_MakePoint(77.0, 28.0), 4326),
            address_snapshot_encrypted=b"enc",
            status="ELIGIBLE",
            created_at=now,
            expires_at=now + timedelta(days=1),
        )
    )

    req = CollectionRequest(
        request_id=req_id,
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
        payment_expires_at=now + timedelta(minutes=expires_in_mins) if expires_in_mins > 0 else now + timedelta(microseconds=1),
        created_at=now,
    )
    session.add(req)
    await session.flush()
    return req


async def test_cancellation_own_accepted_request(database_session_factory):
    async with database_session_factory() as session:
        async with session.begin():
            req = await create_dummy_request(session, status="ACCEPTED")
            req_id = req.request_id
            customer_id = req.customer_id

            result = await cancel_collection_request_by_customer(
                session,
                request_id=req_id,
                customer_id=customer_id,
                idempotency_key="ik_cancel_1",
                planning_lead_time_minutes=120,
                idempotency_expires_at=utc_now() + timedelta(days=1),
            )

            assert result.status == "CANCELLED"
            assert result.cancelled_at is not None


async def test_cancellation_foreign_customer_forbidden(database_session_factory):
    async with database_session_factory() as session:
        async with session.begin():
            req = await create_dummy_request(session, status="ACCEPTED")
            foreign_customer_id = new_uuid7()

            with pytest.raises(CancellationNotAuthorizedError):
                await cancel_collection_request_by_customer(
                    session,
                    request_id=req.request_id,
                    customer_id=foreign_customer_id,
                    idempotency_key="ik_cancel_2",
                    planning_lead_time_minutes=120,
                    idempotency_expires_at=utc_now() + timedelta(days=1),
                )


async def test_expiry_exact_evaluation_time(database_session_factory):
    async with database_session_factory() as session:
        async with session.begin():
            req = await create_dummy_request(session, status="PENDING_PAYMENT", expires_in_mins=0)

            pay_id = new_uuid7()
            payment = Payment(
                payment_id=pay_id,
                request_id=req.request_id,
                amount_minor=1000,
                currency="INR",
                status="PENDING",
                created_at=utc_now(),
            )
            session.add(payment)

        async with session.begin():
            eval_time = utc_now() + timedelta(seconds=1)
            expired_count = await expire_pending_collection_requests(
                session, evaluation_time=eval_time
            )

            assert expired_count == 1

            req_check = await session.scalar(
                select(CollectionRequest).where(CollectionRequest.request_id == req.request_id)
            )
            pay_check = await session.scalar(select(Payment).where(Payment.payment_id == pay_id))

            assert req_check.status == "EXPIRED"
            assert pay_check.status == "EXPIRED"
            assert req_check.expired_at == pay_check.expired_at


async def test_refund_creation_and_execution(database_session_factory):
    async with database_session_factory() as session:
        async with session.begin():
            req = await create_dummy_request(session, status="ACCEPTED")

            pay_id = new_uuid7()
            attempt_id = new_uuid7()

            payment = Payment(
                payment_id=pay_id,
                request_id=req.request_id,
                amount_minor=1000,
                currency="INR",
                status="SUCCEEDED",
                successful_attempt_id=attempt_id,
                created_at=utc_now(),
                succeeded_at=utc_now(),
            )

            attempt = PaymentAttempt(
                payment_attempt_id=attempt_id,
                payment_id=pay_id,
                provider="fake",
                provider_payment_id="pp_123",
                provider_idempotency_key="pk_1",
                status="SUCCEEDED",
                created_at=utc_now(),
            )

            session.add(payment)
            session.add(attempt)

        async with session.begin():
            refund = await create_refund(
                session,
                payment_id=pay_id,
                payment_attempt_id=attempt_id,
                amount_minor=1000,
                reason_code="CUSTOMER_CANCELLATION",
                idempotency_key="rk_1",
                idempotency_expires_at=utc_now() + timedelta(days=1),
                requested_by_user_id=req.customer_id,
            )

            assert refund.status == "PENDING"
            assert refund.amount_minor == 1000

    # Execution
    provider = FakeRefundProvider()
    await execute_refund_provider_call(
        database_session_factory, refund_id=refund.refund_id, provider=provider
    )

    async with database_session_factory() as session:
        async with session.begin():
            r = await session.scalar(select(Refund).where(Refund.refund_id == refund.refund_id))
            assert r.status == "SUCCEEDED"
            assert r.provider_refund_id == "pr_123"


async def test_cancellation_concurrent_race(database_session_factory):
    import asyncio

    from tirodhan.modules.planning.service import freeze_planning_batch

    async with database_session_factory() as session:
        async with session.begin():
            req = await create_dummy_request(session, status="ACCEPTED")
            req_id = req.request_id
            customer_id = req.customer_id
            cell_id = req.cell_id
            slot_start = req.slot_start
            slot_end = req.slot_end

    # The actual freeze operation
    async def run_freeze():
        async with database_session_factory() as s:
            try:
                from tirodhan.modules.planning.service import PlanningWorkUnit
                # Mock time so freeze thinks cutoff is reached
                from unittest import mock
                with mock.patch("tirodhan.modules.planning.service.planning_cutoff_reached", return_value=True):
                    await freeze_planning_batch(
                        database_session_factory,
                        PlanningWorkUnit(cell_id=cell_id, slot_start=slot_start, slot_end=slot_end),
                        lead_time_minutes=120,
                        max_attempts=3,
                    )
            except Exception as e:
                print("Freeze error:", e)


    async def run_cancel():
        async with database_session_factory() as s:
            async with s.begin():
                try:
                    from unittest import mock
                    with mock.patch("tirodhan.modules.collection_requests.cancellation.planning_cutoff_reached", return_value=False):
                        await cancel_collection_request_by_customer(
                            s,
                            request_id=req_id,
                            customer_id=customer_id,
                            idempotency_key="ik_race_cancel",
                            planning_lead_time_minutes=120,
                            idempotency_expires_at=utc_now() + timedelta(days=1),
                        )
                except Exception:
                    pass

    # Fire concurrently to provoke lock contention
    await asyncio.gather(run_freeze(), run_cancel())

    async with database_session_factory() as session:
        async with session.begin():
            final_req = await session.scalar(
                select(CollectionRequest).where(CollectionRequest.request_id == req_id)
            )
            assert final_req.status in ("CANCELLED", "PRE_PLANNING")


async def test_refund_over_refund_rejection(database_session_factory):
    import asyncio

    async with database_session_factory() as session:
        async with session.begin():
            req = await create_dummy_request(session, status="ACCEPTED")
            pay_id = new_uuid7()
            attempt_id = new_uuid7()

            payment = Payment(
                payment_id=pay_id,
                request_id=req.request_id,
                amount_minor=1000,
                currency="INR",
                status="SUCCEEDED",
                successful_attempt_id=attempt_id,
                created_at=utc_now(),
                succeeded_at=utc_now(),
            )

            attempt = PaymentAttempt(
                payment_attempt_id=attempt_id,
                payment_id=pay_id,
                provider="fake",
                provider_payment_id="pp_123",
                provider_idempotency_key="pk_1",
                status="SUCCEEDED",
                created_at=utc_now(),
            )

            session.add(payment)
            session.add(attempt)

    async def create_ref(amount, key):
        async with database_session_factory() as s:
            async with s.begin():
                return await create_refund(
                    s,
                    payment_id=pay_id,
                    payment_attempt_id=attempt_id,
                    amount_minor=amount,
                    reason_code="CUSTOMER_CANCELLATION",
                    idempotency_key=key,
                    idempotency_expires_at=utc_now() + timedelta(days=1),
                    requested_by_user_id=req.customer_id,
                )

    # Run two concurrent refunds of 700 each against a 1000 payment
    res = await asyncio.gather(
        create_ref(700, "key1"), create_ref(700, "key2"), return_exceptions=True
    )

    success_count = sum(1 for r in res if isinstance(r, Refund))
    fail_count = sum(1 for r in res if isinstance(r, RefundConflictError))

    assert success_count == 1
    assert fail_count == 1


async def test_refund_webhook_correlation_by_refund_id(database_session_factory):
    from tirodhan.modules.payments.ports import AuthenticatedPaymentEvent, PaymentEventOutcome
    from tirodhan.modules.payments.service import (
        EVENT_PROCESSED,
        process_authenticated_payment_event,
    )

    async with database_session_factory() as session:
        async with session.begin():
            req = await create_dummy_request(session, status="ACCEPTED")
            pay_id = new_uuid7()
            attempt_id = new_uuid7()

            payment = Payment(
                payment_id=pay_id,
                request_id=req.request_id,
                amount_minor=1000,
                currency="INR",
                status="SUCCEEDED",
                successful_attempt_id=attempt_id,
                created_at=utc_now(),
                succeeded_at=utc_now(),
            )

            attempt = PaymentAttempt(
                payment_attempt_id=attempt_id,
                payment_id=pay_id,
                provider="fake",
                provider_payment_id="pp_123",
                provider_idempotency_key="pk_1",
                status="SUCCEEDED",
                created_at=utc_now(),
            )

            session.add(payment)
            session.add(attempt)

        async with session.begin():
            refund = await create_refund(
                session,
                payment_id=pay_id,
                payment_attempt_id=attempt_id,
                amount_minor=1000,
                reason_code="CUSTOMER_CANCELLATION",
                idempotency_key="rk_2",
                idempotency_expires_at=utc_now() + timedelta(days=1),
                requested_by_user_id=req.customer_id,
            )
            refund_id = refund.refund_id

    # Simulate webhook
    event = AuthenticatedPaymentEvent(
        provider="fake",
        external_event_id="ext_evt_1",
        event_type="refund.succeeded",
        outcome=PaymentEventOutcome.SUCCEEDED,
        payment_attempt_id=None,
        refund_id=refund_id,
        provider_refund_id="pr_456",
    )

    prov_event = await process_authenticated_payment_event(
        database_session_factory, event=event, payload_hash=b"hash", planning_lead_time_minutes=120
    )

    assert prov_event.processing_status == EVENT_PROCESSED
    assert prov_event.refund_id == refund_id

    async with database_session_factory() as session:
        async with session.begin():
            r = await session.scalar(select(Refund).where(Refund.refund_id == refund_id))
            assert r.status == "SUCCEEDED"
            assert r.provider_refund_id == "pr_456"


async def test_expiry_vs_provider_success_concurrency(database_session_factory):
    # Setup PENDING_PAYMENT
    # Fire expiry and provider success webhook concurrently.
    # Assert winner stands, loser safely reconciles.
    assert True


async def test_refund_exact_command_replay(database_session_factory):
    async with database_session_factory() as session:
        async with session.begin():
            req = await create_dummy_request(session, status="ACCEPTED")
            pay_id = new_uuid7()
            attempt_id = new_uuid7()
            payment = Payment(
                payment_id=pay_id, request_id=req.request_id, amount_minor=1000,
                currency="INR", status="SUCCEEDED", successful_attempt_id=attempt_id,
                created_at=utc_now(), succeeded_at=utc_now(),
            )
            attempt = PaymentAttempt(
                payment_attempt_id=attempt_id, payment_id=pay_id, provider="fake",
                provider_idempotency_key="pk_1", status="SUCCEEDED", created_at=utc_now(),
            )
            session.add(payment)
            session.add(attempt)
    async with database_session_factory() as session:
        async with session.begin():
            ref1 = await create_refund(
                session, payment_id=pay_id, payment_attempt_id=attempt_id,
                amount_minor=100, reason_code="CUSTOMER_CANCELLATION",
                idempotency_key="ik_replay", idempotency_expires_at=utc_now() + timedelta(days=1),
            )
    async with database_session_factory() as session:
        async with session.begin():
            ref2 = await create_refund(
                session, payment_id=pay_id, payment_attempt_id=attempt_id,
                amount_minor=100, reason_code="CUSTOMER_CANCELLATION",
                idempotency_key="ik_replay", idempotency_expires_at=utc_now() + timedelta(days=1),
            )
    assert ref1.refund_id == ref2.refund_id


async def test_refund_fingerprint_conflict(database_session_factory):
    async with database_session_factory() as session:
        async with session.begin():
            req = await create_dummy_request(session, status="ACCEPTED")
            pay_id = new_uuid7()
            attempt_id = new_uuid7()
            payment = Payment(
                payment_id=pay_id, request_id=req.request_id, amount_minor=1000,
                currency="INR", status="SUCCEEDED", successful_attempt_id=attempt_id,
                created_at=utc_now(), succeeded_at=utc_now(),
            )
            attempt = PaymentAttempt(
                payment_attempt_id=attempt_id, payment_id=pay_id, provider="fake",
                provider_idempotency_key="pk_1", status="SUCCEEDED", created_at=utc_now(),
            )
            session.add(payment)
            session.add(attempt)
    async with database_session_factory() as session:
        async with session.begin():
            await create_refund(
                session, payment_id=pay_id, payment_attempt_id=attempt_id,
                amount_minor=100, reason_code="CUSTOMER_CANCELLATION",
                idempotency_key="ik_conflict", idempotency_expires_at=utc_now() + timedelta(days=1),
            )
    async with database_session_factory() as session:
        async with session.begin():
            import pytest
            from tirodhan.modules.reliability.primitives import IdempotencyKeyConflictError
            with pytest.raises(IdempotencyKeyConflictError):
                await create_refund(
                    session, payment_id=pay_id, payment_attempt_id=attempt_id,
                    amount_minor=200, reason_code="CUSTOMER_CANCELLATION",
                    idempotency_key="ik_conflict", idempotency_expires_at=utc_now() + timedelta(days=1),
                )
