"""Selected whole-line volumes survive HTTP holds, booking recovery and routing unchanged."""

import asyncio
import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from alembic import command as alembic_command
from alembic.config import Config
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from test_booking_recovery import LoseBookingResponse, app_for
from test_operational_api import _token_for
from test_slot_holds import command_for, configured
from test_weight_pricing import ITEMS, setup

from tirodhan.db.values import new_uuid7
from tirodhan.modules.business_admin.weight_contracts import WeightItemInput
from tirodhan.modules.business_admin.weight_pricing import ConfiguredPickupPricing
from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.reliability.models import IdempotencyRecord
from tirodhan.modules.routing_operations.capacity_models import SlotReservationDebit
from tirodhan.modules.routing_operations.models import RoutingManifest
from tirodhan.modules.routing_operations.orchestration_models import (
    CapacityReservation,
    OperatingRegion,
)
from tirodhan.modules.serviceability.models import ServiceabilityContext

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def case_for(factory, monkeypatch, protector):
    inputs, now, region = await configured(factory, monkeypatch)
    _, policy = await setup(factory)
    customer, original = inputs[0]
    async with factory() as session, session.begin():
        context = await session.get(ServiceabilityContext, original.context_id)
        context.address_snapshot_encrypted = await protector.protect("Synthetic household")
        initial_bookings = await session.scalar(select(func.count()).select_from(CollectionRequest))
    items = tuple(
        replace(i, declared_volume_ml=v) for i, v in zip(ITEMS, (1000, 2000), strict=True)
    )
    declared = [
        WeightItemInput(
            item_category_code=i.item_category_code,
            declared_quantity=i.declared_quantity,
            weight_band=i.weight_band,
            declared_volume_ml=i.declared_volume_ml,
        ).model_dump(mode="json")
        for i in items
    ]
    app = app_for(SimpleNamespace(factory=factory, protector=protector))
    app.state.pricing_port = ConfiguredPickupPricing(factory)
    token, _ = await _token_for(factory, customer, roles=("CUSTOMER",))
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    )
    quote = await client.post(
        "/v1/customer/collection-quotes",
        json={
            "policy_id": str(policy.resource_id),
            "policy_revision": 1,
            "items": declared,
        },
    )
    assert quote.status_code == 200, quote.text
    hold = await command_for(factory, region, original, mass_g=16000, volume_ml=3000)
    body = hold.model_dump(mode="json") | {
        "items": declared,
        "confirmed_quote": quote.json()["confirmation"],
    }
    await client.aclose()
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    )
    return SimpleNamespace(
        factory=factory,
        now=now,
        region=region,
        app=app,
        client=client,
        quote=quote.json(),
        body=body,
        customer=customer,
        initial_bookings=initial_bookings,
    )


