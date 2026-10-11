"""Order-level retry, terminal boundaries and independent replacement accounting."""

import asyncio
import json
from dataclasses import replace
from datetime import timedelta

import httpx
import pytest
from alembic import command as migration_command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from razorpay_helpers import booking, initiate, payment_body, process, settings
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from test_collection_payment import create_context, create_request, freeze_work_unit
from test_customer_financial_cancellation import cancel
from test_financial_reconciliation import POLICY, Gateway

from tirodhan.api.dependencies import get_current_customer_id
from tirodhan.db.base import Base
from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.main import create_app
from tirodhan.modules.collection_requests.models import CollectionRequest, CollectionRequestItem
from tirodhan.modules.collection_requests.ports import (
    DeclaredRequestItem,
    PricingQuote,
    QuotedRequestItem,
)
from tirodhan.modules.collection_requests.scheduling import SlotConflictError
from tirodhan.modules.customer_reads.projections import payment_projection
from tirodhan.modules.payments.models import (
    CapturedCharge,
    FinancialException,
    Payment,
    PaymentAttempt,
    PaymentProviderEvent,
    Refund,
    RefundObligation,
)
from tirodhan.modules.payments.razorpay import RazorpayProvider
from tirodhan.modules.payments.reconciliation import inquire_target, reconcile_batch
from tirodhan.modules.payments.refunds import execute_refund_provider_call
from tirodhan.modules.payments.service import (
    InitiatePaymentAttemptCommand,
    initiate_payment_attempt,
)
from tirodhan.modules.planning.policy import PlanningConfigurationError
from tirodhan.modules.reliability.models import OutboxEvent

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def api_for(factory, user, provider, database_url):
    config = settings(
        database_url=database_url,
        planning_lead_time_minutes=30,
        command_idempotency_ttl_seconds=86400,
        razorpay_merchant_display_name="Tirodhan",
    )
    app = create_app(config, payment_provider=provider)
    app.state.database_session_factory = factory
    app.dependency_overrides[get_current_customer_id] = lambda: user.user_id
    return app


async def project(factory, result, provider, *, blocked=False):
    async with factory() as session:
        request = await session.get(CollectionRequest, result.request.request_id)
        payment = await session.get(Payment, result.payment.payment_id)
        attempts = list(
            await session.scalars(
                select(PaymentAttempt)
                .where(PaymentAttempt.payment_id == payment.payment_id)
                .order_by(PaymentAttempt.created_at, PaymentAttempt.payment_attempt_id)
            )
        )
        return payment_projection(
            request,
            payment,
            attempts,
            now=utc_now(),
            retry_blocked=blocked,
            account_key=provider.account_key,
        )


async def invalidate(factory, attempt, *, failed=False):
    async with factory() as session, session.begin():
        row = await session.get(PaymentAttempt, attempt.payment_attempt_id)
        row.checkout_ready_until = None
        if failed:
            row.status = "FAILED"
            row.next_check_at = None


def instrument(attempt, state, identity="pay_instrument", amount=500):
    return {
        "entity": "payment",
        "id": identity,
        "order_id": attempt.provider_order_id,
        "amount": amount,
        "currency": "INR",
        "status": state,
        "captured": state == "captured",
    }


@pytest.mark.parametrize("state", ["failed", "authorized", "created", "captured"])
async def test_same_order_preparation_action_and_instrument_evidence(
    database_session_factory, database_engine, migrated_database_url, state
):
    factory = database_session_factory
    user, result = await booking(factory)
    gateway = Gateway(database_engine)
    gateway.verify_no_connections = True
    async with httpx.AsyncClient(transport=httpx.MockTransport(gateway)) as remote:
        provider = RazorpayProvider(settings(), client=remote)
        initial = await project(factory, result, provider)
        assert initial.action == "START" and initial.retry_allowed
        first = await initiate(factory, user, result, provider)
        assert first.checkout_ready_until is not None
        gateway.payments = [instrument(first, state)]
        await invalidate(factory, first)
        app = api_for(factory, user, provider, migrated_database_url)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as api:
            path = f"/v1/payments/collection-requests/{result.request.request_id}/attempts"
            response = await api.post(path, headers={"Idempotency-Key": "fresh-key"})
            assert response.status_code == 201, response.text
            value = response.json()
            assert value["payment_attempt_id"] == str(first.payment_attempt_id)
            assert value["provider_order_id"] == first.provider_order_id
            expected = (
                "RESUME" if state == "failed" else "UNAVAILABLE" if state == "captured" else "WAIT"
            )
            assert value["action"] == expected
            before = len(gateway.calls)
            replay = await api.post(path, headers={"Idempotency-Key": "fresh-key"})
            assert replay.json()["action"] == expected
            if state in {"failed", "captured"}:
                assert len(gateway.calls) == before
            handoff = await api.get(
                f"/v1/customer/payment-attempts/{first.payment_attempt_id}/checkout"
            )
            assert handoff.status_code == (200 if state == "failed" else 409)
            assert "test-secret" not in response.text + handoff.text
            assert provider.account_key not in response.text + handoff.text
        async with factory() as session:
            current = await session.get(PaymentAttempt, first.payment_attempt_id)
            assert current.status == ("SUCCEEDED" if state == "captured" else "PENDING")
            assert (
                current.next_check_at is not None
                if state != "captured"
                else current.next_check_at is None
            )
            assert await session.scalar(select(func.count()).select_from(PaymentAttempt)) == 1
            evidence = list(await session.scalars(select(PaymentProviderEvent)))
            assert any(e.provider_payment_id == "pay_instrument" for e in evidence)
        assert len(gateway.posts) == 1


