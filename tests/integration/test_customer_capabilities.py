"""Real PostgreSQL contracts, privacy, races, rollback and notification recovery."""

import asyncio
import base64
import json
import os
import subprocess
import sys
from datetime import timedelta
from typing import Any

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, event, func, select, text, update
from sqlalchemy.exc import DBAPIError
from test_collection_payment import create_context, create_request, create_user
from test_customer_mobile_core import seed_catalogue
from test_operational_api import _token_codec, _token_for

from tirodhan.core.config import Settings
from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.main import create_app
from tirodhan.modules.customer_account import devices, service
from tirodhan.modules.customer_account.models import (
    ContentParagraph,
    ContentRevision,
    CustomerCommandReceipt,
    CustomerFeedback,
    CustomerNotification,
    CustomerPreferences,
    CustomerProfile,
    CustomerPushBinding,
    CustomerPushDelivery,
)
from tirodhan.modules.customer_account.notifications import (
    ConfiguredDelivery,
    deliver_one,
    materialize,
    notification_page,
)
from tirodhan.modules.customer_account.protection import CustomerDataProtection
from tirodhan.modules.customer_account.web_push import b64
from tirodhan.modules.customer_reads.errors import CustomerReadError
from tirodhan.modules.customer_reads.models import CatalogueCategory
from tirodhan.modules.identity.models import RefreshSession, UserPhone, UserRole
from tirodhan.modules.identity.phone_protection import AesGcmPhoneIdentityProtector
from tirodhan.modules.identity.service import AuthenticatedPrincipal
from tirodhan.modules.reliability.models import InboxMessage, OutboxEvent
from tirodhan.modules.reliability.primitives import append_outbox_event

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def config(**overrides: Any) -> Settings:
    values: dict[str, Any] = dict(
        _env_file=None,
        environment="test",
        command_idempotency_ttl_seconds=3600,
        customer_data_active_key_id="synthetic",
        customer_data_encryption_keys=json.dumps(
            {"synthetic": base64.b64encode(b"e" * 32).decode()}
        ),
        customer_data_hmac_key=base64.b64encode(b"h" * 32).decode(),
        customer_cursor_signing_key="synthetic-cursor-key-xxxxxxxxxxxxxxxx",
        customer_cursor_ttl_seconds=60,
    )
    values.update(overrides)
    return Settings(**values)


def phone_crypto() -> AesGcmPhoneIdentityProtector:
    return AesGcmPhoneIdentityProtector(
        active_key_id="phone", encryption_keys={"phone": b"p" * 32}, lookup_hmac_key=b"l" * 32
    )


async def setup(factory: Any, roles: tuple[str, ...] = ("CUSTOMER",), **options: Any) -> Any:
    user = await create_user(factory)
    token, session_id = await _token_for(factory, user.user_id, roles=roles)
    phone = phone_crypto()
    async with factory() as session, session.begin():
        session.add(
            UserPhone(
                user_id=user.user_id,
                phone_encrypted=await phone.protect("+919999991234"),
                phone_lookup_hmac=await phone.lookup_hmac(str(user.user_id)),
                verified_at=utc_now(),
                created_at=utc_now(),
            )
        )
    settings = config(**options)
    app = create_app(settings, access_token_codec=_token_codec(), phone_identity_protector=phone)
    app.state.database_session_factory = factory
    return (
        app,
        {"Authorization": f"Bearer {token}"},
        AuthenticatedPrincipal(user.user_id, session_id, frozenset(roles)),
    )


async def client_setup(factory: Any, **options: Any) -> Any:
    app, headers, principal = await setup(factory, **options)
    return AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers=headers
    ), principal


