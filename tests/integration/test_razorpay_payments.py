from __future__ import annotations

import asyncio
from datetime import timedelta

import httpx
import pytest
from razorpay_helpers import Orders, booking, initiate, payment_body, process, settings, signed
from sqlalchemy import func, select
from test_collection_payment import freeze_work_unit

from tirodhan.api.dependencies import get_current_customer_id
from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.main import create_app
from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.payments import service
from tirodhan.modules.payments.models import Payment, PaymentAttempt, PaymentProviderEvent
from tirodhan.modules.payments.razorpay import RazorpayProvider
from tirodhan.modules.reliability.models import OutboxEvent

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def acceptance_count(factory):
    async with factory() as session:
        return await session.scalar(
            select(func.count())
            .select_from(OutboxEvent)
            .where(OutboxEvent.event_type == "CollectionRequestAccepted")
        )


async def test_payment_command_replay_and_http_io_have_no_checked_out_connection(
    database_session_factory, database_engine
):
    factory = database_session_factory
    user, result = await booking(factory)
    orders = Orders()

    async def transport(request):
        assert database_engine.pool.checkedout() == 0
        async with factory() as session:
            attempt = await session.scalar(select(PaymentAttempt))
            assert attempt is not None and attempt.status == "CREATED"
        return orders(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        provider = RazorpayProvider(settings(), client=client)
        first = await initiate(factory, user, result, provider)
        second = await initiate(factory, user, result, provider)
    assert first.payment_attempt_id == second.payment_attempt_id and second.status == "PENDING"
    assert len(orders.posts) == 1


async def test_timeout_after_remote_order_commit_recovery_and_concurrent_replay_converge(
    database_session_factory,
):
    factory = database_session_factory
    user, result = await booking(factory)
    orders = Orders()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def transport(request):
        if request.method == "POST":
            response = orders(request)
            entered.set()
            await release.wait()
            if response.status_code == 200:
                raise httpx.ReadTimeout("lost", request=request)
            return response
        return orders(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        provider = RazorpayProvider(settings(), client=client)
        first = asyncio.create_task(initiate(factory, user, result, provider))
        await asyncio.wait_for(entered.wait(), 5)
        second = await initiate(factory, user, result, provider)
        release.set()
        first = await first
        assert first.payment_attempt_id == second.payment_attempt_id
        assert first.provider_order_id == second.provider_order_id
    assert len(orders.posts) == 1
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(PaymentAttempt)) == 1


async def test_concurrent_initial_lookups_duplicate_receipt_recovery(database_session_factory):
    factory = database_session_factory
    user, result = await booking(factory)
    orders = Orders()
    reads = 0
    both = asyncio.Event()

    async def transport(request):
        nonlocal reads
        if request.method == "GET" and reads < 2:
            reads += 1
            if reads == 2:
                both.set()
            await asyncio.wait_for(both.wait(), 5)
            return httpx.Response(200, json={"entity": "collection", "count": 0, "items": []})
        return orders(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        provider = RazorpayProvider(settings(), client=client)
        a, b = await asyncio.gather(
            initiate(factory, user, result, provider), initiate(factory, user, result, provider)
        )
    assert (
        a.payment_attempt_id == b.payment_attempt_id and a.provider_order_id == b.provider_order_id
    )
    assert len(orders.orders) == 1 and len(orders.posts) == 2
    assert orders.posts[0] == orders.posts[1]


async def test_uncertain_attempt_replay_recovers_same_receipt(database_session_factory):
    factory = database_session_factory
    user, result = await booking(factory)
    orders = Orders()
    failing = True

    def transport(request):
        if request.method == "POST" and failing:
            raise httpx.ReadTimeout("lost", request=request)
        return orders(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        provider = RazorpayProvider(settings(), client=client)
        first = await initiate(factory, user, result, provider)
        assert first.status == "INITIATION_UNCERTAIN"
        failing = False
        second = await initiate(factory, user, result, provider)
    assert first.payment_attempt_id == second.payment_attempt_id and second.status == "PENDING"
    assert orders.posts[0]["receipt"] == f"pa_{first.payment_attempt_id.hex}"


async def test_captured_external_mapping_duplicate_and_adversarial_state_ordering(
    database_session_factory,
):
    factory = database_session_factory
    user, result = await booking(factory)
    async with httpx.AsyncClient(transport=httpx.MockTransport(Orders())) as client:
        provider = RazorpayProvider(settings(), client=client)
        attempt = await initiate(factory, user, result, provider)
        failed = await process(
            factory,
            provider,
            payment_body(attempt, payment_id="pay_failed", event_type="payment.failed"),
            "evt_failed",
        )
        assert failed.processing_status == "PROCESSED"
        body = payment_body(attempt, notes={"tirodhan_payment_attempt_id": str(new_uuid7())})
        successes = await asyncio.gather(
            *(process(factory, provider, body, "evt_captured") for _ in range(5))
        )
        assert len({e.payment_provider_event_id for e in successes}) == 1
        assert successes[0].processing_status == "PROCESSED"
        await process(
            factory,
            provider,
            payment_body(attempt, payment_id="pay_stale", event_type="payment.failed"),
            "evt_stale",
        )
        await freeze_work_unit(factory, result.request.request_id)
        replay = await process(factory, provider, body, "evt_again")
        assert replay.processing_status == "PROCESSED" and replay.failure_code is None
        extra = await process(
            factory, provider, payment_body(attempt, payment_id="pay_additional"), "evt_additional"
        )
        assert (
            extra.processing_status == "PROCESSED"
            and extra.failure_code == "ADDITIONAL_CAPTURE_COMPENSATED"
        )
    async with factory() as session:
        durable = await session.get(PaymentAttempt, attempt.payment_attempt_id)
        payment = await session.get(Payment, result.payment.payment_id)
        request = await session.get(CollectionRequest, result.request.request_id)
        assert durable.status == "SUCCEEDED" and durable.provider_payment_id == "pay_success"
        assert (
            payment.status == "SUCCEEDED"
            and payment.successful_attempt_id == attempt.payment_attempt_id
        )
        assert request.status == "PRE_PLANNING"
        assert await session.scalar(select(func.count()).select_from(PaymentProviderEvent)) == 5
    assert await acceptance_count(factory) == 1


@pytest.mark.parametrize(
    "case,code",
    [
        ("amount", "AMOUNT_MISMATCH"),
        ("currency", "CURRENCY_MISMATCH"),
        ("unknown", "ATTEMPT_NOT_MAPPED"),
        ("authorized", "EVENT_IGNORED"),
        ("expired", "PAYMENT_WINDOW_EXPIRED"),
        ("cutoff", "PLANNING_CUTOFF_REACHED"),
        ("frozen", "WORK_UNIT_FROZEN"),
    ],
)
async def test_captured_validation_and_existing_payment_gates(database_session_factory, case, code):
    factory = database_session_factory
    user, result = await booking(factory)
    async with httpx.AsyncClient(transport=httpx.MockTransport(Orders())) as client:
        provider = RazorpayProvider(settings(), client=client)
        attempt = await initiate(factory, user, result, provider)
        extra = {}
        event_type = "payment.captured"
        if case == "amount":
            extra["amount"] = 1
        if case == "currency":
            extra["currency"] = "USD"
        if case == "unknown":
            extra["order_id"] = "order_unknown"
        if case == "authorized":
            event_type = "payment.authorized"
        if case in {"expired", "cutoff"}:
            async with factory() as session, session.begin():
                request = await session.get(CollectionRequest, result.request.request_id)
                if case == "expired":
                    request.created_at = utc_now() - timedelta(hours=1)
                    request.payment_expires_at = utc_now() - timedelta(seconds=1)
                else:
                    request.slot_start = utc_now() + timedelta(minutes=10)
                    request.slot_end = request.slot_start + timedelta(minutes=30)
        if case == "frozen":
            await freeze_work_unit(factory, result.request.request_id)
        event = await process(
            factory, provider, payment_body(attempt, event_type=event_type, **extra)
        )
        assert event.failure_code == code
    async with factory() as session:
        assert (await session.get(Payment, result.payment.payment_id)).status == (
            "SUCCEEDED" if case in {"expired", "cutoff", "frozen"} else "PENDING"
        )
        assert (await session.get(CollectionRequest, result.request.request_id)).status == (
            "EXPIRED" if case in {"expired", "cutoff", "frozen"} else "PENDING_PAYMENT"
        )
    assert await acceptance_count(factory) == 0


async def test_conflicting_order_payment_mapping_and_distinct_successful_attempts(
    database_session_factory,
):
    factory = database_session_factory
    user, result = await booking(factory)
    async with httpx.AsyncClient(transport=httpx.MockTransport(Orders())) as client:
        provider = RazorpayProvider(settings(), client=client)
        a = await initiate(factory, user, result, provider, "a")
        async with factory() as session, session.begin():
            (await session.get(PaymentAttempt, a.payment_attempt_id)).status = "FAILED"
        b = await initiate(factory, user, result, provider, "b")
        async with factory() as session, session.begin():
            (await session.get(PaymentAttempt, b.payment_attempt_id)).provider_payment_id = "pay_b"
        conflict = await process(
            factory, provider, payment_body(a, payment_id="pay_b"), "evt_conflict"
        )
        assert conflict.failure_code == "PAYMENT_IDENTITY_CONFLICT"
        events = await asyncio.gather(
            process(factory, provider, payment_body(a, payment_id="pay_a"), "evt_a"),
            process(factory, provider, payment_body(b, payment_id="pay_b"), "evt_b"),
        )
        assert sorted(e.processing_status for e in events) == [
            "PROCESSED",
            "PROCESSED",
        ]
        assert (
            next(e for e in events if e.failure_code is not None).failure_code
            == "ADDITIONAL_CAPTURE_COMPENSATED"
        )
    assert await acceptance_count(factory) == 1


async def test_real_adapter_acceptance_rollback_is_atomic(database_session_factory, monkeypatch):
    factory = database_session_factory
    user, result = await booking(factory)
    async with httpx.AsyncClient(transport=httpx.MockTransport(Orders())) as client:
        provider = RazorpayProvider(settings(), client=client)
        attempt = await initiate(factory, user, result, provider)

        async def fail(*args, **kwargs):
            raise RuntimeError("rollback")

        monkeypatch.setattr(service, "append_outbox_event", fail)
        with pytest.raises(RuntimeError, match="rollback"):
            await process(factory, provider, payment_body(attempt))
    async with factory() as session:
        assert (await session.get(Payment, result.payment.payment_id)).status == "PENDING"
        assert (await session.get(PaymentAttempt, attempt.payment_attempt_id)).status == "PENDING"
        assert await session.scalar(select(func.count()).select_from(PaymentProviderEvent)) == 0
    assert await acceptance_count(factory) == 0


async def test_app_lifespan_real_provider_webhook_auth_and_customer_attempt(
    database_session_factory, migrated_database_url, caplog
):
    factory = database_session_factory
    user, result = await booking(factory)
    async with httpx.AsyncClient(transport=httpx.MockTransport(Orders())) as remote:
        app = create_app(
            settings(
                database_url=migrated_database_url,
                command_idempotency_ttl_seconds=3600,
                planning_lead_time_minutes=30,
            ),
            payment_http_client=remote,
        )
        app.dependency_overrides[get_current_customer_id] = lambda: user.user_id
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as api,
        ):
            assert isinstance(app.state.payment_provider, RazorpayProvider)
            response = await api.post(
                f"/v1/payments/collection-requests/{result.request.request_id}/attempts",
                headers={"Idempotency-Key": "app"},
            )
            assert response.status_code == 201
            async with factory() as session:
                attempt = await session.scalar(select(PaymentAttempt))
            body = payment_body(attempt, private_marker="must-not-persist-or-log")
            assert (
                await api.post(
                    "/v1/payments/provider/webhook",
                    content=body,
                    headers={"x-razorpay-signature": "0" * 64},
                )
            ).status_code == 401
            assert (
                await api.post(
                    "/v1/payments/provider/webhook", content=body, headers=signed(body, event_id="")
                )
            ).status_code == 422
            response = await api.post(
                "/v1/payments/provider/webhook", content=body, headers=signed(body)
            )
            assert (
                response.status_code == 200 and response.json()["processing_status"] == "PROCESSED"
            )
        assert not remote.is_closed
    async with factory() as session:
        event = await session.scalar(select(PaymentProviderEvent))
        assert event.payload_hash is not None and event.external_event_id == "evt_test"
        assert "must-not-persist-or-log" not in repr(event.__dict__)
        assert await session.scalar(select(func.count()).select_from(PaymentProviderEvent)) == 1
    assert "must-not-persist-or-log" not in caplog.text and "test-secret" not in caplog.text


async def test_provider_payment_reference_alone_correlates_canonical_replay(
    database_session_factory,
):
    factory = database_session_factory
    user, result = await booking(factory)
    async with httpx.AsyncClient(transport=httpx.MockTransport(Orders())) as client:
        provider = RazorpayProvider(settings(), client=client)
        attempt = await initiate(factory, user, result, provider)
        await process(factory, provider, payment_body(attempt), "evt_order")
        record = await process(
            factory, provider, payment_body(attempt, order_id=None), "evt_payment_only"
        )
        assert (
            record.processing_status == "PROCESSED"
            and record.payment_attempt_id == attempt.payment_attempt_id
        )
    assert await acceptance_count(factory) == 1


async def test_payment_configuration_error_retains_durable_attempt_for_retry(
    database_session_factory,
):
    factory = database_session_factory
    user, result = await booking(factory)
    invalid = True
    orders = Orders()

    def transport(request):
        if invalid:
            return httpx.Response(401, json={"error": {}})
        return orders(request)

    from tirodhan.modules.payments.ports import PaymentProviderNotConfiguredError

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        provider = RazorpayProvider(settings(), client=client)
        with pytest.raises(PaymentProviderNotConfiguredError):
            await initiate(factory, user, result, provider)
        async with factory() as session:
            attempt = await session.scalar(select(PaymentAttempt))
            assert attempt.status == "CREATED"
        invalid = False
        replay = await initiate(factory, user, result, provider)
        assert (
            replay.payment_attempt_id == attempt.payment_attempt_id and replay.status == "PENDING"
        )


async def test_application_owned_client_closes_injected_provider_not_closed(
    migrated_database_url, monkeypatch
):
    from tirodhan.modules.payments import runtime
    from tirodhan.modules.payments.ports import UnconfiguredPaymentProvider

    clients = []
    real_client = httpx.AsyncClient

    def client_factory(**kwargs):
        client = real_client(
            transport=httpx.MockTransport(lambda request: pytest.fail("no startup network"))
        )
        clients.append(client)
        return client

    monkeypatch.setattr(runtime.httpx, "AsyncClient", client_factory)
    app = create_app(settings(database_url=migrated_database_url))
    async with app.router.lifespan_context(app):
        assert isinstance(app.state.payment_provider, RazorpayProvider)
        assert not clients[0].is_closed
    assert clients[0].is_closed
    injected = UnconfiguredPaymentProvider()
    app = create_app(settings(database_url=migrated_database_url), payment_provider=injected)
    async with app.router.lifespan_context(app):
        assert app.state.payment_provider is injected
    assert len(clients) == 1