async def test_many_failed_instruments_reuse_identity_and_preserve_each_observation(
    database_session_factory, database_engine
):
    factory = database_session_factory
    user, result = await booking(factory)
    gateway = Gateway(database_engine)
    async with httpx.AsyncClient(transport=httpx.MockTransport(gateway)) as remote:
        provider = RazorpayProvider(settings(), client=remote)
        first = await initiate(factory, user, result, provider)
        gateway.payments = [instrument(first, "failed", f"pay_failed{i}") for i in range(3)]
        await invalidate(factory, first)
        for key in ["attempt", "different", "different-again"]:
            resumed = await initiate(factory, user, result, provider, key)
            assert resumed.payment_attempt_id == first.payment_attempt_id
            assert resumed.provider_order_id == first.provider_order_id
            assert resumed.checkout_ready_until is not None
        value = await project(factory, result, provider)
        assert value.action == "RESUME" and value.retry_allowed
        async with factory() as session:
            failed = set(
                await session.scalars(
                    select(PaymentProviderEvent.provider_payment_id).where(
                        PaymentProviderEvent.observed_outcome == "FAILED"
                    )
                )
            )
            assert failed == {f"pay_failed{i}" for i in range(3)}
            assert not list(
                await session.scalars(
                    select(OutboxEvent).where(
                        OutboxEvent.payload["status"].astext == "PAYMENT_FAILED"
                    )
                )
            )
        assert len(gateway.posts) == 1


@pytest.mark.parametrize(
    "defect",
    ["missing_order_facts", "truncated", "unknown", "unavailable", "contradictory", "partial"],
)
async def test_incomplete_readiness_waits_without_another_order(
    database_session_factory, database_engine, defect
):
    factory = database_session_factory
    user, result = await booking(factory)
    gateway = Gateway(database_engine)
    faulty = False

    def transport(request):
        response = gateway(request)
        if faulty and request.url.path.endswith("/payments"):
            if defect == "unavailable":
                return httpx.Response(503, json={})
            data = response.json()
            if defect == "truncated":
                data["count"] += 1
            return httpx.Response(200, json=data)
        if faulty and "/orders/" in request.url.path and not request.url.path.endswith("/payments"):
            data = response.json()
            if defect == "missing_order_facts":
                data.pop("attempts")
            if defect == "contradictory":
                data["amount_due"] = 499
            if defect == "partial":
                data["partial_payment"] = True
            return httpx.Response(200, json=data)
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as remote:
        provider = RazorpayProvider(settings(), client=remote)
        first = await initiate(factory, user, result, provider)
        gateway.payments = [instrument(first, "alien" if defect == "unknown" else "failed")]
        faulty = True
        await invalidate(factory, first)
        resumed = await initiate(factory, user, result, provider, "new-key")
        assert resumed.checkout_ready_until is None
        value = await project(factory, result, provider)
        assert value.action == "WAIT" and not value.retry_allowed
        assert resumed.payment_attempt_id == first.payment_attempt_id
        assert len(gateway.posts) == 1


