"""Owner-scoped recovery reads committed binding, independently of hold eligibility."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import event, func, select, update
from test_collection_payment import ITEMS, FixedPricing, create_ready_attempt, success_event
from test_fleet_hypothesis_concurrency import overlap
from test_operational_api import _headers, _token_codec, _token_for
from test_slot_holds import command_for, configured

from tirodhan.api.routes import routing_orchestration
from tirodhan.core.config import Settings
from tirodhan.db.values import new_uuid7
from tirodhan.main import create_app
from tirodhan.modules.collection_requests import service as booking_service
from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.collection_requests.service import (
    CreateCollectionRequestCommand,
    create_collection_request,
)
from tirodhan.modules.payments.models import Payment, PaymentAttempt
from tirodhan.modules.payments.service import process_authenticated_payment_event
from tirodhan.modules.reliability.models import IdempotencyRecord
from tirodhan.modules.routing_operations.admission import bind_reservation
from tirodhan.modules.routing_operations.commands import OperationalInputError
from tirodhan.modules.routing_operations.models import RoutingManifest
from tirodhan.modules.routing_operations.orchestration_models import CapacityReservation
from tirodhan.modules.serviceability.models import ServiceabilityContext

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

HOLD_FIELDS = {
    "reservation_id",
    "admission_basis",
    "bound_request_id",
    "bookable",
    "expires_at",
    "book_by",
}


async def row_counts(factory):
    async with factory() as session:
        return {
            model: await session.scalar(select(func.count()).select_from(model))
            for model in (CollectionRequest, Payment, RoutingManifest, PaymentAttempt)
        }


def app_for(case):
    app = create_app(
        Settings(
            _env_file=None,
            environment="test",
            routing_orchestration_enabled=True,
            routing_fleet_enabled=True,
            routing_dispatch_enabled=True,
            pending_payment_lifetime_seconds=1800,
            command_idempotency_ttl_seconds=7200,
            planning_lead_time_minutes=15,
        ),
        access_token_codec=_token_codec(),
        pricing_port=FixedPricing(),
        address_protector=case.protector,
    )
    app.state.database_session_factory = case.factory
    return app


def client_for(app, token, *, transport=None):
    return httpx.AsyncClient(
        transport=transport or httpx.ASGITransport(app),
        base_url="http://api.test",
        headers=_headers(token),
    )


@pytest_asyncio.fixture
async def recovery_case(database_session_factory, monkeypatch, address_protector):
    factory = database_session_factory
    inputs, now, region = await configured(factory, monkeypatch)
    customer, original = inputs[0]
    command = await command_for(factory, region, original)
    async with factory() as session, session.begin():
        context = await session.get(ServiceabilityContext, command.context_id)
        context.address_snapshot_encrypted = await address_protector.protect("Synthetic household")
    token, _ = await _token_for(factory, customer, roles=("CUSTOMER",))
    case = SimpleNamespace(
        factory=factory,
        customer=customer,
        other=inputs[1][0],
        command=command,
        now=now,
        token=token,
        protector=address_protector,
    )
    case.app = app_for(case)
    case.hold_url = f"/v2/customer/slot-holds/{command.client_command_id}"
    case.body = {
        "client_request_id": str(new_uuid7()),
        "serviceability_context_id": str(command.context_id),
        "capacity_reservation_id": str(command.client_command_id),
        "slot_start": command.slot_start.isoformat(),
        "slot_end": command.slot_end.isoformat(),
        "items": [
            {
                "item_category_code": i.item_category_code,
                "declared_quantity": i.declared_quantity,
                "declared_weight_grams": i.declared_weight_grams,
            }
            for i in ITEMS
        ],
    }
    async with client_for(case.app, token) as client:
        held = await client.post("/v2/customer/slot-holds", json=command.model_dump(mode="json"))
        assert held.status_code == 201, held.text
        case.held = held.json()
    case.counts = await row_counts(factory)
    return case


async def book(case, client):
    response = await client.post("/v1/collection-requests", json=case.body)
    assert response.status_code == 201, response.text
    return response.json()


async def status(case, client, request_id):
    response = await client.get(case.hold_url)
    assert response.status_code == 200, response.text
    body = response.json()
    # An allow-list catches accidental household/financial disclosure, including nested payloads.
    assert set(body) == HOLD_FIELDS
    assert body == dict(case.held, bound_request_id=str(request_id), bookable=False)
    return body


async def test_unbound_hold_and_missing_or_foreign_lookup(recovery_case):
    case = recovery_case
    assert set(case.held) == HOLD_FIELDS
    assert case.held["bound_request_id"] is None and case.held["bookable"]
    async with client_for(case.app, case.token) as client:
        assert (await client.get(case.hold_url)).json() == case.held
    other_token, _ = await _token_for(case.factory, case.other, roles=("CUSTOMER",))
    async with client_for(case.app, other_token) as client:
        foreign = await client.get(case.hold_url)
        missing = await client.get(f"/v2/customer/slot-holds/{new_uuid7()}")
        assert foreign.status_code == missing.status_code == 404
        assert foreign.json() == missing.json() == {"detail": "SLOT_HOLD_UNAVAILABLE"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(case.app), base_url="http://api.test"
    ) as client:
        assert (await client.get(case.hold_url)).status_code == 401


class LoseBookingResponse(httpx.AsyncBaseTransport):
    def __init__(self, app):
        self.transport = httpx.ASGITransport(app)

    async def handle_async_request(self, request):
        response = await self.transport.handle_async_request(request)
        if request.method == "POST" and request.url.path == "/v1/collection-requests":
            assert response.status_code == 201
            await response.aclose()
            raise httpx.ReadError(
                "synthetic response loss after application commit", request=request
            )
        return response

    async def aclose(self):
        await self.transport.aclose()


async def test_response_loss_then_restart_recovers_booking_without_another_post(recovery_case):
    case = recovery_case
    async with client_for(case.app, case.token, transport=LoseBookingResponse(case.app)) as client:
        with pytest.raises(httpx.ReadError, match="response loss"):
            await client.post("/v1/collection-requests", json=case.body)
    # Fresh app/client and authenticated session retain only the durable hold identity.
    token, _ = await _token_for(case.factory, case.customer, roles=("CUSTOMER",))
    async with client_for(app_for(case), token) as client:
        recovered = await client.get(case.hold_url)
        identity = UUID(recovered.json()["bound_request_id"])
        await status(case, client, identity)
        detail = await client.get(f"/v1/customer/collection-requests/{identity}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["request_id"] == str(identity)
        assert detail.json()["status"] == "PENDING_PAYMENT"
        # Hold replay returns the same binding and never reopens permission.
        held = await client.post(
            "/v2/customer/slot-holds", json=case.command.model_dump(mode="json")
        )
        assert held.json() == recovered.json()
    counts = await row_counts(case.factory)
    assert counts == {
        model: count + (model != PaymentAttempt) for model, count in case.counts.items()
    }
    async with case.factory() as session:
        payment = await session.scalar(select(Payment).where(Payment.request_id == identity))
        assert payment.status == "PENDING" and payment.successful_attempt_id is None
    other_token, _ = await _token_for(case.factory, case.other, roles=("CUSTOMER",))
    async with client_for(case.app, other_token) as client:
        assert (await client.get(case.hold_url)).status_code == 404
        assert (await client.get(f"/v1/customer/collection-requests/{identity}")).status_code == 404


@pytest.mark.parametrize("elapsed", ["book_by", "expires_at", "slot_end"])
async def test_binding_and_exact_replay_survive_time_and_configuration_changes(
    recovery_case, monkeypatch, elapsed
):
    case = recovery_case
    async with client_for(case.app, case.token) as client:
        original = await book(case, client)
        async with case.factory() as session:
            reservation = await session.get(CapacityReservation, case.command.client_command_id)
        later = {
            "book_by": reservation.expires_at - timedelta(minutes=30) + timedelta(seconds=1),
            "expires_at": reservation.expires_at + timedelta(seconds=1),
            "slot_end": case.command.slot_end + timedelta(days=1),
        }[elapsed]
        monkeypatch.setattr(routing_orchestration, "utc_now", lambda: later)
        monkeypatch.setattr(booking_service, "utc_now", lambda: later)

        def forbidden(*args, **kwargs):
            pytest.fail("completed replay invoked fresh slot validation")

        class NoPricing:
            async def quote(self, items):
                pytest.fail("completed replay invoked current pricing")

        monkeypatch.setattr(booking_service, "validate_slot", forbidden)
        case.app.state.pricing_port = NoPricing()
        await status(case, client, original["request_id"])
        assert await book(case, client) == original
        changed = dict(case.body, items=[dict(case.body["items"][0], declared_quantity=2)])
        assert (await client.post("/v1/collection-requests", json=changed)).status_code == 409


@pytest.mark.parametrize("after_expiry", [False, True])
async def test_unbound_expired_hold_never_fabricates_booking(
    recovery_case, monkeypatch, after_expiry
):
    case = recovery_case
    async with case.factory() as session:
        row = await session.get(CapacityReservation, case.command.client_command_id)
    later = row.expires_at + timedelta(seconds=1)
    if not after_expiry:
        later -= timedelta(minutes=30)
    monkeypatch.setattr(routing_orchestration, "utc_now", lambda: later)
    async with client_for(case.app, case.token) as client:
        response = await client.get(case.hold_url)
        assert response.json() == dict(case.held, bookable=False)
    assert await row_counts(case.factory) == case.counts


@pytest.mark.parametrize("terminal", ["CANCELLED", "COMPLETED"])
async def test_binding_survives_later_booking_state(recovery_case, terminal):
    case = recovery_case
    async with client_for(case.app, case.token) as client:
        original = await book(case, client)
        identity = UUID(original["request_id"])
        if terminal == "CANCELLED":
            cancelled = await client.post(
                f"/v1/collection-requests/{identity}/cancel",
                headers={"Idempotency-Key": str(new_uuid7())},
            )
            assert cancelled.status_code == 200, cancelled.text
        else:
            attempt, _ = await create_ready_attempt(
                case.factory,
                case.customer,
                identity,
                key=str(new_uuid7()),
                order_id=str(new_uuid7()),
            )
            await process_authenticated_payment_event(
                case.factory,
                success_event(attempt, str(new_uuid7())),
                payload_hash=b"synthetic-recovery-capture",
                planning_lead_time_minutes=15,
                idempotency_expires_at=case.now + timedelta(hours=2),
            )
            await status(case, client, identity)
            # Fixture advances fulfilment to a terminal persisted state. Completion/evidence
            # authorization is independently covered by test_collection_request_completion.py.
            async with case.factory() as session, session.begin():
                await session.execute(
                    update(CollectionRequest)
                    .where(CollectionRequest.request_id == identity)
                    .values(status="COMPLETED", completed_at=case.now + timedelta(hours=1))
                )
        await status(case, client, identity)
        detail = await client.get(f"/v1/customer/collection-requests/{identity}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["status"] == terminal
        replay = await book(case, client)
        assert replay["request_id"] == str(identity) and replay["status"] == terminal


@pytest.mark.parametrize("same_command", [False, True])
async def test_simultaneous_booking_bindings_have_one_durable_result(recovery_case, same_command):
    case = recovery_case

    async def submit(body):
        async with client_for(case.app, case.token) as client:
            return await client.post("/v1/collection-requests", json=body)

    second = dict(case.body)
    if not same_command:
        second["client_request_id"] = str(new_uuid7())
    results = await overlap(
        case.factory,
        CapacityReservation,
        case.command.client_command_id,
        [lambda: submit(case.body), lambda: submit(second)],
    )
    assert all(isinstance(r, httpx.Response) for r in results), results
    assert sorted(r.status_code for r in results) == ([201, 201] if same_command else [201, 409])
    winner = next(r.json()["request_id"] for r in results if r.status_code == 201)
    if same_command:
        assert results[0].json() == results[1].json()
    async with client_for(case.app, case.token) as client:
        await status(case, client, winner)
    assert await row_counts(case.factory) == {
        model: count + (model != PaymentAttempt) for model, count in case.counts.items()
    }


async def test_consumed_reservation_cannot_be_rebound(recovery_case):
    case = recovery_case
    async with client_for(case.app, case.token) as client:
        original = await book(case, client)
        async with case.factory() as session:
            committed = await session.get(CollectionRequest, UUID(original["request_id"]))
            candidate = CollectionRequest(
                **{
                    column.name: getattr(committed, column.name)
                    for column in CollectionRequest.__table__.columns
                    if column.name != "request_id"
                },
                request_id=new_uuid7(),
            )
        # The guard rejects even before a second request's FK/location/manifest writes.
        with pytest.raises(OperationalInputError, match="CAPACITY_RESERVATION_ALREADY_USED"):
            async with case.factory() as session, session.begin():
                await bind_reservation(session, candidate, case.command.client_command_id)
        await status(case, client, original["request_id"])


async def test_crash_after_binding_rolls_back_and_exact_retry_can_commit(recovery_case):
    case = recovery_case
    engine = case.factory.kw["bind"].sync_engine
    binding_writes = []

    def executed(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("UPDATE regional_capacity_reservation"):
            binding_writes.append(statement)
            raise RuntimeError("synthetic crash after reservation binding")

    event.listen(engine, "after_cursor_execute", executed)
    try:
        async with client_for(case.app, case.token) as client:
            with pytest.raises(RuntimeError, match="synthetic crash"):
                await client.post("/v1/collection-requests", json=case.body)
    finally:
        event.remove(engine, "after_cursor_execute", executed)
    assert len(binding_writes) == 1
    assert await row_counts(case.factory) == case.counts
    async with case.factory() as session:
        row = await session.get(CapacityReservation, case.command.client_command_id)
        assert row.request_id is None
        assert (
            await session.scalar(
                select(func.count())
                .select_from(IdempotencyRecord)
                .where(IdempotencyRecord.scope == f"collection-request.create:{case.customer}")
            )
            == 0
        )
    async with client_for(case.app, case.token) as client:
        assert (await client.get(case.hold_url)).json() == case.held
        result = await book(case, client)
        await status(case, client, result["request_id"])


async def test_uncommitted_binding_stays_ambiguous_until_commit(recovery_case, monkeypatch):
    case = recovery_case
    entered, release = asyncio.Event(), asyncio.Event()

    async def paused_binding(session, request, reservation_id):
        await bind_reservation(session, request, reservation_id)
        await session.flush()
        entered.set()
        await release.wait()

    monkeypatch.setattr(
        "tirodhan.modules.routing_operations.admission.bind_reservation", paused_binding
    )
    async with client_for(case.app, case.token) as writer:
        pending = asyncio.create_task(book(case, writer))
        try:
            await asyncio.wait_for(entered.wait(), 10)
            async with client_for(app_for(case), case.token) as reader:
                assert (await reader.get(case.hold_url)).json() == case.held
                release.set()
                committed = await asyncio.wait_for(pending, 10)
                await status(case, reader, committed["request_id"])
        finally:
            release.set()
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


async def test_expired_reservation_rejects_fresh_booking_without_false_binding(recovery_case):
    case = recovery_case
    # Keep the hold valid now, but insufficient for this fresh booking's payment window.
    command = CreateCollectionRequestCommand(
        customer_id=case.customer,
        client_request_id=UUID(case.body["client_request_id"]),
        serviceability_context_id=case.command.context_id,
        slot_start=case.command.slot_start,
        slot_end=case.command.slot_end,
        items=ITEMS,
        payment_expires_at=case.now + timedelta(hours=2),
        capacity_reservation_id=case.command.client_command_id,
    )
    with pytest.raises(OperationalInputError, match="CAPACITY_RESERVATION_EXPIRED"):
        await create_collection_request(
            case.factory,
            command,
            FixedPricing(),
            idempotency_expires_at=case.now + timedelta(hours=3),
            require_capacity_reservation=True,
            planning_lead_time_minutes=15,
        )
    async with client_for(case.app, case.token) as client:
        assert (await client.get(case.hold_url)).json() == case.held
    # A failed command consumes neither its idempotency key nor its context.
    result = await create_collection_request(
        case.factory,
        replace(command, payment_expires_at=case.now + timedelta(minutes=30)),
        FixedPricing(),
        idempotency_expires_at=case.now + timedelta(hours=3),
        require_capacity_reservation=True,
        planning_lead_time_minutes=15,
    )
    assert result.request.status == "PENDING_PAYMENT"
