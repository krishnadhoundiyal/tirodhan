from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from test_operational_api import _token_codec, _token_for

from tirodhan.core.config import Settings
from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.main import create_app
from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.collection_requests.ports import (
    DeclaredRequestItem,
    PricingQuote,
    QuotedRequestItem,
)
from tirodhan.modules.collection_requests.service import (
    CreateCollectionRequestCommand,
    ServiceabilityContextIneligibleError,
    create_collection_request,
)
from tirodhan.modules.customers.ports import AddressProtector
from tirodhan.modules.customers.service import GeoPoint
from tirodhan.modules.identity.models import AppUser, RefreshSession, UserRole
from tirodhan.modules.reliability.models import InboxMessage, OutboxEvent
from tirodhan.modules.reliability.primitives import append_outbox_event, claim_inbox_message
from tirodhan.modules.reliability.publisher import RoutedMessage, publish_outbox_batch
from tirodhan.modules.serviceability.consumer import (
    CONSUMER_NAME,
    handle_serviceability_delivery,
    process_serviceability_message,
)
from tirodhan.modules.serviceability.h3_cells import H3CellIdDeriver
from tirodhan.modules.serviceability.models import ServiceabilityContext
from tirodhan.modules.serviceability.ports import LocationResolution, LocationResolutionStatus
from tirodhan.modules.serviceability.service import (
    CreateServiceabilityContextCommand,
    create_serviceability_context,
    resolve_serviceability,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
POINT = GeoPoint(28.6139, 77.209)


class TwoCallerBarrier:
    def __init__(self) -> None:
        self.arrivals = 0
        self.ready = asyncio.Event()

    async def wait(self) -> None:
        self.arrivals += 1
        if self.arrivals == 2:
            self.ready.set()
        await self.ready.wait()


async def context_fixture(
    factory: async_sessionmaker[AsyncSession], protector: AddressProtector
) -> ServiceabilityContext:
    async with factory() as session, session.begin():
        user = AppUser(status="ACTIVE")
        session.add(user)
        await session.flush()
        return await create_serviceability_context(
            session,
            CreateServiceabilityContextCommand(
                user_id=user.user_id,
                idempotency_key=str(new_uuid7()),
                one_off_address="synthetic immutable household",
                location=POINT,
                expires_at=utc_now() + timedelta(hours=1),
            ),
            protector,
            idempotency_expires_at=utc_now() + timedelta(hours=1),
        )


class Resolver:
    def __init__(
        self,
        engine: AsyncEngine,
        status: LocationResolutionStatus = LocationResolutionStatus.RESOLVED,
    ) -> None:
        self.engine = engine
        self.status = status
        self.calls = 0
        self.barrier: TwoCallerBarrier | None = None
        self.on_call: Any = None

    async def resolve(
        self, *, address: str, supplied_location: GeoPoint | None
    ) -> LocationResolution:
        self.calls += 1
        if self.barrier is None:
            assert self.engine.pool.checkedout() == 0
            async with self.engine.connect() as connection:
                held = await connection.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
                        "AND pid <> pg_backend_pid() AND state LIKE '%transaction%'"
                    )
                )
                assert held == 0
        if self.barrier:
            await asyncio.wait_for(self.barrier.wait(), timeout=5)
        if self.on_call:
            await self.on_call()
        return LocationResolution(
            self.status,
            POINT if self.status == LocationResolutionStatus.RESOLVED else None,
            None if self.status == LocationResolutionStatus.RESOLVED else "TEST_FAILURE",
        )


class Pricing:
    def __init__(self, resolver: Resolver) -> None:
        self.resolver = resolver
        self.calls = 0
        self.on_quote: Any = None

    async def quote(self, items: Sequence[DeclaredRequestItem]) -> PricingQuote:
        self.calls += 1
        if self.on_quote:
            await self.on_quote()
        return PricingQuote(100, "INR", tuple(QuotedRequestItem(100, "test") for _ in items))