@pytest.mark.parametrize("gate", ["cancelled", "expired", "cutoff", "frozen"])
async def test_uncertain_terminal_replay_never_posts(
    database_session_factory, database_engine, migrated_database_url, gate
):
    factory = database_session_factory
    user, result = await booking(factory)
    gateway = Gateway(database_engine)
    post_calls = []

    def transport(request):
        if request.method == "POST":
            post_calls.append(json.loads(request.content))
            raise httpx.ReadTimeout("unknown delivery", request=request)
        return gateway(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as remote:
        provider = RazorpayProvider(settings(), client=remote)
        first = await initiate(factory, user, result, provider)
        assert first.status == "INITIATION_UNCERTAIN"
        if gate == "cancelled":
            await cancel(factory, result.request)
        elif gate == "expired":
            async with factory() as session, session.begin():
                request = await session.get(CollectionRequest, result.request.request_id)
                request.created_at = utc_now() - timedelta(hours=2)
                request.payment_expires_at = utc_now() - timedelta(seconds=1)
        elif gate == "frozen":
            await freeze_work_unit(factory, result.request.request_id)
        lead = 100000 if gate == "cutoff" else 30
        replay = await initiate_payment_attempt(
            factory,
            InitiatePaymentAttemptCommand(user.user_id, result.request.request_id, "attempt"),
            provider,
            idempotency_expires_at=utc_now() + timedelta(days=1),
            planning_lead_time_minutes=lead,
        )
        assert replay.payment_attempt_id == first.payment_attempt_id
        assert replay.checkout_ready_until is None and len(post_calls) == 1
        app = api_for(factory, user, provider, migrated_database_url)
        app.state.settings.planning_lead_time_minutes = lead
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as api:
            response = await api.post(
                f"/v1/payments/collection-requests/{result.request.request_id}/attempts",
                headers={"Idempotency-Key": "fresh-terminal-key"},
            )
            assert response.status_code == 201 and response.json()["action"] == "UNAVAILABLE"
        async with factory() as session:
            current = await session.get(PaymentAttempt, first.payment_attempt_id)
            assert current.next_check_at is not None and not current.order_creation_pending


@pytest.mark.parametrize("gate", ["cutoff", "frozen", "missing_config"])
async def test_first_initiation_checks_planning_before_remote_creation(
    database_session_factory, database_engine, gate
):
    factory = database_session_factory
    user, result = await booking(factory)
    gateway = Gateway(database_engine)
    if gate == "frozen":
        await freeze_work_unit(factory, result.request.request_id)
    from tirodhan.modules.payments.service import PaymentNotEligibleError

    async with httpx.AsyncClient(transport=httpx.MockTransport(gateway)) as remote:
        provider = RazorpayProvider(settings(), client=remote)
        with pytest.raises(
            PlanningConfigurationError if gate == "missing_config" else PaymentNotEligibleError
        ):
            await initiate_payment_attempt(
                factory,
                InitiatePaymentAttemptCommand(user.user_id, result.request.request_id, "first"),
                provider,
                idempotency_expires_at=utc_now() + timedelta(days=1),
                planning_lead_time_minutes=None
                if gate == "missing_config"
                else 100000
                if gate == "cutoff"
                else 30,
            )
        assert gateway.calls == []
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(PaymentAttempt)) == 0


@pytest.mark.parametrize("pause_at", ["lookup", "post"])
async def test_cancellation_during_creation_rechecks_post_permission_and_retains_inflight_result(
    database_session_factory, database_engine, migrated_database_url, pause_at
):
    factory = database_session_factory
    user, result = await booking(factory)
    gateway = Gateway(database_engine)
    entered, release = asyncio.Event(), asyncio.Event()

    async def transport(request):
        matching = (
            request.method == ("GET" if pause_at == "lookup" else "POST")
            and request.url.path == "/v1/orders"
        )
        response = gateway(request)
        if matching:
            entered.set()
            await release.wait()
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as remote:
        provider = RazorpayProvider(settings(), client=remote)
        preparing = asyncio.create_task(initiate(factory, user, result, provider))
        await asyncio.wait_for(entered.wait(), 5)
        await cancel(factory, result.request)
        release.set()
        attempt = await preparing
        assert attempt.checkout_ready_until is None
        assert len(gateway.posts) == (1 if pause_at == "post" else 0)
        if pause_at == "post":
            assert attempt.provider_order_id
            assert attempt.status == "PENDING" and attempt.next_check_at is not None
            gateway.payments = [instrument(attempt, "captured")]
            await inquire_target(
                factory, provider, attempt.payment_attempt_id, refund=False, policy=POLICY
            )
            async with factory() as session:
                assert (
                    await session.get(CollectionRequest, result.request.request_id)
                ).status == "CANCELLED"
                assert await session.scalar(select(func.count()).select_from(Refund)) == 1
        app = api_for(factory, user, provider, migrated_database_url)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as api:
            assert (
                await api.get(
                    f"/v1/customer/payment-attempts/{attempt.payment_attempt_id}/checkout"
                )
            ).status_code == 409


