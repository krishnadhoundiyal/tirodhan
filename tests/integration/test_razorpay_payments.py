from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import timedelta

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select
from test_collection_payment import create_context, create_request, create_user

from tirodhan.api.dependencies import get_current_customer_id
from tirodhan.core.config import Settings
from tirodhan.db.values import utc_now
from tirodhan.main import create_app
from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.payments.models import Payment, PaymentAttempt, PaymentProviderEvent
from tirodhan.modules.payments.razorpay import RazorpayProvider
from tirodhan.modules.payments.service import (
    InitiatePaymentAttemptCommand,
    initiate_payment_attempt,
    process_authenticated_payment_event,
)
from tirodhan.modules.reliability.models import OutboxEvent

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


class ProviderHarness:
    def __init__(self):
        self.calls = []
        self.mode = "ready"
        self.on_send = None
        self.settings = Settings(
            _env_file=None,
            environment="test",
            razorpay_key_id="test-key",
            razorpay_key_secret="merchant-test-secret",
            razorpay_webhook_secret="webhook-test-secret",
            razorpay_http_timeout_seconds=5,
            planning_lead_time_minutes=30,
            command_idempotency_ttl_seconds=3600,
        )
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self.send))
        self.provider = RazorpayProvider(self.settings, client=self.client)

    async def send(self, request):
        self.calls.append(request)
        if self.on_send:
            await self.on_send(request)
        if self.mode == "timeout":
            raise httpx.ReadTimeout("sensitive provider response", request=request)
        if self.mode == "cancel":
            raise asyncio.CancelledError
        if self.mode == "failed":
            return httpx.Response(
                400,
                json={
                    "error": {
                        "code": "BAD_REQUEST_ERROR",
                        "reason": "input_validation_failed",
                        "field": "amount",
                    }
                },
            )
        data = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "entity": "order",
                "id": "order_" + data["notes"]["tirodhan_payment_attempt_id"].replace("-", ""),
                **data,
            },
        )

    def headers(self, body, identity="event_Test"):
        return {
            "x-razorpay-signature": hmac.new(
                b"webhook-test-secret", body, hashlib.sha256
            ).hexdigest(),
            "x-razorpay-event-id": identity,
        }

    def body(self, attempt, event="payment.captured", **fields):
        entity = {
            "entity": "payment",
            "id": "pay_" + attempt.payment_attempt_id.hex,
            "order_id": attempt.provider_order_id,
            "amount": 500,
            "currency": "INR",
            "status": "captured",
            "captured": True,
            "notes": {},
        }
        entity.update(fields)
        payload = {"payment": {"entity": entity}}
        if event == "order.paid":
            payload["order"] = {
                "entity": {
                    "entity": "order",
                    "id": attempt.provider_order_id,
                    "amount": 500,
                    "currency": "INR",
                    "status": "paid",
                    "notes": {},
                }
            }
        return json.dumps({"entity": "event", "event": event, "payload": payload}).encode()

    async def process(self, factory, body, identity="event_Test"):
        event = await self.provider.authenticate_webhook(
            raw_body=body, headers=self.headers(body, identity)
        )
        return await process_authenticated_payment_event(
            factory,
            event,
            payload_hash=hashlib.sha256(body).digest(),
            planning_lead_time_minutes=30,
        )


@pytest_asyncio.fixture
async def harness():
    value = ProviderHarness()
    try:
        yield value
    finally:
        await value.client.aclose()


async def booking(factory):
    user = await create_user(factory)
    context = await create_context(factory, user.user_id)
    result = await create_request(factory, user.user_id, context.serviceability_context_id)
    return user, result


async def attempt_for(factory, harness, user, result, key="attempt"):
    return await initiate_payment_attempt(
        factory,
        InitiatePaymentAttemptCommand(user.user_id, result.request.request_id, key),
        harness.provider,
        idempotency_expires_at=utc_now() + timedelta(hours=1),
    )


@pytest.mark.parametrize("event_name", ["payment.captured", "order.paid"])
async def test_real_adapter_webhook_atomic_success_duplicates_and_later_failure(
    database_session_factory, harness, event_name
):
    factory = database_session_factory
    user, result = await booking(factory)
    attempt = await attempt_for(factory, harness, user, result)
    body = harness.body(attempt, event_name)
    first = await harness.process(factory, body)
    duplicate = await harness.process(factory, body)
    second_event = await harness.process(factory, body, "event_Second")
    failed = await harness.process(
        factory,
        harness.body(attempt, "payment.failed", status="failed", captured=False),
        "event_Failed",
    )
    assert first.payment_provider_event_id == duplicate.payment_provider_event_id
    assert (
        first.processing_status
        == second_event.processing_status
        == failed.processing_status
        == "PROCESSED"
    )
    async with factory() as session:
        payment = await session.get(Payment, result.payment.payment_id)
        request = await session.get(CollectionRequest, result.request.request_id)
        durable = await session.get(PaymentAttempt, attempt.payment_attempt_id)
        events = list(await session.scalars(select(PaymentProviderEvent)))
        outbox = list(
            await session.scalars(
                select(OutboxEvent).where(OutboxEvent.event_type == "CollectionRequestAccepted")
            )
        )
    assert (
        payment.status == "SUCCEEDED"
        and payment.successful_attempt_id == attempt.payment_attempt_id
    )
    assert request.status == "ACCEPTED" and request.accepted_at is not None
    assert durable.status == "SUCCEEDED"
    assert len(events) == 3 and len(outbox) == 1
    assert outbox[0].payload == {"request_id": str(request.request_id)}
    assert set(vars(events[0])) <= {
        "_sa_instance_state",
        "payment_provider_event_id",
        "provider",
        "external_event_id",
        "event_type",
        "payment_attempt_id",
        "refund_id",
        "payload_hash",
        "processing_status",
        "received_at",
        "processed_at",
        "failure_code",
    }