def command(context: ServiceabilityContext) -> CreateCollectionRequestCommand:
    start = utc_now() + timedelta(days=1)
    return CreateCollectionRequestCommand(
        context.user_id,
        new_uuid7(),
        context.serviceability_context_id,
        start,
        start + timedelta(minutes=30),
        (DeclaredRequestItem("TEST", 1),),
        utc_now() + timedelta(minutes=30),
    )


async def booking(
    factory: async_sessionmaker[AsyncSession],
    cmd: CreateCollectionRequestCommand,
    protector: AddressProtector,
    resolver: Resolver,
    pricing: Pricing,
) -> Any:
    return await create_collection_request(
        factory,
        cmd,
        pricing,
        idempotency_expires_at=utc_now() + timedelta(hours=1),
        protector=protector,
        location_resolver=resolver,
        cell_id_deriver=H3CellIdDeriver(),
    )


async def process(
    factory: async_sessionmaker[AsyncSession],
    protector: AddressProtector,
    resolver: Resolver,
    context_id: UUID,
    message_id: str,
) -> None:
    await process_serviceability_message(
        factory,
        message_id=message_id,
        message_type="ServiceabilityRequested",
        body=json.dumps({"serviceability_context_id": str(context_id)}).encode(),
        protector=protector,
        location_resolver=resolver,
        cell_id_deriver=H3CellIdDeriver(),
    )


async def test_checkout_pending_fallback_then_exact_replay_no_provider_or_pricing(
    database_engine: AsyncEngine,
    database_session_factory: async_sessionmaker[AsyncSession],
    address_protector: AddressProtector,
) -> None:
    context = await context_fixture(database_session_factory, address_protector)
    resolver = Resolver(database_engine)
    pricing = Pricing(resolver)

    async def pricing_after_resolution() -> None:
        async with database_session_factory() as session:
            current = await session.get(ServiceabilityContext, context.serviceability_context_id)
            assert current.status == "SERVICEABLE"

    pricing.on_quote = pricing_after_resolution
    cmd = command(context)
    result = await booking(database_session_factory, cmd, address_protector, resolver, pricing)
    replay = await booking(database_session_factory, cmd, address_protector, resolver, pricing)
    assert result.request.request_id == replay.request.request_id
    assert resolver.calls == pricing.calls == 1
    assert result.request.pickup_address_snapshot_encrypted == context.address_snapshot_encrypted
    assert result.request.cell_id == await H3CellIdDeriver().derive(POINT)


@pytest.mark.parametrize(
    "status", [LocationResolutionStatus.UNSERVICEABLE, LocationResolutionStatus.TECHNICAL_FAILURE]
)
async def test_terminal_failure_blocks_checkout_no_cell_no_pricing_and_replay_no_provider(
    database_engine: AsyncEngine,
    database_session_factory: async_sessionmaker[AsyncSession],
    address_protector: AddressProtector,
    status: LocationResolutionStatus,
) -> None:
    context = await context_fixture(database_session_factory, address_protector)
    resolver = Resolver(database_engine, status)
    pricing = Pricing(resolver)
    for _ in range(2):
        with pytest.raises(ServiceabilityContextIneligibleError):
            await booking(
                database_session_factory, command(context), address_protector, resolver, pricing
            )
    assert resolver.calls == 1
    assert pricing.calls == 0
    async with database_session_factory() as session:
        persisted = await session.get(ServiceabilityContext, context.serviceability_context_id)
        assert persisted.cell_id is None


@pytest.mark.parametrize("invalid", ["expired", "wrong_owner"])
async def test_checkout_rejects_before_provider(
    database_engine: AsyncEngine,
    database_session_factory: async_sessionmaker[AsyncSession],
    address_protector: AddressProtector,
    invalid: str,
) -> None:
    from dataclasses import replace

    context = await context_fixture(database_session_factory, address_protector)
    cmd = command(context)
    if invalid == "expired":
        async with database_session_factory() as session, session.begin():
            await session.execute(
                update(ServiceabilityContext).values(expires_at=utc_now() - timedelta(seconds=1))
            )
    else:
        cmd = replace(cmd, customer_id=new_uuid7())
    resolver = Resolver(database_engine)
    pricing = Pricing(resolver)
    with pytest.raises(ServiceabilityContextIneligibleError):
        await booking(database_session_factory, cmd, address_protector, resolver, pricing)
    assert resolver.calls == pricing.calls == 0