@pytest.mark.parametrize("same_account", [True, False, None])
async def test_account_rotation_and_legacy_ownership_fail_closed(
    database_session_factory, database_engine, migrated_database_url, same_account
):
    factory = database_session_factory
    user, result = await booking(factory)
    gateway = Gateway(database_engine)
    async with httpx.AsyncClient(transport=httpx.MockTransport(gateway)) as remote:
        first_provider = RazorpayProvider(
            settings(razorpay_account_id="acct_original"), client=remote
        )
        first = await initiate(factory, user, result, first_provider)
        rotated_settings = settings(
            razorpay_account_id="acct_original" if same_account else "acct_other"
        )
        rotated_settings.razorpay_key_id = "rzp_rotated"
        rotated = RazorpayProvider(rotated_settings, client=remote)
        if same_account is None:
            async with factory() as session, session.begin():
                (
                    await session.get(PaymentAttempt, first.payment_attempt_id)
                ).provider_account_key = None
        app = api_for(factory, user, rotated, migrated_database_url)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as api:
            handoff = await api.get(
                f"/v1/customer/payment-attempts/{first.payment_attempt_id}/checkout"
            )
            assert handoff.status_code == (200 if same_account is True else 409)
            if same_account is True:
                assert handoff.json()["public_key_id"] == "rzp_rotated"
            before = len(gateway.calls)
            resumed = await initiate(factory, user, result, rotated, "rotated")
            assert resumed.payment_attempt_id == first.payment_attempt_id
            assert len(gateway.calls) == before
            assert bool(resumed.checkout_ready_until) == (same_account is True)
        assert len(gateway.posts) == 1


@pytest.mark.parametrize("capture", [False, True])
async def test_observation_during_and_after_preparation_fences_handoff(
    database_session_factory, database_engine, migrated_database_url, capture
):
    factory = database_session_factory
    user, result = await booking(factory)
    gateway = Gateway(database_engine)
    entered, release = asyncio.Event(), asyncio.Event()
    blocked = False

    async def transport(request):
        response = gateway(request)
        if blocked and request.url.path.endswith("/payments"):
            entered.set()
            await release.wait()
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as remote:
        provider = RazorpayProvider(settings(), client=remote)
        first = await initiate(factory, user, result, provider)
        await invalidate(factory, first)
        blocked = True
        preparing = asyncio.create_task(initiate(factory, user, result, provider, "prepare-again"))
        await asyncio.wait_for(entered.wait(), 5)
        await process(
            factory,
            provider,
            payment_body(first, event_type="payment.captured" if capture else "payment.authorized"),
            event_id="newer-evidence",
        )
        release.set()
        resumed = await preparing
        assert resumed.checkout_ready_until is None
        value = await project(factory, result, provider)
        assert value.action == ("UNAVAILABLE" if capture else "WAIT")
        assert value.status == ("SUCCEEDED" if capture else "CONFIRMING")
        app = api_for(factory, user, provider, migrated_database_url)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as api:
            assert (
                await api.get(f"/v1/customer/payment-attempts/{first.payment_attempt_id}/checkout")
            ).status_code == 409
        assert len(gateway.posts) == 1


@pytest.mark.parametrize("captured", [False, True])
async def test_legacy_failed_established_order_is_discovered_without_new_post(
    database_session_factory, database_engine, captured
):
    factory = database_session_factory
    user, result = await booking(factory)
    gateway = Gateway(database_engine)
    async with httpx.AsyncClient(transport=httpx.MockTransport(gateway)) as remote:
        provider = RazorpayProvider(settings(), client=remote)
        first = await initiate(factory, user, result, provider)
        await cancel(factory, result.request)
        await invalidate(factory, first, failed=True)
        gateway.payments = [instrument(first, "captured" if captured else "failed")]
        assert await reconcile_batch(factory, provider, replace(POLICY, batch_size=1)) == 1
        async with factory() as session:
            attempt = await session.get(PaymentAttempt, first.payment_attempt_id)
            assert attempt.status == ("SUCCEEDED" if captured else "PENDING")
            assert bool(attempt.next_check_at) == (not captured)
            assert (
                await session.get(CollectionRequest, result.request.request_id)
            ).status == "CANCELLED"
            assert await session.scalar(select(func.count()).select_from(Refund)) == (
                1 if captured else 0
            )
        # Restart/re-delivery through fresh inquiry converges on the same charge.
        await inquire_target(
            factory, provider, first.payment_attempt_id, refund=False, policy=POLICY
        )
        assert len(gateway.posts) == 1
        async with factory() as session:
            assert await session.scalar(select(func.count()).select_from(CapturedCharge)) == (
                1 if captured else 0
            )