@pytest.mark.parametrize(
    "path,fields",
    [
        ("profile", {"display_name": " Customer ", "email": "person@example.test"}),
        ("preferences", {"language": "hi-IN", "collection_notifications": False}),
        ("favourite-categories", {"category_codes": []}),
    ],
)
async def test_versioned_contract_replay_after_later_edit_and_owner_isolation(
    database_session_factory: Any,
    path: str,
    fields: dict[str, Any],
) -> None:
    factory = database_session_factory
    client, owner = await client_setup(factory)
    other, _ = await client_setup(factory)
    url = f"/v1/customer/me/{path}"
    async with client, other:
        initial = await client.get(url)
        assert initial.status_code == 200 and initial.json()["version"] == 1
        assert initial.headers["cache-control"] == "private, no-store"
        body = {"client_request_id": str(new_uuid7()), "expected_version": 1, **fields}
        first = await client.put(url, json=body)
        assert first.status_code == 200, first.text
        assert first.json()["version"] == 2
        later = await client.put(
            url, json={**body, "client_request_id": str(new_uuid7()), "expected_version": 2}
        )
        assert later.status_code == 200 and later.json()["version"] == 3
        assert (await client.put(url, json=body)).json() == first.json()
        assert (await client.put(url, json={**body, "expected_version": 3})).status_code == 409
        assert (
            await client.put(url, json={**body, "client_request_id": str(new_uuid7())})
        ).status_code == 409
        assert (await other.get(url)).json()["version"] == 1
        if path == "profile":
            assert first.json()["display_name"] == "Customer"
            assert "999999" not in first.json()["phone_display"]
            async with factory() as session:
                row = await session.get(CustomerProfile, owner.user_id)
                assert b"Customer" not in row.details_encrypted
                receipts = (await session.scalars(select(CustomerCommandReceipt))).all()
                assert all(b"person@example" not in r.result_encrypted for r in receipts)


@pytest.mark.parametrize("same_key", [True, False])
async def test_simultaneous_updates_have_one_command_or_one_version_winner(
    database_session_factory: Any,
    same_key: bool,
) -> None:
    factory = database_session_factory
    client, owner = await client_setup(factory)
    body = {
        "client_request_id": str(new_uuid7()),
        "expected_version": 1,
        "language": "hi-IN",
        "collection_notifications": True,
    }
    async with client, factory() as blocker, blocker.begin():
        await blocker.execute(
            text("SELECT 1 FROM app_user WHERE user_id=:id FOR NO KEY UPDATE"),
            {"id": owner.user_id},
        )
        start = asyncio.Event()

        async def put(second: bool) -> Any:
            await start.wait()
            return await client.put(
                "/v1/customer/me/preferences",
                json={
                    **body,
                    "client_request_id": str(new_uuid7())
                    if second and not same_key
                    else body["client_request_id"],
                },
            )

        tasks = [asyncio.create_task(put(False)), asyncio.create_task(put(True))]
        start.set()
        await blocker.commit()
        responses = await asyncio.wait_for(asyncio.gather(*tasks), 15)
    assert sorted(r.status_code for r in responses) == ([200, 200] if same_key else [200, 409])
    if same_key:
        assert responses[0].json() == responses[1].json()
    async with factory() as session:
        assert (await session.get(CustomerPreferences, owner.user_id)).version == 2
        assert await session.scalar(select(func.count()).select_from(CustomerCommandReceipt)) == 1