async def test_http_volume_hold_booking_and_lost_response_recovery(
    database_session_factory,
    monkeypatch,
    address_protector,
):
    c = await case_for(database_session_factory, monkeypatch, address_protector)
    async with c.client as api:
        assert (
            c.quote["total_volume_ml"] == 3000
        )  # Whole lines; quantities 2 and 3 aren't multipliers.
        assert c.quote["live_customer_holds_available"] and c.quote["hold_blocker"] is None
        for changes, error in (
            ({"volume_ml": 2999}, "HOLD_DECLARED_VOLUME_MISMATCH"),
            ({"mass_g": 15999}, "HOLD_DECLARED_MASS_MISMATCH"),
        ):
            response = await api.post("/v2/customer/slot-holds", json=c.body | changes)
            assert response.status_code == 409, response.text
            assert response.json()["detail"] == error
        responses = await asyncio.gather(
            *(api.post("/v2/customer/slot-holds", json=c.body) for _ in range(2))
        )
        assert all(r.status_code == 201 for r in responses), [r.text for r in responses]
        assert responses[0].json() == responses[1].json()
        held = UUID(c.body["client_command_id"])
        async with c.factory() as session:
            row = await session.get(CapacityReservation, held)
            region = await session.get(OperatingRegion, c.region)
            expected = (
                (16000 * region.policy["safety_percent"] + 99) // 100,
                (3000 * region.policy["safety_percent"] + 99) // 100,
            )
            assert (row.mass_g, row.volume_ml) == expected
            debit_count = await session.scalar(
                select(func.count()).select_from(SlotReservationDebit)
            )
            expiry = row.expires_at
        booking = dict(
            client_request_id=str(new_uuid7()),
            serviceability_context_id=c.body["context_id"],
            capacity_reservation_id=str(held),
            slot_start=c.body["slot_start"],
            slot_end=c.body["slot_end"],
            items=c.body["items"],
            confirmed_quote=c.body["confirmed_quote"],
        )
        # Same weight, fee and aggregate volume; changed line volumes still cannot bind this hold.
        changed = [
            dict(i, declared_volume_ml=v)
            for i, v in zip(c.body["items"], (1500, 1500), strict=True)
        ]
        quote = await api.post(
            "/v1/customer/collection-quotes",
            json={
                "policy_id": c.quote["confirmation"]["policy_id"],
                "policy_revision": 1,
                "items": changed,
            },
        )
        assert quote.json()["total_amount_minor"] == c.quote["total_amount_minor"]
        rejected = await api.post(
            "/v1/collection-requests",
            json=booking
            | {
                "items": changed,
                "confirmed_quote": quote.json()["confirmation"],
            },
        )
        assert rejected.status_code == 409, rejected.text
        assert rejected.json()["detail"]["code"] == "HOLD_DECLARATION_OR_QUOTE_MISMATCH"
        async with httpx.AsyncClient(
            transport=LoseBookingResponse(c.app), base_url="http://test", headers=api.headers
        ) as losing:
            with pytest.raises(httpx.ReadError, match="response loss"):
                await losing.post("/v1/collection-requests", json=booking)
        replay = await api.post("/v1/collection-requests", json=booking)
        assert replay.status_code == 201, replay.text
        identity = UUID(replay.json()["request_id"])
        assert [i["declared_volume_ml"] for i in replay.json()["items"]] == [1000, 2000]
        detail = await api.get(f"/v1/customer/collection-requests/{identity}")
        assert [i["declared_volume_ml"] for i in detail.json()["items"]] == [1000, 2000]
        assert (await api.post("/v2/customer/slot-holds", json=c.body)).status_code == 201
        changed_command = c.body | {"volume_ml": 3001}
        assert (await api.post("/v2/customer/slot-holds", json=changed_command)).status_code == 409
        async with c.factory() as session:
            manifest = await session.get(RoutingManifest, identity)
            row = await session.get(CapacityReservation, held)
            assert (manifest.mass_g, manifest.volume_ml) == expected  # No second safety margin.
            assert row.expires_at == expiry
            assert await session.scalar(select(func.count()).select_from(CapacityReservation)) == 1
            assert (
                await session.scalar(select(func.count()).select_from(CollectionRequest))
                == c.initial_bookings + 1
            )
            assert (
                await session.scalar(select(func.count()).select_from(SlotReservationDebit))
                == debit_count
            )
        with pytest.raises(DBAPIError, match="immutable"):
            async with c.factory() as session, session.begin():
                await session.execute(
                    text(
                        "UPDATE collection_request_item SET declared_volume_ml=1 "
                        "WHERE request_id=:id"
                    ),
                    {"id": identity},
                )
        with pytest.raises(DBAPIError, match="export declared item volumes"):
            await asyncio.to_thread(
                alembic_command.downgrade, Config("alembic.ini"), "0037_weight_pricing"
            )