async def test_checkout_final_transaction_revalidates_after_pricing(
    database_engine: AsyncEngine,
    database_session_factory: async_sessionmaker[AsyncSession],
    address_protector: AddressProtector,
) -> None:
    context = await context_fixture(database_session_factory, address_protector)
    resolver = Resolver(database_engine)
    pricing = Pricing(resolver)

    async def expire_context() -> None:
        async with database_session_factory() as session, session.begin():
            await session.execute(
                update(ServiceabilityContext).values(expires_at=utc_now() - timedelta(seconds=1))
            )

    pricing.on_quote = expire_context
    with pytest.raises(ServiceabilityContextIneligibleError):
        await booking(
            database_session_factory, command(context), address_protector, resolver, pricing
        )
    async with database_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(CollectionRequest)) == 0


async def test_worker_checkout_collision_converges_and_processed_redelivery_skips_provider(
    database_engine: AsyncEngine,
    database_session_factory: async_sessionmaker[AsyncSession],
    address_protector: AddressProtector,
) -> None:
    context = await context_fixture(database_session_factory, address_protector)
    resolver = Resolver(database_engine)
    resolver.barrier = TwoCallerBarrier()
    message_id = str(new_uuid7())
    _, result = await asyncio.wait_for(
        asyncio.gather(
            process(
                database_session_factory,
                address_protector,
                resolver,
                context.serviceability_context_id,
                message_id,
            ),
            booking(
                database_session_factory,
                command(context),
                address_protector,
                resolver,
                Pricing(resolver),
            ),
        ),
        timeout=10,
    )
    assert result.request.status == "PENDING_PAYMENT"
    assert resolver.calls == 2
    await process(
        database_session_factory,
        address_protector,
        resolver,
        context.serviceability_context_id,
        message_id,
    )
    assert resolver.calls == 2
    async with database_session_factory() as session:
        inbox = await session.get(InboxMessage, (CONSUMER_NAME, message_id))
        assert inbox.status == "PROCESSED"
        assert inbox.business_key == str(context.serviceability_context_id)
        persisted = await session.get(ServiceabilityContext, context.serviceability_context_id)
        assert persisted.cell_id == result.request.cell_id


async def test_processing_inbox_redelivery_resumes_without_new_domain_effect(
    database_engine: AsyncEngine,
    database_session_factory: async_sessionmaker[AsyncSession],
    address_protector: AddressProtector,
) -> None:
    context = await context_fixture(database_session_factory, address_protector)
    message_id = str(new_uuid7())
    async with database_session_factory() as session, session.begin():
        await claim_inbox_message(
            session,
            consumer_name=CONSUMER_NAME,
            message_id=message_id,
            message_type="ServiceabilityRequested",
            business_key=str(context.serviceability_context_id),
        )
    resolver = Resolver(database_engine)
    await resolve_serviceability(
        database_session_factory,
        context_id=context.serviceability_context_id,
        protector=address_protector,
        location_resolver=resolver,
        cell_id_deriver=H3CellIdDeriver(),
    )
    async with database_session_factory() as session:
        before = await session.get(ServiceabilityContext, context.serviceability_context_id)
        resolved_at = before.resolved_at
    # Simulate crash after terminal commit, before inbox completion/settlement.
    await process(
        database_session_factory,
        address_protector,
        resolver,
        context.serviceability_context_id,
        message_id,
    )
    async with database_session_factory() as session:
        after = await session.get(ServiceabilityContext, context.serviceability_context_id)
        inbox = await session.get(InboxMessage, (CONSUMER_NAME, message_id))
        assert inbox.status == "PROCESSED"
        assert after.resolved_at == resolved_at
    assert resolver.calls == 1