async def test_feedback_rollback_before_commit_and_replay_after_lost_response(
    database_session_factory: Any,
    monkeypatch: Any,
) -> None:
    factory = database_session_factory
    client, _ = await client_setup(factory)
    body = {"client_request_id": str(new_uuid7()), "message": " Please improve pickup clarity. "}
    original = service.complete_idempotency_record

    async def crash(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("synthetic crash before commit")

    async with client:
        monkeypatch.setattr(service, "complete_idempotency_record", crash)
        with pytest.raises(RuntimeError):
            await client.post("/v1/customer/feedback", json=body)
        async with factory() as session:
            assert await session.scalar(select(func.count()).select_from(CustomerFeedback)) == 0
        monkeypatch.setattr(service, "complete_idempotency_record", original)
        responses = await asyncio.gather(
            *(client.post("/v1/customer/feedback", json=body) for _ in range(2))
        )
        assert all(r.status_code == 201 for r in responses)
        assert responses[0].json() == responses[1].json()
        assert (await client.post("/v1/customer/feedback", json=body)).json() == responses[0].json()
        assert (
            await client.post(
                "/v1/customer/feedback", json={**body, "message": "Different message"}
            )
        ).status_code == 409
    async with factory() as session:
        row = await session.scalar(select(CustomerFeedback))
        assert row.feedback_id.version == 7
        assert b"pickup clarity" not in row.message_encrypted
        assert await session.scalar(select(func.count()).select_from(CustomerFeedback)) == 1


async def test_favourites_catalogue_retirement_and_historical_replay(
    database_session_factory: Any,
) -> None:
    factory = database_session_factory
    client, _ = await client_setup(factory)
    await seed_catalogue(factory)
    body = {
        "client_request_id": str(new_uuid7()),
        "expected_version": 1,
        "category_codes": ["TEST_B", "TEST_A"],
    }
    url = "/v1/customer/me/favourite-categories"
    async with client:
        result = await client.put(url, json=body)
        assert result.json()["category_codes"] == ["TEST_A", "TEST_B"]
        async with factory() as session, session.begin():
            await session.execute(
                update(CatalogueCategory)
                .where(CatalogueCategory.category_code == "TEST_A")
                .values(active=False)
            )
        assert (await client.get(url)).json() == {"category_codes": ["TEST_B"], "version": 2}
        assert (await client.put(url, json=body)).json() == result.json()
        for codes in [["TEST_A"], ["UNKNOWN"], ["TEST_B", "TEST_B"]]:
            response = await client.put(
                url,
                json={
                    **body,
                    "client_request_id": str(new_uuid7()),
                    "expected_version": 2,
                    "category_codes": codes,
                },
            )
            assert response.status_code == 422
        assert (
            await client.put(
                url,
                json={
                    **body,
                    "client_request_id": str(new_uuid7()),
                    "expected_version": 2,
                    "category_codes": [],
                },
            )
        ).json() == {"category_codes": [], "version": 3}


@pytest.mark.parametrize("roles", [(), ("RIDER",), ("MANAGER",)])
async def test_customer_role_boundaries_and_public_content(
    database_session_factory: Any, roles: Any
) -> None:
    app, headers, _ = await setup(database_session_factory, roles=roles)
    reads = [
        "me/profile",
        "me/preferences",
        "me/favourite-categories",
        "payment-methods",
        "notifications",
    ]
    writes = [
        "me/profile",
        "me/preferences",
        "me/favourite-categories",
        "me/push-devices/" + str(new_uuid7()),
        "me/web-push-subscriptions/" + str(new_uuid7()),
    ]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for path in reads:
            assert (await client.get("/v1/customer/" + path)).status_code == 401
            assert (await client.get("/v1/customer/" + path, headers=headers)).status_code == 403
        for path in writes:
            assert (
                await client.put("/v1/customer/" + path, json={}, headers=headers)
            ).status_code == 403
        assert (
            await client.post("/v1/customer/feedback", json={}, headers=headers)
        ).status_code == 403
        assert (await client.get("/v1/content/privacy")).status_code == 404


@pytest.mark.parametrize(
    "url,body",
    [
        ("me/profile", {"display_name": "", "email": None}),
        ("me/profile", {"display_name": "x" * 101, "email": None}),
        ("me/profile", {"display_name": None, "email": "secret-invalid-email"}),
        ("me/profile", {"display_name": "<script>private</script>", "email": None}),
        ("me/preferences", {"language": "fr", "collection_notifications": True}),
        ("me/preferences", {"language": "en-IN", "collection_notifications": "false"}),
        ("me/favourite-categories", {"category_codes": ["x" * 101]}),
        ("feedback", {"message": "bad"}),
        ("feedback", {"message": "x" * 2001}),
    ],
)
async def test_validation_is_bounded_and_does_not_echo_private_input(
    database_session_factory: Any,
    url: str,
    body: Any,
) -> None:
    client, _ = await client_setup(database_session_factory)
    command = {"client_request_id": str(new_uuid7()), **body}
    if url != "feedback":
        command["expected_version"] = 1
    async with client:
        response = await client.request(
            "POST" if url == "feedback" else "PUT", "/v1/customer/" + url, json=command
        )
        assert response.status_code == 422
        assert response.json() == {"error": {"code": "INVALID_INPUT"}}


async def test_provider_disabled_and_missing_crypto_fail_closed(
    database_session_factory: Any,
) -> None:
    client, _ = await client_setup(database_session_factory, customer_data_active_key_id=None)
    async with client:
        assert (await client.get("/v1/customer/me/profile")).status_code == 503
        assert (await client.get("/v1/customer/payment-methods")).json() == {"methods": []}
        installation = str(new_uuid7())
        for platform, provider in [("ANDROID", "FCM"), ("IOS", "APNS")]:
            body = {
                "client_device_id": installation,
                "platform": platform,
                "token_provider": provider,
                "token": "synthetic-token",
            }
            assert (
                await client.put(f"/v1/customer/me/push-devices/{installation}", json=body)
            ).status_code == 503
        assert (
            await client.delete(f"/v1/customer/me/push-devices/{installation}")
        ).status_code == 204


async def register_device(
    factory: Any, principal: Any, device: Any, token: str = "synthetic-fcm"
) -> Any:
    async with factory() as session, session.begin():
        return await devices.register(
            session,
            principal=principal,
            installation=device,
            platform="ANDROID",
            provider="FCM",
            secret=token,
            protection=CustomerDataProtection(config()),
        )


async def test_device_rotation_account_switch_logout_and_delayed_old_commands(
    database_session_factory: Any,
) -> None:
    factory = database_session_factory
    _, _, owner = await setup(factory)
    device = new_uuid7()
    first = await register_device(factory, owner, device)
    assert (await register_device(factory, owner, device)) == first
    await register_device(factory, owner, device, "rotated-token")
    with pytest.raises(CustomerReadError, match="STALE_REGISTRATION"):
        await register_device(factory, owner, device)
    _, _, other = await setup(factory)
    await register_device(factory, other, device, "rotated-token")
    with pytest.raises(CustomerReadError, match="STALE_REGISTRATION"):
        await register_device(factory, owner, device, "late-old-account-token")
    async with factory() as session, session.begin():
        await devices.revoke(session, principal=owner, installation=device, platform="ANDROID")
    async with factory() as session:
        active = (
            await session.scalars(
                select(CustomerPushBinding).where(CustomerPushBinding.revoked_at.is_(None))
            )
        ).all()
        assert len(active) == 1 and active[0].user_id == other.user_id
        assert b"rotated-token" not in active[0].token_encrypted
    async with factory() as session, session.begin():
        await session.execute(
            update(RefreshSession)
            .where(RefreshSession.refresh_session_id == other.refresh_session_id)
            .values(revoked_at=utc_now())
        )
    async with factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(CustomerPushBinding)
                .where(CustomerPushBinding.revoked_at.is_(None))
            )
            == 0
        )
    with pytest.raises(CustomerReadError) as failure:
        await register_device(factory, other, device, "new-token-after-logout")
    assert failure.value.status == 401