class ReplacementPricing:
    def __init__(self):
        self.calls = 0

    async def quote(self, items):
        self.calls += 1
        return PricingQuote(
            total_amount_minor=700,
            currency="INR",
            items=tuple(
                QuotedRequestItem(quoted_line_amount_minor=350, pricing_rule_version="new-quote")
                for _ in items
            ),
        )


@pytest.mark.parametrize("ordering", ["a_first", "b_first", "a_unresolved"])
async def test_cancel_replace_independent_accounting_and_verified_refund(
    database_session_factory, database_engine, ordering
):
    factory = database_session_factory
    user, a = await booking(factory)
    gateway = Gateway(database_engine)
    async with httpx.AsyncClient(transport=httpx.MockTransport(gateway)) as remote:
        provider = RazorpayProvider(settings(), client=remote)
        attempt_a = await initiate(factory, user, a, provider)
        gateway.payments = [instrument(attempt_a, "authorized")]
        await invalidate(factory, attempt_a)
        await initiate(factory, user, a, provider)
        await cancel(factory, a.request, "stable-cancel")
        await cancel(factory, a.request, "stable-cancel")  # lost response/exact replay
        context = await create_context(factory, user.user_id)
        client_id, pricing = new_uuid7(), ReplacementPricing()
        changed_items = (
            DeclaredRequestItem(item_category_code="TEST_A", declared_quantity=2),
            DeclaredRequestItem(item_category_code="TEST_B", declared_weight_grams=900),
        )
        b = await create_request(
            factory,
            user.user_id,
            context.serviceability_context_id,
            client_request_id=client_id,
            pricing=pricing,
            items=changed_items,
        )
        replay_b = await create_request(
            factory,
            user.user_id,
            context.serviceability_context_id,
            client_request_id=client_id,
            pricing=pricing,
            items=changed_items,
        )
        assert b.request.request_id == replay_b.request.request_id and pricing.calls == 1
        assert b.request.client_request_id != a.request.client_request_id
        assert b.request.serviceability_context_id != a.request.serviceability_context_id
        assert b.payment.payment_id != a.payment.payment_id and b.payment.amount_minor == 700
        attempt_b = await initiate(factory, user, b, provider, "b-first")
        assert (
            attempt_b.checkout_ready_until
            and attempt_b.provider_order_id != attempt_a.provider_order_id
        )
        capture_a = instrument(attempt_a, "captured", "pay_a")
        capture_b = instrument(attempt_b, "captured", "pay_b", 700)
        for identity in (
            ["a", "b"] if ordering == "a_first" else ["b", "a"] if ordering == "b_first" else ["b"]
        ):
            gateway.payments = [
                capture_a if identity == "a" else instrument(attempt_a, "authorized"),
                capture_b,
            ]
            await inquire_target(
                factory,
                provider,
                (attempt_a if identity == "a" else attempt_b).payment_attempt_id,
                refund=False,
                policy=POLICY,
            )
        async with factory() as session:
            assert (
                await session.get(CollectionRequest, a.request.request_id)
            ).status == "CANCELLED"
            assert (await session.get(CollectionRequest, b.request.request_id)).status == "ACCEPTED"
            assert (await session.get(Payment, b.payment.payment_id)).amount_minor == 700
            assert await session.scalar(select(func.count()).select_from(CollectionRequest)) == 2
            refunds = list(await session.scalars(select(Refund)))
            assert len(refunds) == (0 if ordering == "a_unresolved" else 1)
        if ordering != "a_unresolved":
            refund = refunds[0]
            assert refund.payment_id == a.payment.payment_id and refund.amount_minor == 500
            await inquire_target(
                factory, provider, attempt_a.payment_attempt_id, refund=False, policy=POLICY
            )
            await execute_refund_provider_call(factory, refund.refund_id, provider)
            async with factory() as session:
                refund = await session.get(Refund, refund.refund_id)
                assert refund.status == "SUBMITTED"
            gateway.refunds[refund.provider_refund_id]["status"] = "processed"
            await inquire_target(factory, provider, refund.refund_id, refund=True, policy=POLICY)
            async with factory() as session:
                assert (await session.get(Refund, refund.refund_id)).status == "SUCCEEDED"
                assert (
                    await session.get(CollectionRequest, b.request.request_id)
                ).status == "ACCEPTED"
                assert await session.scalar(select(func.count()).select_from(RefundObligation)) == 1
                assert await session.scalar(select(func.count()).select_from(Refund)) == 1
        assert len(gateway.posts) == 2


