from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from tirodhan.core.config import Settings
from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.dispatch import push as implementation
from tirodhan.modules.dispatch.events import (
    DispatchMessage,
    InvalidDispatchMessageError,
    parse_dispatch_message,
)
from tirodhan.modules.dispatch.push import (
    FcmPushNotificationPort,
    PushConfigurationError,
    PushNotification,
    PushRecipient,
    PushUnavailableError,
)


def adapter(monkeypatch):
    monkeypatch.setattr(implementation.credentials, "Certificate", lambda material: object())
    monkeypatch.setattr(implementation, "initialize_app", lambda *args, **kwargs: object())
    return FcmPushNotificationPort(
        Settings(_env_file=None, fcm_project_id="test-project", fcm_credentials_json="{}")
    )


def test_fcm_requires_real_runtime_configuration():
    with pytest.raises(PushConfigurationError):
        FcmPushNotificationPort(Settings(_env_file=None))
    with pytest.raises(PushConfigurationError):
        FcmPushNotificationPort(
            Settings(_env_file=None, fcm_project_id="test", fcm_credentials_json="not-json")
        )


@pytest.mark.asyncio
async def test_fcm_real_sdk_multicast_chunks_start_concurrently(monkeypatch, caplog):
    port = adapter(monkeypatch)
    started = []
    both = threading.Event()
    recipients = tuple(PushRecipient(new_uuid7(), f"private-token-{index}") for index in range(501))
    notification = PushNotification(new_uuid7(), 2)

    def send(message, **kwargs):
        with pytest.raises(RuntimeError):
            asyncio.get_running_loop()
        started.append(message)
        if len(started) == 2:
            both.set()
        assert both.wait(2)
        assert message.data == notification.data()
        assert set(message.data) == {"type", "collection_group_id", "offer_round"}
        assert message.notification.title == "New pickup opportunity"
        assert message.notification.body == "Open Tirodhan to review and accept."
        return SimpleNamespace(
            responses=[
                SimpleNamespace(success=True, message_id="provider-id", exception=None)
                for _ in message.tokens
            ]
        )

    monkeypatch.setattr(implementation.messaging, "send_each_for_multicast", send)
    results = await port.send_batch(recipients, notification)
    assert [len(message.tokens) for message in started] == [500, 1]
    assert [result.delivery_id for result in results] == [
        recipient.delivery_id for recipient in recipients
    ]
    assert all(result.status == "SENT" for result in results)
    assert "private-token" not in caplog.text
    assert "private-token" not in repr(recipients)


@pytest.mark.asyncio
async def test_fcm_maps_per_device_permanent_and_transient_failures(monkeypatch):
    port = adapter(monkeypatch)
    errors = [
        implementation.messaging.UnregisteredError("private"),
        implementation.exceptions.InvalidArgumentError("private"),
        implementation.exceptions.UnavailableError("private"),
    ]

    def send(*args, **kwargs):
        return SimpleNamespace(
            responses=[
                SimpleNamespace(success=False, message_id=None, exception=error) for error in errors
            ]
        )

    monkeypatch.setattr(implementation.messaging, "send_each_for_multicast", send)
    results = await port.send_batch(
        [PushRecipient(new_uuid7(), "private") for _ in errors], PushNotification(new_uuid7(), 1)
    )
    assert [result.status for result in results] == [
        "PERMANENTLY_FAILED",
        "PERMANENTLY_FAILED",
        "PENDING",
    ]


@pytest.mark.asyncio
async def test_fcm_ambiguous_exception_never_exposes_provider_text(monkeypatch):
    port = adapter(monkeypatch)

    def send(*args, **kwargs):
        raise RuntimeError("private-registration-token")

    monkeypatch.setattr(implementation.messaging, "send_each_for_multicast", send)
    with pytest.raises(PushUnavailableError) as error:
        await port.send_batch(
            [PushRecipient(new_uuid7(), "private-registration-token")],
            PushNotification(new_uuid7(), 1),
        )
    assert "private" not in str(error.value)
    assert error.value.__suppress_context__


def test_offer_lifetime_requires_positive_configuration():
    for value in (0, -1):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, rider_offer_lifetime_seconds=value)
    assert Settings(_env_file=None).rider_offer_lifetime_seconds is None


def test_dispatch_message_rejects_extra_sensitive_fields():
    from datetime import timedelta

    now = utc_now()
    payload = DispatchMessage(
        new_uuid7(), new_uuid7(), "8764a9d4dffffff", now, now + timedelta(hours=1), "FLEET_FIRST"
    ).payload()
    assert parse_dispatch_message(json.dumps(payload).encode()).payload() == payload
    for field in ("address", "latitude", "longitude", "phone", "customer_id", "registration_token"):
        with pytest.raises(InvalidDispatchMessageError):
            parse_dispatch_message(json.dumps({**payload, field: "private"}).encode())


@pytest.mark.asyncio
async def test_fcm_cleanup_runs_outside_callers_event_loop(monkeypatch):
    port = adapter(monkeypatch)
    closed = []

    def close(app):
        with pytest.raises(RuntimeError):
            asyncio.get_running_loop()
        closed.append(app)

    monkeypatch.setattr(implementation, "delete_app", close)
    await port.close()
    assert closed == [port._app]


@pytest.mark.asyncio
async def test_dispatch_broker_adapter_reads_body_once_and_settles():
    from tirodhan.modules.reliability.service_bus import AzureDispatchDelivery

    calls = []

    class Receiver:
        async def complete_message(self, message):
            calls.append("complete")

        async def abandon_message(self, message):
            calls.append("abandon")

        async def dead_letter_message(self, message, **kwargs):
            calls.append(kwargs["reason"])

    body = b"x" * 1000
    message = SimpleNamespace(
        body=iter([body]), message_id="internal-id", subject="CollectionGroupDispatchRequested"
    )
    delivery = AzureDispatchDelivery(Receiver(), message)
    assert delivery.body == body
    await delivery.complete()
    await delivery.abandon()
    await delivery.dead_letter()
    assert calls == ["complete", "abandon", "INVALID_DISPATCH_MESSAGE"]