async def event_for(factory: Any, owner: Any, key: str) -> Any:
    context = await create_context(factory, owner)
    booking = await create_request(factory, owner, context.serviceability_context_id)
    async with factory() as session, session.begin():
        return await append_outbox_event(
            session,
            event_key=key,
            aggregate_type="collection_request",
            aggregate_id=booking.request.request_id,
            event_type="CustomerFinancialStatusChanged",
            payload={"request_id": str(booking.request.request_id), "change": "PAYMENT_CONFIRMED"},
        )


class Delivery:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = outcomes
        self.sent: list[Any] = []

    async def send(
        self, provider: str, secret: str, signal: Any, browser_origin: str | None
    ) -> str:
        self.sent.append((provider, secret, signal))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


async def due(factory: Any) -> None:
    async with factory() as session, session.begin():
        await session.execute(
            update(CustomerPushDelivery).values(
                next_attempt_at=utc_now() - timedelta(seconds=1),
            )
        )


async def test_outbox_inbox_history_and_retry_invalid_token(database_session_factory: Any) -> None:
    factory = database_session_factory
    client, owner = await client_setup(factory)
    await register_device(factory, owner, new_uuid7())
    source = await event_for(factory, owner.user_id, "synthetic-financial:1")
    assert sum(await asyncio.gather(materialize(factory), materialize(factory))) == 1
    assert await materialize(factory) == 0
    crypto = CustomerDataProtection(config())
    port = Delivery(["RETRY", "INVALID_TOKEN"])
    assert await deliver_one(factory, crypto, port)
    await due(factory)
    assert await deliver_one(factory, crypto, port)
    assert not await deliver_one(factory, crypto, port)
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(CustomerNotification)) == 1
        assert await session.scalar(select(func.count()).select_from(InboxMessage)) == 1
        assert (await session.get(OutboxEvent, source.outbox_event_id)).status == "PENDING"
        assert (await session.scalar(select(CustomerPushDelivery))).status == "PERMANENTLY_FAILED"
        assert (await session.scalar(select(CustomerPushBinding))).revoked_at is not None
    async with client:
        history = (await client.get("/v1/customer/notifications")).json()
        assert history["items"][0]["event_id"] == str(port.sent[0][2].event_id)
        assert set(history["items"][0]) == {
            "event_id",
            "request_id",
            "title",
            "body",
            "target",
            "created_at",
        }


async def test_crash_after_external_send_lease_recovery_and_duplicate_signal(
    database_session_factory: Any,
) -> None:
    factory = database_session_factory
    _, _, owner = await setup(factory)
    await register_device(factory, owner, new_uuid7())
    await event_for(factory, owner.user_id, "synthetic-crash")
    await materialize(factory)
    port = Delivery([RuntimeError("simulated lost provider response"), "SENT"])
    crypto = CustomerDataProtection(config())
    with pytest.raises(RuntimeError):
        await deliver_one(factory, crypto, port)
    assert not await deliver_one(factory, crypto, port)
    async with factory() as session, session.begin():
        await session.execute(
            update(CustomerPushDelivery).values(lease_until=utc_now() - timedelta(seconds=1))
        )
    assert await deliver_one(factory, crypto, port)
    assert port.sent[0][2] == port.sent[1][2]
    assert not await deliver_one(factory, crypto, port)
    assert await materialize(factory) == 0