class Delivery:
    def __init__(self, context_id: UUID, factory: async_sessionmaker[AsyncSession]) -> None:
        self.message_id = str(new_uuid7())
        self.message_type = "ServiceabilityRequested"
        self.body = json.dumps({"serviceability_context_id": str(context_id)}).encode()
        self.factory = factory
        self.completed = self.abandoned = self.dead_lettered = 0
        self.crash_on_complete = False
        self.delivery_count = 900  # Transport metadata is not a business attempt.

    async def complete(self) -> None:
        async with self.factory() as session:
            inbox = await session.get(InboxMessage, (CONSUMER_NAME, self.message_id))
            assert inbox.status == "PROCESSED"
        if self.crash_on_complete:
            raise RuntimeError("simulated settlement failure")
        self.completed += 1

    async def abandon(self) -> None:
        self.abandoned += 1

    async def dead_letter(self) -> None:
        self.dead_lettered += 1


async def test_commit_before_settlement_crash_redelivery_no_provider(
    database_engine: AsyncEngine,
    database_session_factory: async_sessionmaker[AsyncSession],
    address_protector: AddressProtector,
) -> None:
    context = await context_fixture(database_session_factory, address_protector)
    delivery = Delivery(context.serviceability_context_id, database_session_factory)
    resolver = Resolver(database_engine)
    delivery.crash_on_complete = True
    with pytest.raises(RuntimeError):
        await handle_serviceability_delivery(
            delivery,
            database_session_factory,
            protector=address_protector,
            location_resolver=resolver,
            cell_id_deriver=H3CellIdDeriver(),
        )
    delivery.crash_on_complete = False
    await handle_serviceability_delivery(
        delivery,
        database_session_factory,
        protector=address_protector,
        location_resolver=resolver,
        cell_id_deriver=H3CellIdDeriver(),
    )
    assert delivery.completed == resolver.calls == 1


@pytest.mark.parametrize("malformed", ["pii", "type", "id", "large"])
async def test_invalid_delivery_rejected_without_inbox_or_domain_mutation(
    database_engine: AsyncEngine,
    database_session_factory: async_sessionmaker[AsyncSession],
    address_protector: AddressProtector,
    malformed: str,
) -> None:
    context = await context_fixture(database_session_factory, address_protector)
    delivery = Delivery(context.serviceability_context_id, database_session_factory)
    if malformed == "pii":
        delivery.body = b'{"address":"private household"}'
    elif malformed == "type":
        delivery.message_type = "UnregisteredEvent"
    elif malformed == "id":
        delivery.message_id = "phone-like-private-input"
    else:
        delivery.body = b"x" * 1000
    resolver = Resolver(database_engine)
    await handle_serviceability_delivery(
        delivery,
        database_session_factory,
        protector=address_protector,
        location_resolver=resolver,
        cell_id_deriver=H3CellIdDeriver(),
    )
    assert delivery.dead_lettered == 1
    assert resolver.calls == 0
    async with database_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(InboxMessage)) == 0
        current = await session.get(ServiceabilityContext, context.serviceability_context_id)
        assert current.status == "PENDING"


class Publisher:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self.messages: list[tuple[str, RoutedMessage]] = []
        self.fail = False
        self.barrier: TwoCallerBarrier | None = None
        self.first_send = asyncio.Event()

    async def send(self, entity: str, message: RoutedMessage) -> None:
        if self.barrier is None:
            assert self.engine.pool.checkedout() == 0
        self.messages.append((entity, message))
        self.first_send.set()
        if self.barrier:
            await asyncio.wait_for(self.barrier.wait(), timeout=5)
        if self.fail:
            raise RuntimeError("simulated send failure/lost acknowledgement")