async def test_cancel_replace_requires_new_context_and_current_slot(
    database_session_factory, database_engine
):
    factory = database_session_factory
    user, a = await booking(factory)
    gateway = Gateway(database_engine)
    async with httpx.AsyncClient(transport=httpx.MockTransport(gateway)) as remote:
        provider = RazorpayProvider(settings(), client=remote)
        await initiate(factory, user, a, provider)
        await cancel(factory, a.request)
        with pytest.raises(IntegrityError):
            await create_request(factory, user.user_id, a.request.serviceability_context_id)
        context = await create_context(factory, user.user_id)
        await freeze_work_unit(factory, a.request.request_id)
        with pytest.raises(SlotConflictError):
            await create_request(factory, user.user_id, context.serviceability_context_id)
        async with factory() as session:
            assert (
                await session.get(CollectionRequest, a.request.request_id)
            ).status == "CANCELLED"
            assert await session.scalar(select(func.count()).select_from(CollectionRequest)) == 1
            assert await session.scalar(select(func.count()).select_from(Payment)) == 1


async def test_historical_missing_account_gets_bounded_attention_without_guessing(
    database_session_factory, database_engine
):
    factory = database_session_factory
    user, a = await booking(factory)
    gateway = Gateway(database_engine)
    async with httpx.AsyncClient(transport=httpx.MockTransport(gateway)) as remote:
        provider = RazorpayProvider(settings(), client=remote)
        attempt = await initiate(factory, user, a, provider)
        await invalidate(factory, attempt, failed=True)
        async with factory() as session, session.begin():
            row = await session.get(PaymentAttempt, attempt.payment_attempt_id)
            row.provider_account_key = None
            row.created_at = utc_now() - timedelta(days=1)
        before = len(gateway.calls)
        assert await reconcile_batch(factory, provider, replace(POLICY, batch_size=1)) == 1
        assert len(gateway.calls) == before
        async with factory() as session:
            row = await session.get(PaymentAttempt, attempt.payment_attempt_id)
            assert row.status == "FAILED" and row.provider_account_key is None
            assert row.next_check_at is not None
            assert await session.scalar(select(func.count()).select_from(FinancialException)) == 1