@pytest.mark.parametrize("disable", ["preference", "session", "role", "rotation"])
async def test_delivery_rechecks_eligibility_after_notification_creation(
    database_session_factory: Any,
    disable: str,
) -> None:
    factory = database_session_factory
    _, _, owner = await setup(factory)
    device = new_uuid7()
    await register_device(factory, owner, device)
    await event_for(factory, owner.user_id, "eligibility")
    await materialize(factory)
    if disable == "rotation":
        await register_device(factory, owner, device, "rotation")
    else:
        async with factory() as session, session.begin():
            if disable == "preference":
                session.add(
                    CustomerPreferences(
                        user_id=owner.user_id,
                        language="en-IN",
                        collection_notifications=False,
                        version=2,
                    )
                )
            elif disable == "session":
                await session.execute(
                    update(RefreshSession)
                    .where(RefreshSession.refresh_session_id == owner.refresh_session_id)
                    .values(revoked_at=utc_now())
                )
            else:
                await session.execute(
                    update(UserRole)
                    .where(UserRole.user_id == owner.user_id)
                    .values(revoked_at=utc_now())
                )
    port = Delivery([])
    assert await deliver_one(factory, CustomerDataProtection(config()), port)
    assert port.sent == []
    async with factory() as session:
        assert (await session.scalar(select(CustomerPushDelivery))).status == "SUPPRESSED"


async def test_notifications_pagination_owner_expiry_tampering_and_snapshot(
    database_session_factory: Any,
) -> None:
    factory = database_session_factory
    client, owner = await client_setup(factory)
    other, stranger = await client_setup(factory)
    for i in range(3):
        await event_for(factory, owner.user_id, f"page:{i}")
    await materialize(factory)
    async with client, other:
        first = (await client.get("/v1/customer/notifications?limit=1")).json()
        token = first["next_cursor"]
        assert token
        await event_for(factory, owner.user_id, "late-page")
        await materialize(factory)
        seen = first["items"]
        while token:
            result = await client.get(
                "/v1/customer/notifications", params={"limit": 1, "cursor": token}
            )
            assert result.status_code == 200, result.text
            seen += result.json()["items"]
            token = result.json()["next_cursor"]
        assert len({r["event_id"] for r in seen}) == 3
        assert [r["created_at"] for r in seen] == sorted(
            (r["created_at"] for r in seen), reverse=True
        )
        assert (await other.get("/v1/customer/notifications")).json()["items"] == []
        token = first["next_cursor"]
        assert (
            await other.get("/v1/customer/notifications", params={"cursor": token})
        ).status_code == 422
        assert (
            await client.get("/v1/customer/notifications", params={"cursor": token + "x"})
        ).status_code == 422
        async with factory() as session:
            with pytest.raises(CustomerReadError, match="CURSOR_EXPIRED"):
                await notification_page(
                    session,
                    owner=owner.user_id,
                    limit=1,
                    token=token,
                    settings=config(),
                    now=utc_now() + timedelta(seconds=61),
                )
        for limit in [0, 51, "bad"]:
            assert (
                await client.get("/v1/customer/notifications", params={"limit": limit})
            ).status_code == 422


async def test_preferences_off_preserves_history_without_creating_push_debt(
    database_session_factory: Any,
) -> None:
    factory = database_session_factory
    _, _, owner = await setup(factory)
    await register_device(factory, owner, new_uuid7())
    async with factory() as session, session.begin():
        session.add(
            CustomerPreferences(
                user_id=owner.user_id, language="hi-IN", collection_notifications=False, version=2
            )
        )
    await event_for(factory, owner.user_id, "silent-history")
    await materialize(factory)
    async with factory() as session:
        assert (await session.scalar(select(CustomerNotification))).title == "संग्रह अपडेट"
        assert await session.scalar(select(func.count()).select_from(CustomerPushDelivery)) == 0