async def test_explicit_outbox_routing_stable_ids_failure_then_resend_and_publication(
    database_engine: AsyncEngine,
    database_session_factory: async_sessionmaker[AsyncSession],
    address_protector: AddressProtector,
) -> None:
    context = await context_fixture(database_session_factory, address_protector)
    async with database_session_factory() as session, session.begin():
        unrelated = await append_outbox_event(
            session,
            event_key="unregistered",
            aggregate_type="request",
            aggregate_id=new_uuid7(),
            event_type="CollectionRequestAccepted",
            payload={"request_id": str(new_uuid7())},
        )
        future = await append_outbox_event(
            session,
            event_key="future",
            aggregate_type="serviceability_context",
            aggregate_id=context.serviceability_context_id,
            event_type="ServiceabilityRequested",
            payload={"serviceability_context_id": str(context.serviceability_context_id)},
            available_at=utc_now() + timedelta(hours=1),
        )
    publisher = Publisher(database_engine)
    publisher.fail = True
    assert (
        await publish_outbox_batch(
            database_session_factory,
            publisher,
            serviceability_entity="serviceability",
            batch_size=5,
        )
        == 0
    )
    async with database_session_factory() as session:
        event = await session.scalar(
            select(OutboxEvent).where(OutboxEvent.event_key.like("serviceability-requested:%"))
        )
        assert event.status == "PENDING"
        assert event.publish_attempt_count == 1
    publisher.fail = False
    assert (
        await publish_outbox_batch(
            database_session_factory,
            publisher,
            serviceability_entity="serviceability",
            batch_size=5,
        )
        == 1
    )
    assert publisher.messages[0] == publisher.messages[1]
    entity, message = publisher.messages[0]
    assert entity == "serviceability"
    assert message.message_id == str(event.outbox_event_id)
    assert json.loads(message.body) == {
        "serviceability_context_id": str(context.serviceability_context_id)
    }
    async with database_session_factory() as session:
        published = await session.get(OutboxEvent, event.outbox_event_id)
        assert published.status == "PUBLISHED"
        assert published.publish_attempt_count == 2
        assert (await session.get(OutboxEvent, unrelated.outbox_event_id)).status == "PENDING"
        assert (await session.get(OutboxEvent, future.outbox_event_id)).publish_attempt_count == 0
    assert (
        await publish_outbox_batch(
            database_session_factory,
            publisher,
            serviceability_entity="serviceability",
            batch_size=5,
        )
        == 0
    )


async def test_checkout_api_pending_fallback_and_replay_without_header(
    database_engine: AsyncEngine,
    database_session_factory: async_sessionmaker[AsyncSession],
    migrated_database_url: str,
    address_protector: AddressProtector,
) -> None:
    context = await context_fixture(database_session_factory, address_protector)
    resolver = Resolver(database_engine)
    pricing = Pricing(resolver)
    token, _ = await _token_for(database_session_factory, context.user_id, roles=("CUSTOMER",))
    app = create_app(
        Settings(
            _env_file=None,
            environment="test",
            database_url=migrated_database_url,
            command_idempotency_ttl_seconds=3600,
            pending_payment_lifetime_seconds=900,
        ),
        address_protector=address_protector,
        location_resolver=resolver,
        pricing_port=pricing,
        access_token_codec=_token_codec(),
    )
    cmd = command(context)
    body = {
        "client_request_id": str(cmd.client_request_id),
        "serviceability_context_id": str(context.serviceability_context_id),
        "slot_start": cmd.slot_start.isoformat(),
        "slot_end": cmd.slot_end.isoformat(),
        "items": [{"item_category_code": "TEST", "declared_quantity": 1}],
    }
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        created = await client.post(
            "/v1/collection-requests", json=body, headers={"Authorization": f"Bearer {token}"}
        )
        replay = await client.post(
            "/v1/collection-requests", json=body, headers={"Authorization": f"Bearer {token}"}
        )
    assert created.status_code == replay.status_code == 201
    assert created.json() == replay.json()
    assert resolver.calls == pricing.calls == 1


