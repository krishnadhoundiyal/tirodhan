from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from geoalchemy2.elements import WKTElement
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.api.dependencies import get_current_user_id
from tirodhan.core.config import Settings
from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.main import create_app
from tirodhan.modules.collection_requests.models import (
    CollectionRequest,
)
from tirodhan.modules.collection_requests.ports import (
    DeclaredRequestItem,
    PricingPort,
    PricingQuote,
    QuotedRequestItem,
)
from tirodhan.modules.collection_requests.service import (
    PAYMENT_PENDING,
    REQUEST_ACCEPTED,
    REQUEST_PENDING_PAYMENT,
    REQUEST_PRE_PLANNING,
    CollectionRequestResult,
    CreateCollectionRequestCommand,
    ServiceabilityContextIneligibleError,
    create_collection_request,
)
from tirodhan.modules.identity.models import AppUser
from tirodhan.modules.payments.models import Payment, PaymentAttempt, PaymentProviderEvent
from tirodhan.modules.payments.ports import (
    AuthenticatedPaymentEvent,
    PaymentEventOutcome,
    PaymentInitiationOutcome,
    PaymentInitiationResult,
    PaymentProvider,
    PaymentProviderAuthenticationError,
    PaymentProviderUncertainError,
)
from tirodhan.modules.payments.service import (
    ATTEMPT_FAILED,
    ATTEMPT_PENDING,
    ATTEMPT_SUCCEEDED,
    ATTEMPT_UNCERTAIN,
    EVENT_PROCESSED,
    EVENT_RECONCILIATION,
    InitiatePaymentAttemptCommand,
    initiate_payment_attempt,
    process_authenticated_payment_event,
)
from tirodhan.modules.planning.locking import acquire_work_unit_advisory_lock
from tirodhan.modules.planning.models import PlanningBatch
from tirodhan.modules.reliability.models import OutboxEvent
from tirodhan.modules.reliability.primitives import IdempotencyKeyConflictError
from tirodhan.modules.serviceability.models import ServiceabilityContext
from tirodhan.modules.serviceability.service import (
    SERVICEABILITY_PENDING,
    SERVICEABILITY_SERVICEABLE,
    SERVICEABILITY_UNSERVICEABLE,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

SLOT_START = utc_now().replace(second=0, microsecond=0) + timedelta(days=1)
SLOT_END = SLOT_START + timedelta(minutes=30)
ITEMS = (
    DeclaredRequestItem(item_category_code="TEST_A", declared_quantity=1),
    DeclaredRequestItem(item_category_code="TEST_B", declared_weight_grams=500),
)


class FixedPricing(PricingPort):
    def __init__(self) -> None:
        self.calls = 0

    async def quote(self, items: Sequence[DeclaredRequestItem]) -> PricingQuote:
        self.calls += 1
        return PricingQuote(
            total_amount_minor=500,
            currency="INR",
            items=(
                QuotedRequestItem(quoted_line_amount_minor=125, pricing_rule_version="test-v1"),
                QuotedRequestItem(quoted_line_amount_minor=375, pricing_rule_version="test-v2"),
            ),
        )


class FakePaymentProvider(PaymentProvider):
    provider_code = "testpay"

    def __init__(self) -> None:
        self.initiation_results: list[PaymentInitiationResult | Exception] = []
        self.initiation_calls: list[tuple[UUID, str]] = []
        self.on_initiate: Any = None
        self.webhook_event: AuthenticatedPaymentEvent | None = None

    async def initiate_payment(
        self,
        *,
        payment_attempt_id: UUID,
        amount_minor: int,
        currency: str,
        provider_idempotency_key: str,
    ) -> PaymentInitiationResult:
        self.initiation_calls.append((payment_attempt_id, provider_idempotency_key))
        if self.on_initiate is not None:
            await self.on_initiate(payment_attempt_id)
        result = self.initiation_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def authenticate_webhook(
        self, *, raw_body: bytes, headers: Mapping[str, str]
    ) -> AuthenticatedPaymentEvent:
        if headers.get("x-test-signature") != "valid":
            raise PaymentProviderAuthenticationError("bad test signature")
        if self.webhook_event is None:
            raise AssertionError("test webhook event was not configured")
        return self.webhook_event


async def create_user(factory: async_sessionmaker[AsyncSession]) -> AppUser:
    async with factory() as session, session.begin():
        user = AppUser(status="ACTIVE")
        session.add(user)
        await session.flush()
        return user


async def create_context(
    factory: async_sessionmaker[AsyncSession],
    user_id: UUID,
    *,
    status: str = SERVICEABILITY_SERVICEABLE,
    expired: bool = False,
    has_location: bool = True,
    has_cell: bool = True,
) -> ServiceabilityContext:
    now = utc_now()
    async with factory() as session, session.begin():
        context = ServiceabilityContext(
            serviceability_context_id=new_uuid7(),
            user_id=user_id,
            address_snapshot_encrypted=b"test-envelope:immutable-booking-address",
            location=(WKTElement("POINT(77.2090 28.6139)", srid=4326) if has_location else None),
            cell_id="test-cell" if has_cell else None,
            status=status,
            expires_at=now - timedelta(minutes=1) if expired else now + timedelta(hours=1),
            created_at=now,
            resolved_at=now if status == SERVICEABILITY_SERVICEABLE else None,
        )
        session.add(context)
        await session.flush()
        return context


async def create_request(
    factory: async_sessionmaker[AsyncSession],
    user_id: UUID,
    context_id: UUID,
    *,
    client_request_id: UUID | None = None,
    pricing: PricingPort | None = None,
    slot_start: datetime = SLOT_START,
    slot_end: datetime = SLOT_END,
    items: tuple[DeclaredRequestItem, ...] = ITEMS,
) -> CollectionRequestResult:
    return await create_collection_request(
        factory,
        CreateCollectionRequestCommand(
            customer_id=user_id,
            client_request_id=client_request_id or new_uuid7(),
            serviceability_context_id=context_id,
            slot_start=slot_start,
            slot_end=slot_end,
            items=items,
            payment_expires_at=utc_now() + timedelta(minutes=30),
        ),
        pricing or FixedPricing(),
        idempotency_expires_at=utc_now() + timedelta(hours=1),
    )


async def create_ready_attempt(
    factory: async_sessionmaker[AsyncSession],
    user_id: UUID,
    request_id: UUID,
    *,
    key: str,
    order_id: str,
) -> tuple[PaymentAttempt, FakePaymentProvider]:
    provider = FakePaymentProvider()
    provider.initiation_results.append(
        PaymentInitiationResult(
            outcome=PaymentInitiationOutcome.READY,
            provider_order_id=order_id,
        )
    )
    attempt = await initiate_payment_attempt(
        factory,
        InitiatePaymentAttemptCommand(
            customer_id=user_id,
            request_id=request_id,
            idempotency_key=key,
        ),
        provider,
        idempotency_expires_at=utc_now() + timedelta(hours=1),
    )
    return attempt, provider


def success_event(attempt: PaymentAttempt, event_id: str) -> AuthenticatedPaymentEvent:
    return AuthenticatedPaymentEvent(
        provider=attempt.provider,
        external_event_id=event_id,
        event_type="payment.succeeded",
        outcome=PaymentEventOutcome.SUCCEEDED,
        payment_attempt_id=attempt.payment_attempt_id,
        provider_order_id=attempt.provider_order_id,
        provider_payment_id=f"pay-{event_id}",
    )


@pytest.mark.asyncio
async def test_request_creation_replay_quote_and_immutable_snapshot(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user = await create_user(database_session_factory)
    context = await create_context(database_session_factory, user.user_id)
    pricing = FixedPricing()
    client_request_id = new_uuid7()
    result = await create_request(
        database_session_factory,
        user.user_id,
        context.serviceability_context_id,
        client_request_id=client_request_id,
        pricing=pricing,
    )
    replay = await create_request(
        database_session_factory,
        user.user_id,
        context.serviceability_context_id,
        client_request_id=client_request_id,
        pricing=pricing,
    )

    assert replay.request.request_id == result.request.request_id
    assert pricing.calls == 1
    assert result.request.status == REQUEST_PENDING_PAYMENT
    assert result.request.quoted_amount_minor == 500
    assert result.payment.amount_minor == 500
    assert [item.quoted_line_amount_minor for item in result.items] == [125, 375]
    assert [item.pricing_rule_version for item in result.items] == ["test-v1", "test-v2"]

    async with database_session_factory() as session, session.begin():
        await session.execute(
            update(ServiceabilityContext)
            .where(
                ServiceabilityContext.serviceability_context_id == context.serviceability_context_id
            )
            .values(
                address_snapshot_encrypted=b"test-envelope:changed-later",
                location=WKTElement("POINT(78 29)", srid=4326),
                cell_id="changed-cell",
            )
        )
    async with database_session_factory() as session:
        snapshot, cell_id, longitude, latitude = (
            await session.execute(
                text(
                    "SELECT pickup_address_snapshot_encrypted, cell_id, "
                    "ST_X(pickup_location::geometry), ST_Y(pickup_location::geometry) "
                    "FROM collection_request WHERE request_id = :request_id"
                ),
                {"request_id": result.request.request_id},
            )
        ).one()
    assert snapshot == b"test-envelope:immutable-booking-address"
    assert cell_id == "test-cell"
    assert longitude == pytest.approx(77.2090)
    assert latitude == pytest.approx(28.6139)

    with pytest.raises(IntegrityError):
        await create_request(
            database_session_factory,
            user.user_id,
            context.serviceability_context_id,
        )


@pytest.mark.asyncio
async def test_request_client_id_conflicts_when_booking_payload_changes(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user = await create_user(database_session_factory)
    first_context = await create_context(database_session_factory, user.user_id)
    second_context = await create_context(database_session_factory, user.user_id)
    client_request_id = new_uuid7()
    pricing = FixedPricing()
    await create_request(
        database_session_factory,
        user.user_id,
        first_context.serviceability_context_id,
        client_request_id=client_request_id,
        pricing=pricing,
    )

    changed_payloads = (
        {
            "context_id": first_context.serviceability_context_id,
            "slot_start": SLOT_START + timedelta(hours=1),
            "slot_end": SLOT_END + timedelta(hours=1),
            "items": ITEMS,
        },
        {
            "context_id": first_context.serviceability_context_id,
            "slot_start": SLOT_START,
            "slot_end": SLOT_END,
            "items": (DeclaredRequestItem(item_category_code="CHANGED", declared_quantity=2),),
        },
        {
            "context_id": second_context.serviceability_context_id,
            "slot_start": SLOT_START,
            "slot_end": SLOT_END,
            "items": ITEMS,
        },
    )
    for payload in changed_payloads:
        with pytest.raises(IdempotencyKeyConflictError):
            await create_request(
                database_session_factory,
                user.user_id,
                payload["context_id"],
                client_request_id=client_request_id,
                pricing=pricing,
                slot_start=payload["slot_start"],
                slot_end=payload["slot_end"],
                items=payload["items"],
            )

    assert pricing.calls == 1


@pytest.mark.asyncio
async def test_collection_request_api_creates_pending_request_and_payment(
    migrated_database_url: str,
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user = await create_user(database_session_factory)
    context = await create_context(database_session_factory, user.user_id)
    pricing = FixedPricing()
    app = create_app(
        Settings(
            _env_file=None,
            environment="test",
            database_url=migrated_database_url,
            command_idempotency_ttl_seconds=3600,
            pending_payment_lifetime_seconds=1800,
        ),
        pricing_port=pricing,
    )
    app.dependency_overrides[get_current_user_id] = lambda: user.user_id
    body = {
        "client_request_id": str(new_uuid7()),
        "serviceability_context_id": str(context.serviceability_context_id),
        "slot_start": SLOT_START.isoformat(),
        "slot_end": SLOT_END.isoformat(),
        "items": [
            {"item_category_code": "TEST_A", "declared_quantity": 1},
            {"item_category_code": "TEST_B", "declared_weight_grams": 500},
        ],
    }
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/v1/collection-requests",
                json=body,
            )
            replay = await client.post(
                "/v1/collection-requests",
                json=body,
            )

    assert response.status_code == 201
    assert replay.status_code == 201
    assert response.json() == replay.json()
    assert response.json()["status"] == REQUEST_PENDING_PAYMENT
    assert response.json()["quoted_amount_minor"] == 500
    assert pricing.calls == 1


@pytest.mark.asyncio
async def test_request_rejects_ineligible_serviceability_contexts(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    owner = await create_user(database_session_factory)
    other = await create_user(database_session_factory)
    cases = [
        await create_context(
            database_session_factory, owner.user_id, status=SERVICEABILITY_PENDING
        ),
        await create_context(
            database_session_factory, owner.user_id, status=SERVICEABILITY_UNSERVICEABLE
        ),
        await create_context(database_session_factory, owner.user_id, expired=True),
        await create_context(database_session_factory, other.user_id),
        await create_context(database_session_factory, owner.user_id, has_location=False),
        await create_context(database_session_factory, owner.user_id, has_cell=False),
    ]
    for context in cases:
        with pytest.raises(ServiceabilityContextIneligibleError):
            await create_request(
                database_session_factory,
                owner.user_id,
                context.serviceability_context_id,
            )


@pytest.mark.asyncio
async def test_payment_attempt_api_replay_commits_before_provider_call(
    migrated_database_url: str,
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user = await create_user(database_session_factory)
    context = await create_context(database_session_factory, user.user_id)
    request_result = await create_request(
        database_session_factory,
        user.user_id,
        context.serviceability_context_id,
    )
    provider = FakePaymentProvider()
    provider.initiation_results.append(
        PaymentInitiationResult(
            outcome=PaymentInitiationOutcome.READY,
            provider_order_id="order-api-1",
        )
    )

    async def assert_attempt_is_committed(attempt_id: UUID) -> None:
        async with database_session_factory() as session:
            assert await session.get(PaymentAttempt, attempt_id) is not None

    provider.on_initiate = assert_attempt_is_committed
    app = create_app(
        Settings(
            _env_file=None,
            environment="test",
            database_url=migrated_database_url,
            command_idempotency_ttl_seconds=3600,
        ),
        payment_provider=provider,
    )
    app.dependency_overrides[get_current_user_id] = lambda: user.user_id
    url = f"/v1/payments/collection-requests/{request_result.request.request_id}/attempts"
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            first = await client.post(url, headers={"Idempotency-Key": "attempt-api-key"})
            replay = await client.post(url, headers={"Idempotency-Key": "attempt-api-key"})

    assert first.status_code == 201
    assert replay.status_code == 201
    assert first.json()["payment_attempt_id"] == replay.json()["payment_attempt_id"]
    assert len(provider.initiation_calls) == 1
    attempt_id, stable_key = provider.initiation_calls[0]
    assert stable_key == f"payment-attempt:{attempt_id}"


@pytest.mark.asyncio
async def test_known_failure_and_uncertain_retry_are_durable(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user = await create_user(database_session_factory)
    context_one = await create_context(database_session_factory, user.user_id)
    request_one = await create_request(
        database_session_factory,
        user.user_id,
        context_one.serviceability_context_id,
    )
    failed_provider = FakePaymentProvider()
    failed_provider.initiation_results.append(
        PaymentInitiationResult(
            outcome=PaymentInitiationOutcome.FAILED,
            failure_code="DECLINED",
        )
    )
    failed = await initiate_payment_attempt(
        database_session_factory,
        InitiatePaymentAttemptCommand(
            customer_id=user.user_id,
            request_id=request_one.request.request_id,
            idempotency_key="known-failure-attempt",
        ),
        failed_provider,
        idempotency_expires_at=utc_now() + timedelta(hours=1),
    )
    assert failed.status == ATTEMPT_FAILED
    assert failed.failure_code == "DECLINED"

    context_two = await create_context(database_session_factory, user.user_id)
    request_two = await create_request(
        database_session_factory,
        user.user_id,
        context_two.serviceability_context_id,
    )
    uncertain_provider = FakePaymentProvider()
    uncertain_provider.initiation_results.extend(
        [
            PaymentProviderUncertainError("TIMEOUT"),
            PaymentInitiationResult(
                outcome=PaymentInitiationOutcome.READY,
                provider_order_id="order-after-timeout",
            ),
        ]
    )
    command = InitiatePaymentAttemptCommand(
        customer_id=user.user_id,
        request_id=request_two.request.request_id,
        idempotency_key="uncertain-attempt",
    )
    uncertain = await initiate_payment_attempt(
        database_session_factory,
        command,
        uncertain_provider,
        idempotency_expires_at=utc_now() + timedelta(hours=1),
    )
    retried = await initiate_payment_attempt(
        database_session_factory,
        command,
        uncertain_provider,
        idempotency_expires_at=utc_now() + timedelta(hours=1),
    )
    assert uncertain.status == ATTEMPT_UNCERTAIN
    assert retried.status == ATTEMPT_PENDING
    assert uncertain.payment_attempt_id == retried.payment_attempt_id
    assert {key for _, key in uncertain_provider.initiation_calls} == {
        uncertain.provider_idempotency_key
    }


@pytest.mark.asyncio
async def test_webhook_authentication_failure_has_no_business_effect(
    migrated_database_url: str,
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user = await create_user(database_session_factory)
    context = await create_context(database_session_factory, user.user_id)
    request_result = await create_request(
        database_session_factory,
        user.user_id,
        context.serviceability_context_id,
    )
    attempt, provider = await create_ready_attempt(
        database_session_factory,
        user.user_id,
        request_result.request.request_id,
        key="bad-webhook-attempt",
        order_id="order-bad-webhook",
    )
    provider.webhook_event = success_event(attempt, "bad-signature-event")
    app = create_app(
        Settings(_env_file=None, environment="test", database_url=migrated_database_url),
        payment_provider=provider,
    )
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/v1/payments/provider/webhook",
                headers={"x-test-signature": "invalid"},
                content=b'{"secret":"must-not-persist"}',
            )
    assert response.status_code == 401
    async with database_session_factory() as session:
        assert await session.scalar(select(func.count(PaymentProviderEvent.provider))) == 0
        payment = await session.get(Payment, request_result.payment.payment_id)
        request = await session.get(CollectionRequest, request_result.request.request_id)
    assert payment is not None and payment.status == PAYMENT_PENDING
    assert request is not None and request.status == REQUEST_PENDING_PAYMENT


@pytest.mark.asyncio
async def test_success_duplicate_and_additional_success_preserve_one_canonical_attempt(
    migrated_database_url: str,
    database_session_factory: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    user = await create_user(database_session_factory)
    context = await create_context(database_session_factory, user.user_id)
    request_result = await create_request(
        database_session_factory,
        user.user_id,
        context.serviceability_context_id,
    )
    first, _ = await create_ready_attempt(
        database_session_factory,
        user.user_id,
        request_result.request.request_id,
        key="success-attempt-one",
        order_id="order-success-one",
    )
    second, _ = await create_ready_attempt(
        database_session_factory,
        user.user_id,
        request_result.request.request_id,
        key="success-attempt-two",
        order_id="order-success-two",
    )
    raw_body = b'{"address":"must-not-persist","token":"secret"}'
    first_event = success_event(first, "success-event-one")
    webhook_provider = FakePaymentProvider()
    webhook_provider.webhook_event = first_event
    app = create_app(
        Settings(
            _env_file=None,
            environment="test",
            database_url=migrated_database_url,
            planning_lead_time_minutes=30,
        ),
        payment_provider=webhook_provider,
    )
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/v1/payments/provider/webhook",
                headers={"x-test-signature": "valid"},
                content=raw_body,
            )
            duplicate_response = await client.post(
                "/v1/payments/provider/webhook",
                headers={"x-test-signature": "valid"},
                content=raw_body,
            )
    assert response.status_code == 200
    assert duplicate_response.status_code == 200
    assert (
        response.json()["payment_provider_event_id"]
        == duplicate_response.json()["payment_provider_event_id"]
    )
    additional = await process_authenticated_payment_event(
        database_session_factory,
        success_event(second, "success-event-two"),
        payload_hash=hashlib.sha256(b"second").digest(),
        planning_lead_time_minutes=30,
    )

    async with database_session_factory() as session:
        payment = await session.get(Payment, request_result.payment.payment_id)
        request = await session.get(CollectionRequest, request_result.request.request_id)
        attempts = list(
            await session.scalars(
                select(PaymentAttempt).where(
                    PaymentAttempt.payment_id == request_result.payment.payment_id
                )
            )
        )
        event_count = await session.scalar(select(func.count(PaymentProviderEvent.provider)))
        outbox_count = await session.scalar(
            select(func.count(OutboxEvent.outbox_event_id)).where(
                OutboxEvent.event_type == "CollectionRequestAccepted"
            )
        )
        stored_hash = await session.scalar(
            select(PaymentProviderEvent.payload_hash).where(
                PaymentProviderEvent.external_event_id == first_event.external_event_id
            )
        )
    assert additional.processing_status == EVENT_RECONCILIATION
    assert additional.failure_code == "ADDITIONAL_SUCCESS"
    assert payment is not None and payment.successful_attempt_id == first.payment_attempt_id
    assert request is not None and request.status == REQUEST_ACCEPTED
    assert all(attempt.status == ATTEMPT_SUCCEEDED for attempt in attempts)
    assert event_count == 2
    assert outbox_count == 1
    assert stored_hash == hashlib.sha256(raw_body).digest()
    assert raw_body.decode() not in caplog.text


async def freeze_work_unit(
    factory: async_sessionmaker[AsyncSession],
    request_id: UUID,
    *,
    acquired: asyncio.Event | None = None,
    release: asyncio.Event | None = None,
) -> None:
    async with factory() as session, session.begin():
        request = await session.get(CollectionRequest, request_id)
        assert request is not None
        await acquire_work_unit_advisory_lock(
            session,
            cell_id=request.cell_id,
            slot_start=request.slot_start,
            slot_end=request.slot_end,
        )
        if acquired is not None:
            acquired.set()
        if release is not None:
            await release.wait()
        batch = PlanningBatch(
            planning_batch_id=new_uuid7(),
            cell_id=request.cell_id,
            slot_start=request.slot_start,
            slot_end=request.slot_end,
            status="CREATED",
            max_attempts_snapshot=1,
            created_at=utc_now(),
        )
        session.add(batch)
        await session.flush([batch])
        await session.refresh(request)
        if request.status == REQUEST_ACCEPTED:
            request.status = REQUEST_PRE_PLANNING
            request.planning_batch_id = batch.planning_batch_id


@pytest.mark.asyncio
async def test_distinct_success_events_for_canonical_attempt_are_normally_processed_once(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user = await create_user(database_session_factory)
    context = await create_context(database_session_factory, user.user_id)
    request_result = await create_request(
        database_session_factory,
        user.user_id,
        context.serviceability_context_id,
    )
    attempt, _ = await create_ready_attempt(
        database_session_factory,
        user.user_id,
        request_result.request.request_id,
        key="canonical-repeat-attempt",
        order_id="order-canonical-repeat",
    )

    first = await process_authenticated_payment_event(
        database_session_factory,
        success_event(attempt, "canonical-success-one"),
        payload_hash=hashlib.sha256(b"canonical-one").digest(),
        planning_lead_time_minutes=30,
    )
    second = await process_authenticated_payment_event(
        database_session_factory,
        success_event(attempt, "canonical-success-two"),
        payload_hash=hashlib.sha256(b"canonical-two").digest(),
        planning_lead_time_minutes=30,
    )

    async with database_session_factory() as session:
        request = await session.get(CollectionRequest, request_result.request.request_id)
        payment = await session.get(Payment, request_result.payment.payment_id)
        events = list(
            await session.scalars(
                select(PaymentProviderEvent).where(
                    PaymentProviderEvent.payment_attempt_id == attempt.payment_attempt_id
                )
            )
        )
        outbox_count = await session.scalar(
            select(func.count(OutboxEvent.outbox_event_id)).where(
                OutboxEvent.event_type == "CollectionRequestAccepted"
            )
        )

    assert first.processing_status == EVENT_PROCESSED
    assert second.processing_status == EVENT_PROCESSED
    assert second.failure_code is None
    assert request is not None and request.status == REQUEST_ACCEPTED
    assert payment is not None and payment.successful_attempt_id == attempt.payment_attempt_id
    assert {event.processing_status for event in events} == {EVENT_PROCESSED}
    assert len(events) == 2
    assert outbox_count == 1


@pytest.mark.asyncio
async def test_canonical_success_event_after_planning_freeze_is_normally_processed(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user = await create_user(database_session_factory)
    context = await create_context(database_session_factory, user.user_id)
    request_result = await create_request(
        database_session_factory,
        user.user_id,
        context.serviceability_context_id,
    )
    attempt, _ = await create_ready_attempt(
        database_session_factory,
        user.user_id,
        request_result.request.request_id,
        key="canonical-after-freeze-attempt",
        order_id="order-canonical-after-freeze",
    )
    first = await process_authenticated_payment_event(
        database_session_factory,
        success_event(attempt, "canonical-before-freeze"),
        payload_hash=hashlib.sha256(b"before-freeze").digest(),
        planning_lead_time_minutes=30,
    )
    await freeze_work_unit(database_session_factory, request_result.request.request_id)
    second = await process_authenticated_payment_event(
        database_session_factory,
        success_event(attempt, "canonical-after-freeze"),
        payload_hash=hashlib.sha256(b"after-freeze").digest(),
        planning_lead_time_minutes=30,
    )

    async with database_session_factory() as session:
        request = await session.get(CollectionRequest, request_result.request.request_id)
        payment = await session.get(Payment, request_result.payment.payment_id)
        batch_count = await session.scalar(select(func.count(PlanningBatch.planning_batch_id)))
        outbox_count = await session.scalar(
            select(func.count(OutboxEvent.outbox_event_id)).where(
                OutboxEvent.event_type == "CollectionRequestAccepted"
            )
        )

    assert first.processing_status == EVENT_PROCESSED
    assert second.processing_status == EVENT_PROCESSED
    assert second.failure_code is None
    assert request is not None and request.status == REQUEST_PRE_PLANNING
    assert request.planning_batch_id is not None
    assert payment is not None and payment.successful_attempt_id == attempt.payment_attempt_id
    assert batch_count == 1
    assert outbox_count == 1


@pytest.mark.asyncio
async def test_freeze_wins_shared_lock_and_prevents_acceptance(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user = await create_user(database_session_factory)
    context = await create_context(database_session_factory, user.user_id)
    request_result = await create_request(
        database_session_factory,
        user.user_id,
        context.serviceability_context_id,
    )
    attempt, _ = await create_ready_attempt(
        database_session_factory,
        user.user_id,
        request_result.request.request_id,
        key="freeze-wins-attempt",
        order_id="order-freeze-wins",
    )
    acquired = asyncio.Event()
    release = asyncio.Event()
    freeze_task = asyncio.create_task(
        freeze_work_unit(
            database_session_factory,
            request_result.request.request_id,
            acquired=acquired,
            release=release,
        )
    )
    await acquired.wait()
    payment_task = asyncio.create_task(
        process_authenticated_payment_event(
            database_session_factory,
            success_event(attempt, "freeze-wins-event"),
            payload_hash=hashlib.sha256(b"freeze-wins").digest(),
            planning_lead_time_minutes=30,
        )
    )
    await asyncio.sleep(0)
    release.set()
    await freeze_task
    event = await payment_task

    async with database_session_factory() as session:
        request = await session.get(CollectionRequest, request_result.request.request_id)
        payment = await session.get(Payment, request_result.payment.payment_id)
        durable_attempt = await session.get(PaymentAttempt, attempt.payment_attempt_id)
        outbox_count = await session.scalar(
            select(func.count(OutboxEvent.outbox_event_id)).where(
                OutboxEvent.event_type == "CollectionRequestAccepted"
            )
        )
    assert event.processing_status == EVENT_RECONCILIATION
    assert event.failure_code == "WORK_UNIT_FROZEN"
    assert request is not None and request.status == REQUEST_PENDING_PAYMENT
    assert payment is not None and payment.status == PAYMENT_PENDING
    assert durable_attempt is not None and durable_attempt.status == ATTEMPT_SUCCEEDED
    assert outbox_count == 0


@pytest.mark.asyncio
async def test_payment_wins_shared_lock_then_freeze_includes_request_consistently(
    database_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tirodhan.modules.payments import service as payment_service

    user = await create_user(database_session_factory)
    context = await create_context(database_session_factory, user.user_id)
    request_result = await create_request(
        database_session_factory,
        user.user_id,
        context.serviceability_context_id,
    )
    attempt, _ = await create_ready_attempt(
        database_session_factory,
        user.user_id,
        request_result.request.request_id,
        key="payment-wins-attempt",
        order_id="order-payment-wins",
    )
    acquired = asyncio.Event()
    release = asyncio.Event()
    real_lock = acquire_work_unit_advisory_lock

    async def held_payment_lock(session: AsyncSession, **kwargs: Any) -> int:
        key = await real_lock(session, **kwargs)
        acquired.set()
        await release.wait()
        return key

    monkeypatch.setattr(payment_service, "acquire_work_unit_advisory_lock", held_payment_lock)
    payment_task = asyncio.create_task(
        process_authenticated_payment_event(
            database_session_factory,
            success_event(attempt, "payment-wins-event"),
            payload_hash=hashlib.sha256(b"payment-wins").digest(),
            planning_lead_time_minutes=30,
        )
    )
    await acquired.wait()
    freeze_task = asyncio.create_task(
        freeze_work_unit(database_session_factory, request_result.request.request_id)
    )
    await asyncio.sleep(0)
    release.set()
    event = await payment_task
    await freeze_task

    async with database_session_factory() as session:
        request = await session.get(CollectionRequest, request_result.request.request_id)
        payment = await session.get(Payment, request_result.payment.payment_id)
        outbox_count = await session.scalar(
            select(func.count(OutboxEvent.outbox_event_id)).where(
                OutboxEvent.event_type == "CollectionRequestAccepted"
            )
        )
    assert event.processing_status == EVENT_PROCESSED
    assert request is not None and request.status == REQUEST_PRE_PLANNING
    assert request.planning_batch_id is not None
    assert payment is not None and payment.successful_attempt_id == attempt.payment_attempt_id
    assert outbox_count == 1


class ExpectedRollback(Exception):
    pass


@pytest.mark.asyncio
async def test_acceptance_and_outbox_roll_back_atomically(
    database_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tirodhan.modules.payments import service as payment_service

    user = await create_user(database_session_factory)
    context = await create_context(database_session_factory, user.user_id)
    request_result = await create_request(
        database_session_factory,
        user.user_id,
        context.serviceability_context_id,
    )
    attempt, _ = await create_ready_attempt(
        database_session_factory,
        user.user_id,
        request_result.request.request_id,
        key="rollback-attempt",
        order_id="order-rollback",
    )

    async def fail_outbox(*args: Any, **kwargs: Any) -> None:
        raise ExpectedRollback

    monkeypatch.setattr(payment_service, "append_outbox_event", fail_outbox)
    with pytest.raises(ExpectedRollback):
        await process_authenticated_payment_event(
            database_session_factory,
            success_event(attempt, "rollback-event"),
            payload_hash=hashlib.sha256(b"rollback").digest(),
            planning_lead_time_minutes=30,
        )

    async with database_session_factory() as session:
        request = await session.get(CollectionRequest, request_result.request.request_id)
        payment = await session.get(Payment, request_result.payment.payment_id)
        durable_attempt = await session.get(PaymentAttempt, attempt.payment_attempt_id)
        event_count = await session.scalar(select(func.count(PaymentProviderEvent.provider)))
        outbox_count = await session.scalar(
            select(func.count(OutboxEvent.outbox_event_id)).where(
                OutboxEvent.event_type == "CollectionRequestAccepted"
            )
        )
    assert request is not None and request.status == REQUEST_PENDING_PAYMENT
    assert payment is not None and payment.status == PAYMENT_PENDING
    assert durable_attempt is not None and durable_attempt.status == ATTEMPT_PENDING
    assert event_count == 0
    assert outbox_count == 0