@pytest.mark.parametrize(
    "mismatch,expected",
    [
        ("order", "ORDER_ID_MISMATCH"),
        ("amount", "PAYMENT_AMOUNT_MISMATCH"),
        ("currency", "PAYMENT_CURRENCY_MISMATCH"),
        ("identity", "ATTEMPT_IDENTITY_CONFLICT"),
        ("authorized", "INVALID_PAYMENT_FACTS"),
    ],
)
async def test_authenticated_conflicting_facts_never_accept(
    database_session_factory, harness, mismatch, expected
):
    factory = database_session_factory
    user, result = await booking(factory)
    attempt = await attempt_for(factory, harness, user, result)
    fields = {"notes": {"tirodhan_payment_attempt_id": str(attempt.payment_attempt_id)}}
    if mismatch == "order":
        fields["order_id"] = "order_Other"
    elif mismatch == "amount":
        fields["amount"] = 501
    elif mismatch == "currency":
        fields["currency"] = "USD"
    elif mismatch == "identity":
        other = await attempt_for(factory, harness, user, result, "other")
        fields["notes"] = {"tirodhan_payment_attempt_id": str(other.payment_attempt_id)}
    else:
        fields.update(status="authorized", captured=False)
    event = await harness.process(factory, harness.body(attempt, **fields))
    assert event.processing_status == "RECONCILIATION_REQUIRED" and event.failure_code == expected
    async with factory() as session:
        assert (await session.get(Payment, result.payment.payment_id)).status == "PENDING"
        assert (
            await session.get(CollectionRequest, result.request.request_id)
        ).status == "PENDING_PAYMENT"
        assert (
            await session.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.event_type == "CollectionRequestAccepted")
            )
            == 0
        )


async def test_additional_success_and_concurrent_success_keep_one_canonical_attempt(
    database_session_factory, harness
):
    factory = database_session_factory
    user, result = await booking(factory)
    attempts = [await attempt_for(factory, harness, user, result, key) for key in ["one", "two"]]
    events = await asyncio.gather(
        *(
            harness.process(factory, harness.body(attempt), f"event_{index}")
            for index, attempt in enumerate(attempts)
        )
    )
    assert sorted(event.processing_status for event in events) == [
        "PROCESSED",
        "RECONCILIATION_REQUIRED",
    ]
    assert [event.failure_code for event in events if event.processing_status != "PROCESSED"] == [
        "ADDITIONAL_SUCCESS"
    ]
    async with factory() as session:
        assert {row.status for row in await session.scalars(select(PaymentAttempt))} == {
            "SUCCEEDED"
        }
        assert (
            await session.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.event_type == "CollectionRequestAccepted")
            )
            == 1
        )


@pytest.mark.parametrize(
    "mode,expected",
    [("timeout", "INITIATION_UNCERTAIN"), ("failed", "FAILED"), ("cancel", "INITIATION_UNCERTAIN")],
)
async def test_uncertain_or_interrupted_order_does_not_blindly_post_again(
    database_session_factory, database_engine, harness, mode, expected
):
    factory = database_session_factory
    user, result = await booking(factory)
    harness.mode = mode

    async def outside_transaction(request):
        assert database_engine.pool.checkedout() == 0
        async with factory() as session:
            assert await session.scalar(select(PaymentAttempt.payment_attempt_id)) is not None

    harness.on_send = outside_transaction
    if mode == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await attempt_for(factory, harness, user, result)
    else:
        first = await attempt_for(factory, harness, user, result)
        assert first.status == expected
    replay = await attempt_for(factory, harness, user, result)
    assert replay.status == expected and len(harness.calls) == 1
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(PaymentAttempt)) == 1


async def test_concurrent_order_replay_invokes_provider_once(
    database_session_factory, database_engine, harness
):
    factory = database_session_factory
    user, result = await booking(factory)
    entered, release = asyncio.Event(), asyncio.Event()

    async def block(request):
        assert database_engine.pool.checkedout() == 0
        entered.set()
        await release.wait()

    harness.on_send = block
    task = asyncio.create_task(attempt_for(factory, harness, user, result))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        replay = await attempt_for(factory, harness, user, result)
        assert replay.status == "INITIATION_UNCERTAIN"
    finally:
        release.set()
    first = await asyncio.wait_for(task, 5)
    assert first.status == "PENDING" and first.payment_attempt_id == replay.payment_attempt_id
    assert len(harness.calls) == 1