async def test_send_then_mark_crash_resends_same_durable_transport_id(
    database_engine: AsyncEngine,
    database_session_factory: async_sessionmaker[AsyncSession],
    address_protector: AddressProtector,
) -> None:
    await context_fixture(database_session_factory, address_protector)
    publisher = Publisher(database_engine)

    def fail_mark(
        connection: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if statement.startswith("UPDATE outbox_event") and "published_at" in statement:
            raise RuntimeError("simulated crash after send")

    sqlalchemy_event.listen(database_engine.sync_engine, "before_cursor_execute", fail_mark)
    try:
        with pytest.raises(RuntimeError):
            await publish_outbox_batch(
                database_session_factory,
                publisher,
                serviceability_entity="serviceability",
                batch_size=1,
            )
    finally:
        sqlalchemy_event.remove(database_engine.sync_engine, "before_cursor_execute", fail_mark)
    async with database_session_factory() as session:
        persisted = await session.scalar(select(OutboxEvent))
        assert persisted.status == "PENDING"
    assert (
        await publish_outbox_batch(
            database_session_factory,
            publisher,
            serviceability_entity="serviceability",
            batch_size=1,
        )
        == 1
    )
    assert len(publisher.messages) == 2
    assert publisher.messages[0] == publisher.messages[1]


async def test_overlapping_publisher_jobs_and_duplicate_deliveries_one_business_effect(
    database_engine: AsyncEngine,
    database_session_factory: async_sessionmaker[AsyncSession],
    address_protector: AddressProtector,
) -> None:
    context = await context_fixture(database_session_factory, address_protector)
    publisher = Publisher(database_engine)
    publisher.barrier = TwoCallerBarrier()
    # The second finite execution starts after the first read transaction has
    # committed and reached send, guaranteeing the send-before-mark overlap.
    first = asyncio.create_task(
        publish_outbox_batch(
            database_session_factory,
            publisher,
            serviceability_entity="serviceability",
            batch_size=1,
        )
    )

    await asyncio.wait_for(publisher.first_send.wait(), timeout=5)
    second = asyncio.create_task(
        publish_outbox_batch(
            database_session_factory,
            publisher,
            serviceability_entity="serviceability",
            batch_size=1,
        )
    )
    await asyncio.wait_for(asyncio.gather(first, second), timeout=10)
    assert len(publisher.messages) == 2
    assert publisher.messages[0] == publisher.messages[1]
    resolver = Resolver(database_engine)
    resolver.barrier = TwoCallerBarrier()
    await asyncio.wait_for(
        asyncio.gather(
            *(
                process(
                    database_session_factory,
                    address_protector,
                    resolver,
                    context.serviceability_context_id,
                    message.message_id,
                )
                for _, message in publisher.messages
            )
        ),
        timeout=10,
    )
    async with database_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(InboxMessage)) == 1
        inbox = await session.scalar(select(InboxMessage))
        assert inbox.status == "PROCESSED"
        persisted = await session.get(ServiceabilityContext, context.serviceability_context_id)
        assert persisted.status == "SERVICEABLE"
        assert persisted.resolved_at is not None


async def test_worker_first_unserviceable_wins_over_inflight_checkout_resolution(
    database_engine: AsyncEngine,
    database_session_factory: async_sessionmaker[AsyncSession],
    address_protector: AddressProtector,
) -> None:
    context = await context_fixture(database_session_factory, address_protector)
    fallback_started = asyncio.Event()
    release_fallback = asyncio.Event()
    worker = Resolver(database_engine, LocationResolutionStatus.UNSERVICEABLE)
    fallback = Resolver(database_engine)
    pricing = Pricing(fallback)

    async def hold_fallback_provider() -> None:
        fallback_started.set()
        await asyncio.wait_for(release_fallback.wait(), timeout=5)

    fallback.on_call = hold_fallback_provider
    task = asyncio.create_task(
        booking(database_session_factory, command(context), address_protector, fallback, pricing)
    )
    await asyncio.wait_for(fallback_started.wait(), timeout=5)
    try:
        await process(
            database_session_factory,
            address_protector,
            worker,
            context.serviceability_context_id,
            str(new_uuid7()),
        )
    finally:
        release_fallback.set()
    with pytest.raises(ServiceabilityContextIneligibleError):
        await asyncio.wait_for(task, timeout=5)
    assert pricing.calls == 0
    async with database_session_factory() as session:
        persisted = await session.get(ServiceabilityContext, context.serviceability_context_id)
        assert persisted.status == "UNSERVICEABLE"
        assert persisted.cell_id is None


