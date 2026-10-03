from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from azure.servicebus import ServiceBusReceiveMode

from tirodhan.core.config import Settings
from tirodhan.workers import refunds as worker


@pytest.mark.asyncio
@pytest.mark.parametrize("setting", ["refund_queue_name", "refund_lock_renewal_seconds"])
async def test_refund_worker_requires_dedicated_runtime_settings(monkeypatch, setting):
    settings = Settings(
        _env_file=None,
        service_bus_namespace="test.servicebus.windows.net",
        service_bus_operation_timeout_seconds=5,
        **{setting: None},
    )
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(worker, "configure_logging", lambda *args: None)
    monkeypatch.setattr(
        worker,
        "create_database_engine",
        lambda *args: pytest.fail("no resources before configuration validation"),
    )
    with pytest.raises(ValueError, match="configuration is incomplete"):
        await worker.run()


@pytest.mark.parametrize("value", [0, -1])
def test_refund_lock_renewal_setting_is_positive(value):
    with pytest.raises(ValueError):
        Settings(_env_file=None, refund_lock_renewal_seconds=value)


@pytest.mark.asyncio
async def test_refund_worker_uses_peek_lock_zero_prefetch_and_registers_before_handler(monkeypatch):
    settings = Settings(
        _env_file=None,
        service_bus_namespace="test.servicebus.windows.net",
        service_bus_operation_timeout_seconds=5,
        refund_queue_name="refunds",
        refund_lock_renewal_seconds=180,
    )
    lifecycle, registered, handled = [], [], []
    provider = object()
    messages = [object(), object()]

    @asynccontextmanager
    async def context(name, value):
        lifecycle.append((name, "enter"))
        try:
            yield value
        finally:
            lifecycle.append((name, "exit"))

    async def receive():
        for message in messages:
            yield message

    receiver = receive()

    def queue_receiver(**kwargs):
        assert kwargs == {
            "queue_name": "refunds",
            "receive_mode": ServiceBusReceiveMode.PEEK_LOCK,
            "prefetch_count": 0,
        }
        return context("receiver", receiver)

    def register(source, message, *, max_lock_renewal_duration):
        assert source is receiver and max_lock_renewal_duration == 180
        assert ("renewer", "exit") not in lifecycle
        registered.append(message)

    async def handle(message, factory, *, provider):
        assert registered[-1] is message
        assert ("renewer", "exit") not in lifecycle
        handled.append(message)

    async def dispose():
        lifecycle.append(("engine", "disposed"))

    engine = SimpleNamespace(dispose=dispose)
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(worker, "configure_logging", lambda *args: None)
    monkeypatch.setattr(worker, "razorpay_configured", lambda value: True)
    monkeypatch.setattr(worker, "razorpay_runtime", lambda value: context("provider", provider))
    monkeypatch.setattr(worker, "create_database_engine", lambda value: engine)
    monkeypatch.setattr(worker, "create_session_factory", lambda value: object())
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
    monkeypatch.setattr(worker, "AzureRefundDelivery", lambda source, message: message)
    monkeypatch.setattr(worker, "handle_refund_delivery", handle)
    await worker.run()
    assert registered == handled == messages
    assert lifecycle[-1] == ("engine", "disposed")
    assert (
        lifecycle.index(("receiver", "exit"))
        < lifecycle.index(("renewer", "exit"))
        < lifecycle.index(("provider", "exit"))
    )