@pytest.mark.parametrize("event", ["payment.authorized", "unhandled.event"])
async def test_authenticated_unknown_events_are_acknowledged_and_deduplicated(
    database_session_factory, harness, event
):
    factory = database_session_factory
    user, result = await booking(factory)
    attempt = await attempt_for(factory, harness, user, result)
    body = harness.body(attempt, event)
    first = await harness.process(factory, body)
    replay = await harness.process(factory, body)
    assert first.processing_status == "UNMATCHED"
    assert first.payment_provider_event_id == replay.payment_provider_event_id
    async with factory() as session:
        assert (await session.get(PaymentAttempt, attempt.payment_attempt_id)).status == "PENDING"


async def test_checkout_api_ownership_stored_order_signature_and_no_business_success(
    database_session_factory, migrated_database_url, harness
):
    factory = database_session_factory
    user, result = await booking(factory)
    attempt = await attempt_for(factory, harness, user, result)
    settings = harness.settings.model_copy(
        update={
            "database_url": Settings(
                _env_file=None, database_url=migrated_database_url
            ).database_url
        }
    )
    app = create_app(settings, payment_provider=harness.provider)
    app.dependency_overrides[get_current_customer_id] = lambda: user.user_id
    url = f"/v1/payments/attempts/{attempt.payment_attempt_id}/checkout-confirmation"
    payment_id = "pay_" + attempt.payment_attempt_id.hex
    signature = hmac.new(
        b"merchant-test-secret",
        f"{attempt.provider_order_id}|{payment_id}".encode(),
        hashlib.sha256,
    ).hexdigest()
    body = {
        "razorpay_order_id": attempt.provider_order_id,
        "razorpay_payment_id": payment_id,
        "razorpay_signature": signature,
    }
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            for invalid in [
                {**body, "razorpay_order_id": "order_Other"},
                {**body, "razorpay_payment_id": "pay_Other"},
                {**body, "razorpay_signature": "0" * 64},
            ]:
                assert (await client.post(url, json=invalid)).status_code == 409
            foreign = await create_user(factory)
            app.dependency_overrides[get_current_customer_id] = lambda: foreign.user_id
            assert (await client.post(url, json=body)).status_code == 409
            app.dependency_overrides[get_current_customer_id] = lambda: user.user_id
            first = await client.post(url, json=body)
            replay = await client.post(url, json=body)
    assert first.status_code == replay.status_code == 200 and first.json() == replay.json()
    assert first.json()["status"] == "PENDING"
    assert signature not in first.text and "merchant-test-secret" not in first.text
    async with factory() as session:
        assert (await session.get(Payment, result.payment.payment_id)).status == "PENDING"
        assert (
            await session.get(CollectionRequest, result.request.request_id)
        ).status == "PENDING_PAYMENT"
        assert (
            await session.get(PaymentAttempt, attempt.payment_attempt_id)
        ).provider_payment_id == payment_id


async def test_app_real_runtime_webhook_hmac_failure_and_exact_body_no_pii_persistence(
    database_session_factory, migrated_database_url, harness, caplog
):
    factory = database_session_factory
    user, result = await booking(factory)
    attempt = await attempt_for(factory, harness, user, result)
    settings = harness.settings.model_copy(
        update={
            "database_url": Settings(
                _env_file=None, database_url=migrated_database_url
            ).database_url
        }
    )
    app = create_app(settings, payment_http_client=harness.client)
    body = harness.body(attempt)
    async with app.router.lifespan_context(app):
        assert isinstance(app.state.payment_provider, RazorpayProvider)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            url = "/v1/payments/provider/webhook"
            for changed, headers in [
                (body, {}),
                (body, {"x-razorpay-signature": "0" * 64}),
                (body + b" ", harness.headers(body)),
            ]:
                assert (await client.post(url, content=changed, headers=headers)).status_code == 401
            async with factory() as session:
                assert (
                    await session.scalar(select(func.count()).select_from(PaymentProviderEvent))
                    == 0
                )
                assert (await session.get(Payment, result.payment.payment_id)).status == "PENDING"
            first = await client.post(url, content=body, headers=harness.headers(body))
            replay = await client.post(url, content=body, headers=harness.headers(body))
    assert first.status_code == replay.status_code == 200 and first.json() == replay.json()
    assert not harness.client.is_closed
    assert body.decode() not in caplog.text
    assert "merchant-test-secret" not in caplog.text and "webhook-test-secret" not in caplog.text


async def test_app_owned_client_lifecycle_makes_no_startup_network_calls(
    migrated_database_url, monkeypatch, harness
):
    owned = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: pytest.fail("no startup provider network call"))
    )
    monkeypatch.setattr(
        "tirodhan.modules.payments.runtime.httpx.AsyncClient", lambda **kwargs: owned
    )
    settings = harness.settings.model_copy(
        update={
            "database_url": Settings(
                _env_file=None, database_url=migrated_database_url
            ).database_url,
        }
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        assert isinstance(app.state.payment_provider, RazorpayProvider)
        assert not owned.is_closed
    assert owned.is_closed
