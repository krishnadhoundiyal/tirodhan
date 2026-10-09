from datetime import timedelta

import httpx
import pytest
from razorpay_helpers import Orders, booking, initiate, payment_body, process, settings
from sqlalchemy import func, select, update

from tirodhan.api.dependencies import get_current_customer_id
from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.main import create_app
from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.payments.models import Payment, PaymentAttempt, PaymentProviderEvent
from tirodhan.modules.payments.ports import UnconfiguredPaymentProvider
from tirodhan.modules.payments.razorpay import RazorpayProvider
from tirodhan.modules.planning.models import PlanningBatch
from tirodhan.modules.reliability.models import IdempotencyRecord, OutboxEvent

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def financial_snapshot(factory):
    async with factory() as session:
        return {
            model.__tablename__: [
                tuple(getattr(row, column.key) for column in model.__table__.columns)
                for row in (await session.scalars(select(model))).all()
            ]
            for model in (
                CollectionRequest,
                Payment,
                PaymentAttempt,
                PaymentProviderEvent,
                IdempotencyRecord,
                OutboxEvent,
            )
        }


async def test_checkout_proposal_is_owned_persisted_read_only_and_capture_authoritative(
    database_session_factory, migrated_database_url
):
    factory = database_session_factory
    user, result = await booking(factory)
    orders = Orders()
    async with httpx.AsyncClient(transport=httpx.MockTransport(orders)) as remote:
        config = settings(
            database_url=migrated_database_url,
            planning_lead_time_minutes=30,
            razorpay_merchant_display_name="Tirodhan",
        )
        provider = RazorpayProvider(config, client=remote)
        attempt = await initiate(factory, user, result, provider)
        before = await financial_snapshot(factory)
        app = create_app(config, payment_provider=provider)
        app.state.database_session_factory = factory
        app.dependency_overrides[get_current_customer_id] = lambda: user.user_id
        path = f"/v1/customer/payment-attempts/{attempt.payment_attempt_id}/checkout"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as api:
            response = await api.get(path)
            assert response.status_code == 200
            assert response.headers["cache-control"] == "private, no-store"
            assert response.json() == {
                "request_id": str(result.request.request_id),
                "payment_attempt_id": str(attempt.payment_attempt_id),
                "provider": "RAZORPAY",
                "public_key_id": "rzp_test",
                "provider_order_id": attempt.provider_order_id,
                "merchant_display_name": "Tirodhan",
                "amount_minor": 500,
                "currency": "INR",
                "expires_at": result.request.payment_expires_at.isoformat().replace("+00:00", "Z"),
            }
            assert "test-secret" not in response.text and "webhook-secret" not in response.text
            assert (await api.get(path)).json() == response.json()
            assert await financial_snapshot(factory) == before
            assert len(orders.posts) == 1
            app.dependency_overrides[get_current_customer_id] = lambda: new_uuid7()
            assert (await api.get(path)).status_code == 404
            app.dependency_overrides.clear()
            assert (await api.get(path)).status_code == 401
            app.dependency_overrides[get_current_customer_id] = lambda: user.user_id
            assert (
                await api.get(f"/v1/customer/payment-attempts/{new_uuid7()}/checkout")
            ).status_code == 404
            assert (
                await api.get("/v1/customer/payment-attempts/invalid/checkout")
            ).status_code == 422
            await process(factory, provider, payment_body(attempt), event_id="checkout_capture")
            assert (await api.get(path)).status_code == 409
        async with factory() as session:
            request = await session.get(CollectionRequest, result.request.request_id)
            assert request.status == "ACCEPTED"
            assert await session.scalar(select(func.count()).select_from(PaymentAttempt)) == 1


@pytest.mark.parametrize(
    "gate, expected",
    [
        ("merchant", 503),
        ("provider", 503),
        ("planning_config", 503),
        ("expired", 409),
        ("cutoff", 409),
        ("frozen", 409),
        ("uncertain", 409),
        ("failed", 409),
        ("order", 503),
        ("amount", 503),
    ],
)
async def test_checkout_fails_closed_without_side_effects(
    database_session_factory, migrated_database_url, gate, expected
):
    factory = database_session_factory
    user, result = await booking(factory)
    orders = Orders()
    async with httpx.AsyncClient(transport=httpx.MockTransport(orders)) as remote:
        config = settings(
            database_url=migrated_database_url,
            planning_lead_time_minutes=30,
            razorpay_merchant_display_name="Tirodhan",
        )
        provider = RazorpayProvider(config, client=remote)
        attempt = await initiate(factory, user, result, provider)
        app = create_app(config, payment_provider=provider)
        app.state.database_session_factory = factory
        app.dependency_overrides[get_current_customer_id] = lambda: user.user_id
        if gate == "merchant":
            config.razorpay_merchant_display_name = None
        elif gate == "provider":
            app.state.payment_provider = UnconfiguredPaymentProvider()
        elif gate == "planning_config":
            config.planning_lead_time_minutes = None
        async with factory() as session, session.begin():
            if gate == "expired":
                await session.execute(
                    update(CollectionRequest)
                    .where(CollectionRequest.request_id == result.request.request_id)
                    .values(
                        created_at=utc_now() - timedelta(hours=2),
                        payment_expires_at=utc_now() - timedelta(seconds=1),
                    )
                )
            elif gate == "cutoff":
                config.planning_lead_time_minutes = 100000
            elif gate == "frozen":
                session.add(
                    PlanningBatch(
                        cell_id=result.request.cell_id,
                        slot_start=result.request.slot_start,
                        slot_end=result.request.slot_end,
                        status="PENDING",
                        max_attempts_snapshot=3,
                    )
                )
            elif gate in ("uncertain", "failed", "order"):
                await session.execute(
                    update(PaymentAttempt)
                    .where(PaymentAttempt.payment_attempt_id == attempt.payment_attempt_id)
                    .values(
                        **(
                            {"provider_order_id": "invalid-order"}
                            if gate == "order"
                            else {
                                "status": "INITIATION_UNCERTAIN"
                                if gate == "uncertain"
                                else "FAILED"
                            }
                        )
                    )
                )
            elif gate == "amount":
                await session.execute(
                    update(Payment)
                    .where(Payment.payment_id == attempt.payment_id)
                    .values(amount_minor=501)
                )
        before = await financial_snapshot(factory)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as api:
            response = await api.get(
                f"/v1/customer/payment-attempts/{attempt.payment_attempt_id}/checkout"
            )
            assert response.status_code == expected, response.text
            assert response.headers["cache-control"] == "private, no-store"
        assert await financial_snapshot(factory) == before and len(orders.posts) == 1