async def test_publication_requires_manager_draft_and_explicit_approval(
    database_session_factory: Any,
) -> None:
    factory = database_session_factory
    app, headers, manager = await setup(factory, roles=("MANAGER",))
    other, _ = await client_setup(factory)
    async with (
        AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers=headers
        ) as client,
        other,
    ):
        for slug in ["help", "about", "privacy", "terms", "unknown"]:
            assert (
                await client.get(f"/v1/content/{slug}", headers={"Authorization": ""})
            ).status_code == 404
        body = {
            "client_request_id": str(new_uuid7()),
            "expected_version": 0,
            "title": "Synthetic text",
            "paragraphs": ["Synthetic content for tests only."],
        }
        path = "/v1/manager/content/privacy/revisions"
        assert (await other.post(path, json=body)).status_code == 403
        draft = await client.post(path, json=body)
        assert draft.status_code == 200, draft.text
        assert (await client.post(path, json=body)).json() == draft.json()
        assert (await client.get("/v1/content/privacy")).status_code == 404
        publish = path + f"/{draft.json()['revision_id']}/publish"
        publish_body = {
            "client_request_id": str(new_uuid7()),
            "expected_version": 1,
            "approval_reference": "SYNTHETIC-TEST-APPROVAL",
        }
        result = await client.post(publish, json=publish_body)
        assert result.status_code == 200, result.text
        assert (await client.post(publish, json=publish_body)).json() == result.json()
        public = await client.get("/v1/content/privacy", headers={"Authorization": ""})
        assert public.status_code == 200 and public.json()["revision"] == 1
        assert public.headers["cache-control"] == "public, max-age=300"
        assert public.json()["paragraphs"] == body["paragraphs"]
        assert "approved_by" not in public.json()
        assert (
            await client.post(
                path,
                json={
                    **body,
                    "client_request_id": str(new_uuid7()),
                    "expected_version": 1,
                    "title": "<script>bad</script>",
                },
            )
        ).status_code == 422
    async with factory() as session:
        row = await session.scalar(select(ContentRevision))
        assert row.approved_by == manager.user_id
        assert row.approval_reference == "SYNTHETIC-TEST-APPROVAL"
        with pytest.raises(DBAPIError):
            async with session.begin_nested():
                await session.execute(update(ContentParagraph).values(body="tampering"))


async def test_database_owner_constraints_and_immutable_history(
    database_session_factory: Any,
) -> None:
    factory = database_session_factory
    _, _, owner = await setup(factory)
    _, _, stranger = await setup(factory)
    await register_device(factory, owner, new_uuid7())
    await event_for(factory, owner.user_id, "database-guard")
    await materialize(factory)
    async with factory() as session:
        notification = await session.scalar(select(CustomerNotification))
        binding = await session.scalar(select(CustomerPushBinding))
        with pytest.raises(DBAPIError):
            async with session.begin_nested():
                await session.execute(update(CustomerNotification).values(title="changed"))
        with pytest.raises(DBAPIError):
            async with session.begin_nested():
                session.add(
                    CustomerPushDelivery(
                        notification_id=notification.notification_id,
                        binding_id=binding.binding_id,
                        user_id=stranger.user_id,
                        binding_generation=1,
                    )
                )
                await session.flush()
        with pytest.raises(DBAPIError):
            async with session.begin_nested():
                session.add(
                    CustomerPreferences(
                        user_id=owner.user_id,
                        language="invalid",
                        collection_notifications=True,
                        version=0,
                    )
                )
                await session.flush()
        version = await session.scalar(text("SELECT version_num FROM alembic_version"))
        assert version == "0039_customer_capabilities"


async def test_disabled_adapter_retains_retry_debt_without_provider_invocation(
    database_session_factory: Any,
) -> None:
    factory = database_session_factory
    _, _, owner = await setup(factory)
    await register_device(factory, owner, new_uuid7())
    await event_for(factory, owner.user_id, "disabled")
    await materialize(factory)
    port = ConfiguredDelivery(config())
    assert await deliver_one(factory, CustomerDataProtection(config()), port)
    async with factory() as session:
        row = await session.scalar(select(CustomerPushDelivery))
        assert row.status == "PENDING" and row.failure_code == "DISABLED"