async def test_missing_partial_and_invalid_volume_fail_closed(
    database_session_factory,
    monkeypatch,
    address_protector,
):
    c = await case_for(database_session_factory, monkeypatch, address_protector)
    async with c.client as api:
        for volumes in ((None, None), (1000, None)):
            items = [
                dict(i, declared_volume_ml=v) for i, v in zip(c.body["items"], volumes, strict=True)
            ]
            quote = await api.post(
                "/v1/customer/collection-quotes",
                json={
                    "policy_id": c.quote["confirmation"]["policy_id"],
                    "policy_revision": 1,
                    "items": items,
                },
            )
            assert quote.status_code == 200, quote.text
            assert quote.json()["total_volume_ml"] is None
            assert not quote.json()["live_customer_holds_available"]
            result = await api.post(
                "/v2/customer/slot-holds",
                json=c.body
                | {
                    "items": items,
                    "confirmed_quote": quote.json()["confirmation"],
                },
            )
            assert (
                result.status_code == 409
                and result.json()["detail"] == "ITEM_VOLUME_DECLARATIONS_REQUIRED"
            )
        for invalid in (0, -1, True, 1.5, "1000", 2**31):
            items = [dict(c.body["items"][0], declared_volume_ml=invalid), c.body["items"][1]]
            result = await api.post("/v2/customer/slot-holds", json=c.body | {"items": items})
            assert result.status_code == 422, result.text
        async with c.factory() as session:
            assert await session.scalar(select(func.count()).select_from(CapacityReservation)) == 0
            assert await session.scalar(select(func.count()).select_from(SlotReservationDebit)) == 0


async def test_failed_volume_hold_rolls_back_inventory_and_retries(
    database_session_factory,
    monkeypatch,
    address_protector,
):
    from tirodhan.modules.routing_operations import slot_holds

    c = await case_for(database_session_factory, monkeypatch, address_protector)
    original = slot_holds.complete_idempotency_record

    async def crash(*args, **kwargs):
        raise RuntimeError("synthetic failure before commit")

    monkeypatch.setattr(slot_holds, "complete_idempotency_record", crash)
    async with c.client as api:
        with pytest.raises(RuntimeError, match="synthetic failure"):
            await api.post("/v2/customer/slot-holds", json=c.body)
        async with c.factory() as session:
            assert await session.scalar(select(func.count()).select_from(CapacityReservation)) == 0
            assert await session.scalar(select(func.count()).select_from(SlotReservationDebit)) == 0
        monkeypatch.setattr(slot_holds, "complete_idempotency_record", original)
        assert (await api.post("/v2/customer/slot-holds", json=c.body)).status_code == 201


async def test_pre_volume_hold_replay_retains_original_nested_fingerprint(
    database_session_factory,
    monkeypatch,
    address_protector,
):
    from datetime import timedelta

    from tirodhan.modules.routing_operations.slot_holds import HoldSlot, hold_slot

    c = await case_for(database_session_factory, monkeypatch, address_protector)
    body = dict(
        c.body,
        items=[{k: v for k, v in i.items() if k != "declared_volume_ml"} for i in c.body["items"]],
    )
    async with c.client as api:
        quote = await api.post(
            "/v1/customer/collection-quotes",
            json={
                "policy_id": c.quote["confirmation"]["policy_id"],
                "policy_revision": 1,
                "items": body["items"],
            },
        )
        body["confirmed_quote"] = quote.json()["confirmation"]
        # Create an existing-style operational hold and independently calculate the
        # pre-volume serializer's bytes. The HTTP replay must retain this identity.
        old_fingerprint = hashlib.sha256(json.dumps(body, separators=(",", ":")).encode()).digest()
        held = await hold_slot(
            c.factory,
            customer_id=c.customer,
            command=HoldSlot.model_validate(body),
            expires_at=c.now + timedelta(hours=1),
            now=c.now,
        )
        async with c.factory() as session:
            record = await session.scalar(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.scope == f"routing.slot-hold:{c.customer}",
                    IdempotencyRecord.idempotency_key == body["client_command_id"],
                )
            )
            assert bytes(record.request_fingerprint) == old_fingerprint
        replay = await api.post("/v2/customer/slot-holds", json=body)
        assert replay.status_code == 201, replay.text
        assert replay.json()["reservation_id"] == str(held)