async def test_serviceable_checkout_and_terminal_resolution_replay_never_call_provider(
    database_engine: AsyncEngine,
    database_session_factory: async_sessionmaker[AsyncSession],
    address_protector: AddressProtector,
) -> None:
    context = await context_fixture(database_session_factory, address_protector)
    resolver = Resolver(database_engine)
    resolved = await resolve_serviceability(
        database_session_factory,
        context_id=context.serviceability_context_id,
        protector=address_protector,
        location_resolver=resolver,
        cell_id_deriver=H3CellIdDeriver(),
    )

    async def fail_if_provider_called() -> None:
        raise AssertionError("terminal replay must not call provider")

    resolver.on_call = fail_if_provider_called
    replay = await resolve_serviceability(
        database_session_factory,
        context_id=context.serviceability_context_id,
        protector=address_protector,
        location_resolver=resolver,
        cell_id_deriver=H3CellIdDeriver(),
    )
    result = await booking(
        database_session_factory, command(context), address_protector, resolver, Pricing(resolver)
    )
    assert resolver.calls == 1
    assert resolved.resolved_at == replay.resolved_at
    assert result.request.cell_id == resolved.cell_id


@pytest.mark.parametrize(
    "change,expected",
    [
        ("disable_user", 401),
        ("revoke_session", 401),
        ("expire_session", 401),
        ("revoke_role", 403),
        ("malformed_jwt", 401),
        ("expired_jwt", 401),
    ],
)
async def test_checkout_live_authorization_on_next_request_before_google(
    database_engine: AsyncEngine,
    database_session_factory: async_sessionmaker[AsyncSession],
    migrated_database_url: str,
    address_protector: AddressProtector,
    change: str,
    expected: int,
) -> None:
    context = await context_fixture(database_session_factory, address_protector)
    token, session_id = await _token_for(
        database_session_factory, context.user_id, roles=("CUSTOMER",)
    )
    resolver = Resolver(database_engine)
    pricing = Pricing(resolver)
    app = create_app(
        Settings(
            _env_file=None,
            environment="test",
            database_url=migrated_database_url,
            command_idempotency_ttl_seconds=3600,
            pending_payment_lifetime_seconds=900,
        ),
        address_protector=address_protector,
        location_resolver=resolver,
        pricing_port=pricing,
        access_token_codec=_token_codec(),
    )
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        headers = {"Authorization": f"Bearer {token}"}
        assert (
            await client.get(
                f"/v1/serviceability/contexts/{context.serviceability_context_id}", headers=headers
            )
        ).status_code == 200
        async with database_session_factory() as session, session.begin():
            if change == "disable_user":
                await session.execute(
                    update(AppUser)
                    .where(AppUser.user_id == context.user_id)
                    .values(status="DISABLED")
                )
            elif change == "revoke_session":
                await session.execute(
                    update(RefreshSession)
                    .where(RefreshSession.refresh_session_id == session_id)
                    .values(revoked_at=utc_now())
                )
            elif change == "expire_session":
                await session.execute(
                    update(RefreshSession)
                    .where(RefreshSession.refresh_session_id == session_id)
                    .values(
                        created_at=utc_now() - timedelta(hours=2),
                        expires_at=utc_now() - timedelta(seconds=1),
                    )
                )
            elif change == "revoke_role":
                await session.execute(
                    update(UserRole)
                    .where(UserRole.user_id == context.user_id, UserRole.role_code == "CUSTOMER")
                    .values(revoked_at=utc_now())
                )
        if change == "malformed_jwt":
            headers = {"Authorization": "Bearer malformed"}
        elif change == "expired_jwt":
            expired = _token_codec().issue(
                user_id=context.user_id,
                refresh_session_id=session_id,
                issued_at=utc_now() - timedelta(minutes=2),
                ttl_seconds=1,
            )
            headers = {"Authorization": f"Bearer {expired}"}
        cmd = command(context)
        response = await client.post(
            "/v1/collection-requests",
            headers=headers,
            json={
                "client_request_id": str(cmd.client_request_id),
                "serviceability_context_id": str(context.serviceability_context_id),
                "slot_start": cmd.slot_start.isoformat(),
                "slot_end": cmd.slot_end.isoformat(),
                "items": [{"item_category_code": "TEST", "declared_quantity": 1}],
            },
        )
        assert response.status_code == expected
    assert resolver.calls == pricing.calls == 0