async def test_native_http_registration_validation_and_session_revoke(
    database_session_factory: Any,
) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    material = json.dumps(
        {
            "type": "service_account",
            "project_id": "synthetic-project",
            "private_key_id": "synthetic-only",
            "client_email": "test@synthetic-project.iam.gserviceaccount.com",
            "token_uri": "https://oauth2.googleapis.com/token",
            "private_key": key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ).decode(),
        }
    )
    factory = database_session_factory
    client, owner = await client_setup(
        factory,
        customer_fcm_enabled=True,
        fcm_project_id="synthetic-project",
        fcm_credentials_json=material,
    )
    device = str(new_uuid7())
    path = f"/v1/customer/me/push-devices/{device}"
    body = {
        "client_device_id": device,
        "platform": "ANDROID",
        "token_provider": "FCM",
        "token": "synthetic-private-device-token",
    }
    async with client:
        result = await client.put(path, json=body)
        assert result.status_code == 200, result.text
        assert set(result.json()) == {"client_device_id", "registered_at"}
        assert (await client.put(path, json=body)).json() == result.json()
        assert (
            await client.put(path, json={**body, "client_device_id": str(new_uuid7())})
        ).status_code == 422
        assert (await client.put(path, json={**body, "token_provider": "APNS"})).status_code == 422
        assert (await client.put(path, json={**body, "token": "private token invalid"})).json() == {
            "error": {"code": "INVALID_INPUT"}
        }
        assert (await client.delete(path)).status_code == 204
        assert (await client.delete(path)).status_code == 204
        # Replaying the old registration cannot undo revocation in this login session.
        assert (await client.put(path, json=body)).status_code == 409
    async with factory() as session:
        binding = await session.scalar(select(CustomerPushBinding))
        assert binding.session_id == owner.refresh_session_id and binding.revoked_at is not None


async def test_browser_subscription_contract_origin_fencing_and_key_rotation(
    database_session_factory: Any,
) -> None:
    factory = database_session_factory
    key = ec.generate_private_key(ec.SECP256R1())
    origin = "https://customer.example.test"
    client, owner = await client_setup(
        factory,
        customer_web_push_enabled=True,
        customer_web_push_origins=[origin],
        customer_web_push_endpoint_origins=["https://push.example.test"],
        customer_vapid_subject="mailto:synthetic@example.test",
        customer_vapid_private_key_pem=key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
    )
    device = str(new_uuid7())
    path = f"/v1/customer/me/web-push-subscriptions/{device}"
    public = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    body = {
        "endpoint": "https://push.example.test/private-endpoint",
        "expirationTime": None,
        "keys": {"p256dh": b64(public), "auth": b64(b"a" * 16)},
    }
    async with client:
        assert (await client.put(path, json=body)).status_code == 403
        assert (
            await client.put(path, json=body, headers={"Origin": "https://foreign.test"})
        ).status_code == 403
        first = await client.put(path, json=body, headers={"Origin": origin})
        assert first.status_code == 200, first.text
        assert (
            await client.put(path, json=body, headers={"Origin": origin})
        ).json() == first.json()
        for endpoint in ["https://127.0.0.1/secret", "https://push.example.test.evil.test/x"]:
            assert (
                await client.put(
                    path, json={**body, "endpoint": endpoint}, headers={"Origin": origin}
                )
            ).status_code == 422
        rotated = {**body, "keys": {**body["keys"], "auth": b64(b"b" * 16)}}
        assert (await client.put(path, json=rotated, headers={"Origin": origin})).status_code == 200
        assert (await client.put(path, json=body, headers={"Origin": origin})).status_code == 409
        async with factory() as session:
            bindings = list(await session.scalars(select(CustomerPushBinding)))
            assert len(bindings) == 2 and sum(b.revoked_at is None for b in bindings) == 1
            assert len({b.token_hmac for b in bindings}) == 1
            assert all(b"private-endpoint" not in b.token_encrypted for b in bindings)
        assert (await client.delete(path, headers={"Origin": origin})).status_code == 204
        assert (await client.delete(path, headers={"Origin": origin})).status_code == 204


async def test_notification_claim_allows_only_one_simultaneous_sender(
    database_session_factory: Any,
) -> None:
    factory = database_session_factory
    _, _, owner = await setup(factory)
    await register_device(factory, owner, new_uuid7())
    await event_for(factory, owner.user_id, "one-sender")
    await materialize(factory)
    sending, release = asyncio.Event(), asyncio.Event()

    class PausedDelivery(Delivery):
        async def send(self, *args: Any) -> str:
            sending.set()
            await release.wait()
            return await super().send(*args)

    port = PausedDelivery(["SENT"])
    protection = CustomerDataProtection(config())
    first = asyncio.create_task(deliver_one(factory, protection, port))
    await asyncio.wait_for(sending.wait(), 10)
    assert not await deliver_one(factory, protection, port)
    release.set()
    assert await first
    assert len(port.sent) == 1


async def test_lossy_downgrade_refuses_customer_history(database_session_factory: Any) -> None:
    client, _ = await client_setup(database_session_factory)
    async with client:
        response = await client.post(
            "/v1/customer/feedback",
            json={"client_request_id": str(new_uuid7()), "message": "Synthetic retained feedback"},
        )
        assert response.status_code == 201
    with pytest.raises(DBAPIError, match="export customer capability data"):
        await asyncio.to_thread(
            alembic_command.downgrade, Config("alembic.ini"), "0038_item_volume"
        )
    async with database_session_factory() as session:
        assert (
            await session.scalar(text("SELECT version_num FROM alembic_version"))
            == "0039_customer_capabilities"
        )
        assert await session.scalar(select(func.count()).select_from(CustomerFeedback)) == 1


