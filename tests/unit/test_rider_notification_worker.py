from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from azure.servicebus import ServiceBusReceiveMode

from tirodhan.core.config import Settings
from tirodhan.workers import rider_notifications as worker


def runtime_settings(**overrides):
    return Settings(
        _env_file=None,
        service_bus_namespace="test.servicebus.windows.net",
        rider_notification_queue_name="rider-notifications",
        service_bus_operation_timeout_seconds=5,
        rider_offer_lifetime_seconds=60,
        serviceability_lock_renewal_seconds=90,
        **overrides,
    )


@pytest.mark.asyncio
async def test_worker_requires_dedicated_lock_renewal_before_opening_resources(monkeypatch):
    settings = runtime_settings(rider_notification_lock_renewal_seconds=None)
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(worker, "configure_logging", lambda *args: None)

    def unexpected_resource(*args, **kwargs):
        pytest.fail("missing lock renewal must fail before opening runtime resources")

    monkeypatch.setattr(worker, "FcmPushNotificationPort", unexpected_resource)
    monkeypatch.setattr(worker, "create_database_engine", unexpected_resource)
    monkeypatch.setattr(worker, "DefaultAzureCredential", unexpected_resource)
    with pytest.raises(ValueError, match="lock renewal duration must be configured"):
        await worker.run()


@pytest.mark.asyncio
@pytest.mark.parametrize("first_delivery_fails", [False, True])
async def test_worker_renews_every_message_before_delivery_and_keeps_context_open(
    monkeypatch, first_delivery_fails
):
    settings = runtime_settings(rider_notification_lock_renewal_seconds=180)
    messages = [object(), object()]
    registered = []
    handled = []
    lifecycle = []
    sessions = object()

    @asynccontextmanager
    async def context(name, value):
        lifecycle.append((name, "entered"))
        try:
            yield value
        finally:
            lifecycle.append((name, "exited"))

    async def receive():
        for message in messages:
            yield message

    receiver = receive()

    def queue_receiver(**kwargs):
        assert kwargs == {
            "queue_name": settings.rider_notification_queue_name,
            "receive_mode": ServiceBusReceiveMode.PEEK_LOCK,
            "prefetch_count": 0,
        }
        return context("receiver", receiver)

    def register(received_from, message, *, max_lock_renewal_duration):
        assert received_from is receiver
        assert ("renewer", "entered") in lifecycle
        assert ("renewer", "exited") not in lifecycle
        assert max_lock_renewal_duration == 180
        registered.append(message)

    async def handle(message, session_factory, *, lifetime_seconds, push):
        assert registered[-1] is message
        assert session_factory is sessions
        assert lifetime_seconds == settings.rider_offer_lifetime_seconds
        assert push is push_port
        await asyncio.sleep(0)
        assert ("renewer", "exited") not in lifecycle
        assert ("receiver", "exited") not in lifecycle
        handled.append(message)
        if first_delivery_fails and message is messages[0]:
            raise RuntimeError("delivery failed")

    async def close_push():
        lifecycle.append(("push", "closed"))

    async def dispose_engine():
        lifecycle.append(("engine", "disposed"))

    push_port = SimpleNamespace(close=close_push)
    engine = SimpleNamespace(dispose=dispose_engine)
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(worker, "configure_logging", lambda *args: None)
    monkeypatch.setattr(worker, "FcmPushNotificationPort", lambda configured: push_port)
    monkeypatch.setattr(worker, "create_database_engine", lambda configured: engine)
    monkeypatch.setattr(worker, "create_session_factory", lambda created: sessions)
    monkeypatch.setattr(
        worker, "DefaultAzureCredential", lambda **kwargs: context("credential", object())
    )
    monkeypatch.setattr(
        worker,
        "ServiceBusClient",
        lambda **kwargs: context("bus", SimpleNamespace(get_queue_receiver=queue_receiver)),
    )
    monkeypatch.setattr(
        worker, "AutoLockRenewer", lambda: context("renewer", SimpleNamespace(register=register))
    )
    monkeypatch.setattr(worker, "AzureDispatchDelivery", lambda source, message: message)
    monkeypatch.setattr(worker, "handle_dispatch_delivery", handle)

    await worker.run()

    assert registered == messages
    assert handled == messages
    assert lifecycle == [
        ("credential", "entered"),
        ("bus", "entered"),
        ("renewer", "entered"),
        ("receiver", "entered"),
        ("receiver", "exited"),
        ("renewer", "exited"),
        ("bus", "exited"),
        ("credential", "exited"),
        ("push", "closed"),
        ("engine", "disposed"),
    ]
