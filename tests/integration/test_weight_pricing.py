"""Real policy/booking transactions, synthetic operational loads, no approved customer volume."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from alembic import command as alembic_command
from alembic.config import Config
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from test_collection_payment import create_context, create_ready_attempt, create_user, success_event
from test_customer_mobile_core import app_for, seed_catalogue
from test_fleet_hypothesis_concurrency import overlap
from test_manager_business import change, manager
from test_payment_preparation import assert_readiness_schema
from test_slot_holds import command_for, configured

from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.business_admin.commands import WeightPriceEdit
from tirodhan.modules.business_admin.history import current_version, versions
from tirodhan.modules.business_admin.models import BookingWeightQuote, WeightPricingPolicy
from tirodhan.modules.business_admin.weight_contracts import (
    WeightItemInput,
    WeightQuoteConfirmation,
)
from tirodhan.modules.business_admin.weight_pricing import (
    ConfiguredPickupPricing,
    WeightPricingConflict,
)
from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.collection_requests.ports import (
    DeclaredRequestItem,
    PricingNotConfiguredError,
)
from tirodhan.modules.collection_requests.service import (
    CreateCollectionRequestCommand,
    create_collection_request,
)
from tirodhan.modules.payments.models import Payment
from tirodhan.modules.payments.service import process_authenticated_payment_event
from tirodhan.modules.routing_operations.commands import OperationalInputError
from tirodhan.modules.routing_operations.models import RoutingManifest
from tirodhan.modules.routing_operations.orchestration_models import CapacityReservation
from tirodhan.modules.routing_operations.slot_holds import hold_slot

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def policy_command(**changes):
    # Explicit synthetic values; no runtime configuration is seeded by the migration.
    return WeightPriceEdit(
        **(
            dict(
                client_command_id=new_uuid7(),
                resource_id=new_uuid7(),
                expected_version=0,
                reason="SYNTHETIC_WEIGHT_POLICY",
                first_band_upper_grams=4000,
                numerical_threshold_grams=12000,
                first_band_label="Below B kg",
                middle_band_label="B to T kg",
                open_band_label="Above T kg",
                display_unit="kg",
                minimum_amount_minor=1000,
                maximum_amount_minor=5000,
                included_weight_grams=2000,
                saturation_weight_grams=30000,
                curvature="2.5",
                currency="INR",
                active=True,
            )
            | changes
        )
    )


ITEMS = (
    DeclaredRequestItem("TEST_A", 2, weight_band="BELOW_B"),
    DeclaredRequestItem("TEST_B", 3, weight_band="B_TO_T"),
)


def confirmation(quote):
    facts = quote.weight_facts
    return WeightQuoteConfirmation(
        policy_id=facts.policy_id,
        policy_revision=facts.policy_revision,
        quote_fingerprint=facts.quote_fingerprint,
    )


async def setup(factory):
    actor = await manager(factory)
    policy = policy_command()
    await seed_catalogue(factory)
    await change(factory, actor, policy)
    return actor, policy


async def create(factory, customer, context, quote, **changes):
    from test_collection_payment import SLOT_END, SLOT_START

    command = CreateCollectionRequestCommand(
        customer_id=customer,
        client_request_id=new_uuid7(),
        serviceability_context_id=context,
        slot_start=SLOT_START,
        slot_end=SLOT_END,
        items=ITEMS,
        payment_expires_at=utc_now() + timedelta(minutes=30),
        confirmed_quote=confirmation(quote),
    )
    command = replace(command, **changes)
    result = await create_collection_request(
        factory,
        command,
        ConfiguredPickupPricing(factory),
        idempotency_expires_at=utc_now() + timedelta(days=1),
        planning_lead_time_minutes=15,
    )
    return command, result


async def test_manager_revision_replay_conflict_and_immutable_history(database_session_factory):
    factory = database_session_factory
    actor, policy = await setup(factory)
    assert await change(factory, actor, policy) == policy.resource_id
    edit = policy.model_copy(
        update={"client_command_id": new_uuid7(), "expected_version": 1, "curvature": Decimal("3")}
    )
    await change(factory, actor, edit)
    async with factory() as session:
        history = await versions(session, "weight_pricing_policy", policy.resource_id)
        assert history["current_revision"] == 2 and len(history["versions"]) == 2
        assert history["versions"][-1]["actor_id"] == actor
        assert history["versions"][-1]["command_id"] == edit.client_command_id
        assert history["versions"][-1]["resulting_state"]["curvature"] == "3.000000"
    with pytest.raises(OperationalInputError, match="BUSINESS_VERSION_CHANGED"):
        await change(factory, actor, policy.model_copy(update={"client_command_id": new_uuid7()}))
    with pytest.raises(DBAPIError, match="append-only"):
        async with factory() as session, session.begin():
            await session.execute(
                text("UPDATE weight_pricing_policy_revision SET reason='rewrite'")
            )


async def test_concurrent_manager_updates_have_one_version_winner(database_session_factory):
    factory = database_session_factory
    actor, policy = await setup(factory)
    edits = [
        policy.model_copy(
            update={
                "client_command_id": new_uuid7(),
                "expected_version": 1,
                "maximum_amount_minor": 6000 + i,
            }
        )
        for i in range(2)
    ]
    results = await overlap(
        factory,
        WeightPricingPolicy,
        policy.resource_id,
        [lambda c=c: change(factory, actor, c) for c in edits],
    )
    assert sum(not isinstance(r, Exception) for r in results) == 1, results
    async with factory() as session:
        assert await current_version(session, "weight_pricing_policy", policy.resource_id) == 2


async def test_quote_api_has_no_business_writes_or_price_override(database_session_factory):
    factory = database_session_factory
    actor, policy = await setup(factory)
    user = await create_user(factory)
    app = app_for(factory, user.user_id)
    app.state.pricing_port = ConfiguredPickupPricing(factory)
    body = {
        "policy_id": str(policy.resource_id),
        "policy_revision": 1,
        "items": [
            dict(
                item_category_code="TEST_A",
                declared_quantity=20,
                weight_band="ABOVE_T",
                weight_kg="12.001",
            )
        ],
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as api:
        offered = await api.get("/v1/customer/weight-pricing-policy")
        assert offered.status_code == 200, offered.text
        assert offered.json()["numerical_threshold_grams"] == 12000
        first = await api.post("/v1/customer/collection-quotes", json=body)
        assert first.status_code == 200, first.text
        assert first.headers["cache-control"] == "private, no-store"
        assert first.json()["total_weight_grams"] == 12001
        assert first.json()["items"][0]["declared_weight_grams"] == 12001
        assert not first.json()["live_customer_holds_available"]
        assert (await api.post("/v1/customer/collection-quotes", json=body)).json() == first.json()
        assert (
            await api.post("/v1/customer/collection-quotes", json=dict(body, total_amount_minor=1))
        ).status_code == 422
        old_revision = dict(body, policy_revision=2)
        conflict = await api.post("/v1/customer/collection-quotes", json=old_revision)
        assert conflict.status_code == 409
        assert conflict.json()["detail"] == "WEIGHT_POLICY_CHANGED_REVIEW_DECLARATIONS"
        async with factory() as session:
            for model in (CollectionRequest, Payment, CapacityReservation, RoutingManifest):
                assert await session.scalar(select(func.count()).select_from(model)) == 0
        await change(
            factory,
            actor,
            policy.model_copy(
                update={"client_command_id": new_uuid7(), "expected_version": 1, "active": False}
            ),
        )
        assert (await api.post("/v1/customer/collection-quotes", json=body)).status_code == 503


async def test_stale_confirmation_then_booking_payment_and_history_remain_immutable(
    database_session_factory, address_protector
):
    factory = database_session_factory
    actor, policy = await setup(factory)
    user = await create_user(factory)
    context = await create_context(factory, user.user_id)
    pricing = ConfiguredPickupPricing(factory)
    original = await pricing.quote(ITEMS)
    await change(
        factory,
        actor,
        policy.model_copy(
            update={
                "client_command_id": new_uuid7(),
                "expected_version": 1,
                "minimum_amount_minor": 2000,
            }
        ),
    )
    with pytest.raises(WeightPricingConflict, match="STALE"):
        await create(factory, user.user_id, context.serviceability_context_id, original)
    current = await pricing.quote(ITEMS)
    command, result = await create(
        factory, user.user_id, context.serviceability_context_id, current
    )
    identity = result.request.request_id
    assert sum(i.declared_weight_grams for i in result.items) == 16000
    assert [i.declared_quantity for i in result.items] == [2, 3]
    assert (
        result.request.quoted_amount_minor
        == result.payment.amount_minor
        == current.total_amount_minor
    )
    for sql in (
        "UPDATE collection_request_item SET declared_weight_grams=1 WHERE request_id=:id",
        "UPDATE collection_request SET quoted_amount_minor=1 WHERE request_id=:id",
        "UPDATE payment SET amount_minor=1 WHERE request_id=:id",
        "UPDATE booking_weight_quote SET total_weight_grams=1 WHERE request_id=:id",
    ):
        with pytest.raises(DBAPIError, match="immutable|append-only"):
            async with factory() as session, session.begin():
                await session.execute(text(sql), {"id": identity})
    # Initiation and verified capture use exactly the original immutable amount.
    attempt, _ = await create_ready_attempt(
        factory, user.user_id, identity, key=str(new_uuid7()), order_id=str(new_uuid7())
    )
    await process_authenticated_payment_event(
        factory,
        success_event(attempt, str(new_uuid7())),
        payload_hash=b"synthetic-weight-capture",
        planning_lead_time_minutes=15,
        idempotency_expires_at=utc_now() + timedelta(days=1),
    )
    from test_operational_orchestration import operational

    from tirodhan.modules.routing_operations.operational_commands import ObservePhysicalLoad

    await operational(
        factory,
        actor,
        ObservePhysicalLoad(
            client_command_id=new_uuid7(),
            request_id=identity,
            mass_g=17000,
            volume_ml=5000,
            client_capture_id=new_uuid7(),
            captured_at=utc_now(),
        ),
        utc_now(),
    )
    await change(
        factory,
        actor,
        policy.model_copy(
            update={
                "client_command_id": new_uuid7(),
                "expected_version": 2,
                "minimum_amount_minor": 3000,
            }
        ),
    )
    replay = await create_collection_request(
        factory,
        command,
        pricing,
        idempotency_expires_at=utc_now() + timedelta(days=1),
        planning_lead_time_minutes=15,
    )
    assert replay.request.request_id == identity and replay.request.status == "ACCEPTED"
    assert replay.request.quoted_amount_minor == current.total_amount_minor
    async with factory() as session:
        fact = await session.get(BookingWeightQuote, identity)
        assert fact.policy_revision == 2 and fact.total_weight_grams == 16000
    from tirodhan.modules.collection_requests.cancellation import (
        cancel_collection_request_by_customer,
    )
    from tirodhan.modules.payments.models import Refund

    async with factory() as session, session.begin():
        await cancel_collection_request_by_customer(
            session,
            request_id=identity,
            customer_id=user.user_id,
            idempotency_key=str(new_uuid7()),
            planning_lead_time_minutes=15,
            idempotency_expires_at=utc_now() + timedelta(days=1),
        )
    async with factory() as session:
        refund = await session.scalar(
            select(Refund).where(Refund.payment_id == result.payment.payment_id)
        )
        assert refund.amount_minor == current.total_amount_minor


async def test_quote_config_change_during_booking_is_fenced(database_session_factory, monkeypatch):
    from tirodhan.modules.collection_requests import service

    factory = database_session_factory
    actor, policy = await setup(factory)
    user = await create_user(factory)
    context = await create_context(factory, user.user_id)
    quoted = await ConfiguredPickupPricing(factory).quote(ITEMS)
    entered, release = asyncio.Event(), asyncio.Event()
    original = service._validate_quote

    # Pause quote acquisition before the final booking transaction, without holding DB locks.
    class PausedPricing:
        async def quote(self, items):
            result = await ConfiguredPickupPricing(factory).quote(items)
            entered.set()
            await release.wait()
            return result

    from test_collection_payment import SLOT_END, SLOT_START

    command = CreateCollectionRequestCommand(
        user.user_id,
        new_uuid7(),
        context.serviceability_context_id,
        SLOT_START,
        SLOT_END,
        ITEMS,
        utc_now() + timedelta(minutes=30),
        confirmed_quote=confirmation(quoted),
    )
    task = asyncio.create_task(
        create_collection_request(
            factory,
            command,
            PausedPricing(),
            idempotency_expires_at=utc_now() + timedelta(days=1),
            planning_lead_time_minutes=15,
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 10)
        await change(
            factory,
            actor,
            policy.model_copy(
                update={
                    "client_command_id": new_uuid7(),
                    "expected_version": 1,
                    "maximum_amount_minor": 7000,
                }
            ),
        )
        release.set()
        with pytest.raises(WeightPricingConflict, match="STALE"):
            await asyncio.wait_for(task, 10)
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert service._validate_quote is original
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(CollectionRequest)) == 0


async def test_operational_hold_mass_once_and_changed_declarations_rejected(
    database_session_factory, monkeypatch, address_protector
):
    factory = database_session_factory
    inputs, now, region = await configured(factory, monkeypatch)
    actor, policy = await setup(factory)
    customer, original = inputs[0]
    from tirodhan.modules.serviceability.models import ServiceabilityContext

    async with factory() as session, session.begin():
        context = await session.get(ServiceabilityContext, original.context_id)
        context.address_snapshot_encrypted = await address_protector.protect("Synthetic household")
    pricing = ConfiguredPickupPricing(factory)
    quoted = await pricing.quote(ITEMS)
    c = await command_for(factory, region, original, mass_g=16000)
    c = c.model_copy(
        update={
            "items": tuple(
                WeightItemInput(
                    item_category_code=i.item_category_code,
                    declared_quantity=i.declared_quantity,
                    weight_band=i.weight_band,
                )
                for i in ITEMS
            ),
            "confirmed_quote": confirmation(quoted),
        }
    )
    held = await hold_slot(
        factory, customer_id=customer, command=c, expires_at=now + timedelta(hours=1), now=now
    )
    async with factory() as session:
        row = await session.get(CapacityReservation, held)
        # Fixture's existing regional margin is applied once to raw declared grams.
        from tirodhan.modules.routing_operations.orchestration_models import OperatingRegion

        region_row = await session.get(OperatingRegion, region)
        assert row.declared_mass_g == 16000
        assert row.mass_g == (16000 * region_row.policy["safety_percent"] + 99) // 100
        mass, volume = row.mass_g, row.volume_ml
    changed = (
        replace(ITEMS[0], declared_quantity=3),
        ITEMS[1],
    )  # Same grams, different line facts.
    changed_quote = await pricing.quote(changed)
    from test_commercial_admission_benchmark import create_collection_request as create_booking

    command = CreateCollectionRequestCommand(
        customer,
        new_uuid7(),
        c.context_id,
        c.slot_start,
        c.slot_end,
        changed,
        now + timedelta(minutes=30),
        held,
        confirmation(changed_quote),
    )
    with pytest.raises(OperationalInputError, match="DECLARATION_OR_QUOTE_MISMATCH"):
        await create_booking(
            factory,
            command,
            pricing,
            idempotency_expires_at=now + timedelta(days=1),
            planning_lead_time_minutes=15,
            require_capacity_reservation=True,
        )
    from test_booking_recovery import LoseBookingResponse
    from test_booking_recovery import app_for as recovery_app
    from test_operational_api import _token_for

    app = recovery_app(SimpleNamespace(factory=factory, protector=address_protector))
    app.state.pricing_port = pricing
    token, _ = await _token_for(factory, customer, roles=("CUSTOMER",))
    body = dict(
        client_request_id=str(command.client_request_id),
        serviceability_context_id=str(c.context_id),
        capacity_reservation_id=str(held),
        slot_start=c.slot_start.isoformat(),
        slot_end=c.slot_end.isoformat(),
        items=[
            dict(
                item_category_code=i.item_category_code,
                declared_quantity=i.declared_quantity,
                weight_band=i.weight_band,
            )
            for i in ITEMS
        ],
        confirmed_quote=confirmation(quoted).model_dump(mode="json"),
    )
    async with httpx.AsyncClient(
        transport=LoseBookingResponse(app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as api:
        with pytest.raises(httpx.ReadError, match="response loss"):
            await api.post("/v1/collection-requests", json=body)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as api:
        recovered = await api.get(f"/v2/customer/slot-holds/{held}")
        assert recovered.status_code == 200, recovered.text
        identity = UUID(recovered.json()["bound_request_id"])
        assert not recovered.json()["bookable"]
        replay = await api.post("/v1/collection-requests", json=body)
        assert replay.status_code == 201 and replay.json()["request_id"] == str(identity)
        detail = await api.get(f"/v1/customer/collection-requests/{identity}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["weight_quote"]["total_weight_grams"] == 16000
        fresh_body = c.model_copy(update={"client_command_id": new_uuid7()}).model_dump(mode="json")
        blocked = await api.post("/v2/customer/slot-holds", json=fresh_body)
        assert blocked.status_code == 409
        assert blocked.json()["detail"] == "PHYSICAL_VOLUME_POLICY_REQUIRED"
    async with factory() as session:
        manifest = await session.get(RoutingManifest, identity)
        assert (manifest.mass_g, manifest.volume_ml) == (mass, volume)
    assert (
        await hold_slot(
            factory,
            customer_id=customer,
            command=c,
            expires_at=now + timedelta(hours=2),
            customer_checkout=True,
        )
        == held
    )
    fresh = c.model_copy(update={"client_command_id": new_uuid7()})
    with pytest.raises(OperationalInputError, match="PHYSICAL_VOLUME_POLICY_REQUIRED"):
        await hold_slot(
            factory,
            customer_id=customer,
            command=fresh,
            expires_at=now + timedelta(hours=1),
            customer_checkout=True,
        )


async def test_migration_empty_roundtrip_and_populated_refusal(database_session_factory):
    factory = database_session_factory
    config = Config("alembic.ini")
    await asyncio.to_thread(alembic_command.downgrade, config, "0036_recovery_work_requests")
    await asyncio.to_thread(alembic_command.upgrade, config, "head")
    async with factory() as session:
        await (await session.connection()).run_sync(assert_readiness_schema)
    await setup(factory)
    with pytest.raises(DBAPIError, match="export adopted weight pricing"):
        await asyncio.to_thread(alembic_command.downgrade, config, "0036_recovery_work_requests")
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(WeightPricingPolicy)) == 1


async def test_missing_policy_and_numerical_inputs_fail_closed(database_session_factory):
    pricing = ConfiguredPickupPricing(database_session_factory)
    with pytest.raises(PricingNotConfiguredError, match="WEIGHT_PRICING"):
        await pricing.quote(ITEMS)
    await setup(database_session_factory)
    for kg in (None, "12", "11.999", "0", "1e4", "12.0001"):
        with pytest.raises(WeightPricingConflict):
            await pricing.quote(
                [DeclaredRequestItem("TEST_A", weight_band="ABOVE_T", weight_kg=kg)]
            )


async def test_draft_activation_retirement_and_legacy_booking_no_volume_bypass(
    database_session_factory,
):
    from test_collection_payment import SLOT_END, SLOT_START

    from tirodhan.modules.business_admin.commands import PriceEdit

    factory = database_session_factory
    actor = await manager(factory)
    await seed_catalogue(factory)
    await change(
        factory,
        actor,
        PriceEdit(
            client_command_id=new_uuid7(),
            resource_id=new_uuid7(),
            expected_version=0,
            reason="SYNTHETIC_LEGACY_PRICE",
            amount_minor=1000,
            active=True,
        ),
    )
    draft = policy_command(active=False)
    await change(factory, actor, draft)
    pricing = ConfiguredPickupPricing(factory)
    assert (await pricing.quote([DeclaredRequestItem("TEST_A")])).total_amount_minor == 1000
    await change(
        factory,
        actor,
        draft.model_copy(
            update={"client_command_id": new_uuid7(), "expected_version": 1, "active": True}
        ),
    )
    quote = await pricing.quote(ITEMS)
    user = await create_user(factory)
    context = await create_context(factory, user.user_id)
    app = app_for(factory, user.user_id)
    app.state.pricing_port = pricing
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as api:
        body = dict(
            client_request_id=str(new_uuid7()),
            serviceability_context_id=str(context.serviceability_context_id),
            slot_start=SLOT_START.isoformat(),
            slot_end=SLOT_END.isoformat(),
            items=[
                dict(
                    item_category_code=i.item_category_code,
                    declared_quantity=i.declared_quantity,
                    weight_band=i.weight_band,
                )
                for i in ITEMS
            ],
            confirmed_quote=confirmation(quote).model_dump(mode="json"),
        )
        result = await api.post("/v1/collection-requests", json=body)
        assert result.status_code == 409, result.text
        assert result.json()["detail"]["code"] == "CAPACITY_RESERVATION_REQUIRED"
    await change(
        factory,
        actor,
        draft.model_copy(
            update={"client_command_id": new_uuid7(), "expected_version": 2, "active": False}
        ),
    )
    with pytest.raises(PricingNotConfiguredError, match="ACTIVE_WEIGHT"):
        await pricing.quote([DeclaredRequestItem("TEST_A")])


async def test_pre_transition_booking_exact_replay_after_weight_activation(
    database_session_factory,
):
    from test_collection_payment import ITEMS as legacy_items
    from test_collection_payment import create_request

    factory = database_session_factory
    user = await create_user(factory)
    context = await create_context(factory, user.user_id)
    original = await create_request(factory, user.user_id, context.serviceability_context_id)
    await setup(factory)
    command = CreateCollectionRequestCommand(
        user.user_id,
        original.request.client_request_id,
        context.serviceability_context_id,
        original.request.slot_start,
        original.request.slot_end,
        legacy_items,
        original.request.payment_expires_at,
    )
    replay = await create_collection_request(
        factory,
        command,
        ConfiguredPickupPricing(factory),
        idempotency_expires_at=utc_now() + timedelta(days=1),
        planning_lead_time_minutes=15,
    )
    assert replay.request.request_id == original.request.request_id
    assert replay.request.quoted_amount_minor == original.request.quoted_amount_minor