async def test_commit_failure_cannot_send_http_success_and_retry_is_clean(
    database_session_factory: Any,
) -> None:
    factory = database_session_factory
    app, headers, _ = await setup(factory)
    failing = True

    def failing_factory() -> Any:
        session = factory()

        def before_commit(sync_session: Any) -> None:
            if failing and sync_session.scalar(select(func.count()).select_from(CustomerFeedback)):
                raise RuntimeError("synthetic commit failure")

        event.listen(session.sync_session, "before_commit", before_commit)
        return session

    app.state.database_session_factory = failing_factory
    body = {"client_request_id": str(new_uuid7()), "message": "Synthetic commit failure feedback"}
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
        headers=headers,
    ) as client:
        assert (await client.post("/v1/customer/feedback", json=body)).status_code == 500
        async with factory() as session:
            assert await session.scalar(select(func.count()).select_from(CustomerFeedback)) == 0
        failing = False
        result = await client.post("/v1/customer/feedback", json=body)
        assert result.status_code == 201, result.text
        assert (await client.post("/v1/customer/feedback", json=body)).json() == result.json()


async def test_domain_event_replay_survives_missing_inbox_without_expanding_devices(
    database_session_factory: Any,
) -> None:
    factory = database_session_factory
    _, _, owner = await setup(factory)
    await register_device(factory, owner, new_uuid7())
    await event_for(factory, owner.user_id, "independent-business-replay")
    assert await materialize(factory) == 1
    await register_device(factory, owner, new_uuid7(), "another-device-token")
    async with factory() as session, session.begin():
        await session.execute(
            delete(InboxMessage).where(
                InboxMessage.consumer_name == "customer-notification-history"
            )
        )
    assert await materialize(factory) == 1
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(CustomerNotification)) == 1
        assert await session.scalar(select(func.count()).select_from(CustomerPushDelivery)) == 1
        assert await session.scalar(select(func.count()).select_from(InboxMessage)) == 1


async def test_finite_worker_entrypoint_materializes_with_providers_disabled(
    database_session_factory: Any,
) -> None:
    factory = database_session_factory
    _, _, owner = await setup(factory)
    await register_device(factory, owner, new_uuid7())
    await event_for(factory, owner.user_id, "standalone-worker")
    environment = dict(
        os.environ,
        TIRODHAN_DATABASE_URL=os.environ["TIRODHAN_TEST_DATABASE_URL"],
        TIRODHAN_OUTBOX_PUBLISH_BATCH_SIZE="10",
        TIRODHAN_CUSTOMER_FCM_ENABLED="false",
        TIRODHAN_CUSTOMER_WEB_PUSH_ENABLED="false",
    )
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "tirodhan.workers.customer_notifications"],
        env=environment,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr.decode()
    assert b"synthetic-fcm" not in result.stdout + result.stderr
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(CustomerNotification)) == 1
        delivery = await session.scalar(select(CustomerPushDelivery))
        assert delivery.status == "PENDING" and delivery.attempts == 0


async def test_concurrent_customer_device_claims_leave_one_latest_session_owner(
    database_session_factory: Any,
) -> None:
    factory = database_session_factory
    _, _, older = await setup(factory)
    _, _, newer = await setup(factory)
    device = new_uuid7()
    # Establish both connections before timing the race; connection startup is not its invariant.
    async with factory() as left, factory() as right:
        await left.execute(text("SELECT 1"))
        await right.execute(text("SELECT 1"))
    ready = asyncio.Event()
    arrived = 0

    async def claim(principal: Any) -> Any:
        nonlocal arrived
        arrived += 1
        if arrived == 2:
            ready.set()
        await ready.wait()
        return await register_device(factory, principal, device, "shared-device-token")

    results = await asyncio.wait_for(
        asyncio.gather(claim(older), claim(newer), return_exceptions=True), 15
    )
    assert not isinstance(results[1], Exception)
    if isinstance(results[0], Exception):
        assert isinstance(results[0], CustomerReadError) and results[0].status == 409
    async with factory() as session:
        active = list(
            await session.scalars(
                select(CustomerPushBinding).where(CustomerPushBinding.revoked_at.is_(None))
            )
        )
        assert len(active) == 1
        assert active[0].user_id == newer.user_id
        assert active[0].session_id == newer.refresh_session_id