async def test_positive_initial_order_rejection_is_distinct_from_instrument_failure(
    database_session_factory, database_engine, migrated_database_url
):
    factory = database_session_factory
    user, result = await booking(factory)
    gateway = Gateway(database_engine)
    rejected = False

    def transport(request):
        nonlocal rejected
        if request.method == "POST" and not rejected:
            rejected = True
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
        return gateway(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as remote:
        provider = RazorpayProvider(settings(), client=remote)
        first = await initiate(factory, user, result, provider)
        assert first.status == "FAILED" and first.provider_order_id is None
        assert first.failure_code == "RAZORPAY_ORDER_REJECTED"
        assert not first.order_creation_pending
        replay = await initiate(factory, user, result, provider)
        assert replay.payment_attempt_id == first.payment_attempt_id and not gateway.posts
        second = await initiate(factory, user, result, provider, "fresh-after-rejection")
        assert second.payment_attempt_id != first.payment_attempt_id and second.checkout_ready_until
        assert len(gateway.posts) == 1
        app = api_for(factory, user, provider, migrated_database_url)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as api:
            replay = await api.post(
                f"/v1/payments/collection-requests/{result.request.request_id}/attempts",
                headers={"Idempotency-Key": "attempt"},
            )
            assert replay.json()["payment_attempt_id"] == str(first.payment_attempt_id)
            assert replay.json()["action_reason"] == "SUPERSEDED_ATTEMPT"
            assert replay.json()["action"] == "UNAVAILABLE"


@pytest.mark.parametrize("pause_at", ["lookup", "post"])
async def test_restart_before_or_after_post_recovers_one_intent(
    database_session_factory, database_engine, pause_at
):
    factory = database_session_factory
    user, result = await booking(factory)
    gateway = Gateway(database_engine)
    entered, release = asyncio.Event(), asyncio.Event()
    interrupted = True

    async def transport(request):
        response = gateway(request)
        if interrupted and request.method == ("GET" if pause_at == "lookup" else "POST"):
            entered.set()
            await release.wait()
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as remote:
        provider = RazorpayProvider(settings(), client=remote)
        task = asyncio.create_task(initiate(factory, user, result, provider))
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        async with factory() as session, session.begin():
            attempt = await session.scalar(select(PaymentAttempt))
            attempt.claim_until = utc_now() - timedelta(seconds=1)  # expired process lease
            identity = attempt.payment_attempt_id
            assert attempt.order_creation_pending == (pause_at == "lookup")
        interrupted = False
        resumed = await initiate(factory, user, result, provider, "restart-key")
        assert resumed.payment_attempt_id == identity and resumed.checkout_ready_until
        assert len(gateway.posts) == 1 and len(gateway.orders) == 1
        async with factory() as session:
            assert await session.scalar(select(func.count()).select_from(PaymentAttempt)) == 1


@pytest.mark.parametrize(
    "change",
    ["payment_amount", "payment_currency", "quote", "item_update", "item_delete", "item_insert"],
)
async def test_financial_terms_are_frozen_at_first_durable_attempt(
    database_session_factory, database_engine, change
):
    factory = database_session_factory
    user, result = await booking(factory)
    gateway = Gateway(database_engine)
    async with httpx.AsyncClient(transport=httpx.MockTransport(gateway)) as remote:
        provider = RazorpayProvider(settings(), client=remote)
        first = await initiate(factory, user, result, provider)
        with pytest.raises(IntegrityError, match="immutable"):
            async with factory() as session, session.begin():
                if change.startswith("payment"):
                    payment = await session.get(Payment, result.payment.payment_id)
                    if change == "payment_amount":
                        payment.amount_minor = 501
                    else:
                        payment.currency = "USD"
                elif change == "quote":
                    (
                        await session.get(CollectionRequest, result.request.request_id)
                    ).quoted_amount_minor = 501
                else:
                    item = await session.scalar(
                        select(CollectionRequestItem).where(
                            CollectionRequestItem.request_id == result.request.request_id
                        )
                    )
                    if change == "item_delete":
                        await session.delete(item)
                    elif change == "item_update":
                        item.declared_quantity = 99
                    else:
                        session.add(
                            CollectionRequestItem(
                                request_id=result.request.request_id,
                                item_category_code="TEST_A",
                                declared_quantity=1,
                                quoted_line_amount_minor=1,
                                currency="INR",
                                pricing_rule_version="bad-edit",
                            )
                        )
        replay = await initiate(factory, user, result, provider, "unchanged")
        assert replay.payment_attempt_id == first.payment_attempt_id
        async with factory() as session:
            assert (await session.get(Payment, result.payment.payment_id)).amount_minor == 500
            assert (
                await session.get(CollectionRequest, result.request.request_id)
            ).quoted_amount_minor == 500
            assert (
                await session.scalar(select(func.count()).select_from(CollectionRequestItem)) == 2
            )
        assert len(gateway.posts) == 1


@pytest.mark.parametrize("captured", [True, False])
async def test_committed_observation_during_checkout_read_revokes_handoff(
    database_session_factory, database_engine, migrated_database_url, monkeypatch, captured
):
    from sqlalchemy.ext.asyncio import AsyncSession

    factory = database_session_factory
    user, result = await booking(factory)
    gateway = Gateway(database_engine)
    async with httpx.AsyncClient(transport=httpx.MockTransport(gateway)) as remote:
        provider = RazorpayProvider(settings(), client=remote)
        attempt = await initiate(factory, user, result, provider)
        app = api_for(factory, user, provider, migrated_database_url)
        original = AsyncSession.execute
        observing = False

        async def read_then_observe(session, statement, *args, **kwargs):
            nonlocal observing
            row = await original(session, statement, *args, **kwargs)
            if not observing and "FROM payment_attempt JOIN payment" in str(statement):
                observing = True
                await process(
                    factory,
                    provider,
                    payment_body(
                        attempt, event_type="payment.captured" if captured else "payment.authorized"
                    ),
                    event_id="during-handoff",
                )
            return row

        monkeypatch.setattr(AsyncSession, "execute", read_then_observe)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as api:
            response = await api.get(
                f"/v1/customer/payment-attempts/{attempt.payment_attempt_id}/checkout"
            )
            assert observing and response.status_code == 409
        assert len(gateway.posts) == 1


async def test_readiness_expiry_is_permission_expiry_and_unset_policy_fails_closed(
    database_session_factory, database_engine, migrated_database_url
):
    from tirodhan.modules.payments.ports import PaymentProviderNotConfiguredError

    factory = database_session_factory
    user, result = await booking(factory)
    gateway = Gateway(database_engine)
    async with httpx.AsyncClient(transport=httpx.MockTransport(gateway)) as remote:
        provider = RazorpayProvider(settings(), client=remote)
        attempt = await initiate(factory, user, result, provider)
        async with factory() as session, session.begin():
            (await session.get(PaymentAttempt, attempt.payment_attempt_id)).checkout_ready_until = (
                utc_now() - timedelta(seconds=1)
            )
        value = await project(factory, result, provider)
        assert value.action == "WAIT" and value.status == "CONFIRMING"
        assert not value.retry_allowed and value.verification_pending
        app = api_for(factory, user, provider, migrated_database_url)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as api:
            assert (
                await api.get(
                    f"/v1/customer/payment-attempts/{attempt.payment_attempt_id}/checkout"
                )
            ).status_code == 409
        resumed = await initiate(factory, user, result, provider, "permission-expired")
        assert (
            resumed.payment_attempt_id == attempt.payment_attempt_id
            and resumed.checkout_ready_until
        )
        unset = settings()
        unset.payment_checkout_readiness_seconds = None
        unavailable = RazorpayProvider(unset, client=remote)
        with pytest.raises(PaymentProviderNotConfiguredError):
            await initiate(factory, user, result, unavailable, "unconfigured")
        assert len(gateway.posts) == 1


async def test_documented_order_response_without_creation_option_can_start_checkout(
    database_session_factory, database_engine
):
    factory = database_session_factory
    user, result = await booking(factory)
    gateway = Gateway(database_engine)

    def transport(request):
        response = gateway(request)
        if (
            request.method == "GET"
            and "/orders/" in request.url.path
            and not request.url.path.endswith("/payments")
        ):
            order = response.json()
            order.pop("partial_payment", None)
            return httpx.Response(200, json=order)
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as remote:
        provider = RazorpayProvider(settings(), client=remote)
        attempt = await initiate(factory, user, result, provider)
        assert attempt.checkout_ready_until and attempt.status == "PENDING"
        assert gateway.posts[0]["partial_payment"] is False


async def test_readiness_migration_preserves_populated_financial_history_and_schema(
    database_session_factory, database_engine
):
    factory = database_session_factory
    user, result = await booking(factory)
    gateway = Gateway(database_engine)
    async with httpx.AsyncClient(transport=httpx.MockTransport(gateway)) as remote:
        provider = RazorpayProvider(settings(), client=remote)
        attempt = await initiate(factory, user, result, provider)
        await process(factory, provider, payment_body(attempt), event_id="migration-capture")
        await cancel(factory, result.request)
    tables = (
        "collection_request",
        "collection_request_item",
        "payment",
        "payment_attempt",
        "captured_charge",
        "payment_provider_event",
        "refund",
        "refund_obligation",
        "outbox_event",
    )

    async def history():
        async with factory() as session:
            snapshots = {}
            for table in tables:
                exclude = (
                    " - 'checkout_ready_until' - 'observation_version' - 'order_creation_pending'"
                    if table == "payment_attempt"
                    else ""
                )
                rows = list(
                    await session.scalars(text(f"SELECT to_jsonb(t){exclude} FROM {table} t"))
                )
                assert rows, table
                snapshots[table] = sorted(json.dumps(row, sort_keys=True) for row in rows)
            return snapshots

    before = await history()
    try:
        await asyncio.to_thread(
            migration_command.downgrade, Config("alembic.ini"), "0022_financial_review_recovery"
        )
        assert await history() == before
    finally:
        await asyncio.to_thread(migration_command.upgrade, Config("alembic.ini"), "head")
    assert await history() == before
    async with factory() as session:
        migrated = await session.get(PaymentAttempt, attempt.payment_attempt_id)
        assert migrated.checkout_ready_until is None
        assert migrated.observation_version == 0 and not migrated.order_creation_pending
        triggers = await session.scalar(
            text(
                "SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal "
                "AND tgname IN ('initiated_item_terms','initiated_quote_terms',"
                "'initiated_payment_terms')"
            )
        )
        assert triggers == 3
    async with database_engine.connect() as connection:

        def check_schema(sync):
            differences = compare_metadata(
                MigrationContext.configure(
                    sync,
                    opts={
                        "compare_type": True,
                    },
                ),
                Base.metadata,
            )
            # Existing migration 0007 indexes and PostGIS extension-owned table are
            # known baseline omissions from ORM metadata, not new schema drift.
            assert len(differences) == 3, differences
            assert {(entry[0], entry[1].name) for entry in differences} == {
                ("remove_table", "spatial_ref_sys"),
                ("remove_index", "ix_collection_request_pickup_location_gist"),
                ("remove_index", "ix_collection_request_planning_batch_id"),
            }, differences

            def include_attempt(obj, name, kind, reflected, compare_to):
                return kind != "table" or name == "payment_attempt"

            assert (
                compare_metadata(
                    MigrationContext.configure(
                        sync,
                        opts={
                            "include_object": include_attempt,
                            "compare_type": True,
                            "compare_server_default": True,
                        },
                    ),
                    Base.metadata,
                )
                == []
            )

        await connection.run_sync(check_schema)
