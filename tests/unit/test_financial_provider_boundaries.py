"""Documented HTTP boundaries with no database or live provider dependency."""

import asyncio
import json

import httpx
import pytest
from test_razorpay import collection, configuration, signed

from tirodhan.api.dependencies import get_session_factory
from tirodhan.db.values import new_uuid7
from tirodhan.main import create_app
from tirodhan.modules.payments.ports import PaymentProviderUncertainError
from tirodhan.modules.payments.razorpay import RazorpayProvider
from tirodhan.modules.payments.webhook_queue import MAX_WEBHOOK_BYTES


class Publisher:
    def __init__(self):
        self.entered, self.resume = asyncio.Event(), asyncio.Event()
        self.resume.set()
        self.messages = []
        self.fail = False

    async def send(self, entity, message):
        self.entered.set()
        await self.resume.wait()
        if self.fail:
            raise RuntimeError("synthetic broker unavailable")
        self.messages.append((entity, message))


def body():
    return json.dumps(
        dict(
            event="payment.captured",
            payload=dict(
                payment=dict(
                    entity=dict(
                        entity="payment",
                        id="pay_webhook",
                        order_id="order_webhook",
                        amount=500,
                        currency="INR",
                        status="captured",
                        captured=True,
                        notes={},
                        email="private-marker",
                        contact="private-marker",
                    )
                )
            ),
        )
    ).encode()


def app_for(provider, publisher, **settings):
    app = create_app(
        configuration(
            financial_webhook_queue_name="financial-webhook",
            financial_webhook_send_timeout_seconds=0.5,
            **settings,
        ),
        payment_provider=provider,
        financial_webhook_publisher=publisher,
    )

    def forbidden_db():
        raise AssertionError("Webhook HTTP must not resolve a database dependency")

    app.dependency_overrides[get_session_factory] = forbidden_db
    return app


@pytest.mark.asyncio
async def test_http_ack_waits_for_broker_and_exact_replay_keeps_minimal_identity():
    publisher = Publisher()
    publisher.resume.clear()
    async with httpx.AsyncClient() as remote:
        provider = RazorpayProvider(configuration(), client=remote)
        app = app_for(provider, publisher)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as api:
            raw = body()
            task = asyncio.create_task(
                api.post("/v1/payments/provider/webhook", content=raw, headers=signed(raw))
            )
            await asyncio.wait_for(publisher.entered.wait(), 1)
            assert not task.done()
            concurrent = asyncio.create_task(
                api.post("/v1/payments/provider/webhook", content=raw, headers=signed(raw))
            )
            publisher.resume.set()
            first = await task
            simultaneous = await concurrent
            replay = await api.post(
                "/v1/payments/provider/webhook", content=raw, headers=signed(raw)
            )
    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json()
    assert simultaneous.status_code == 200 and simultaneous.json() == first.json()
    assert first.json()["processing_status"] == "QUEUED"
    assert len(publisher.messages) == 3
    assert publisher.messages[0][1] == publisher.messages[1][1]
    assert b"private-marker" not in publisher.messages[0][1].body


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["send", "timeout", "oversize", "signature", "missing_id"])
async def test_http_failures_are_not_acknowledged_and_never_touch_db(failure):
    publisher = Publisher()
    if failure == "send":
        publisher.fail = True
    if failure == "timeout":
        publisher.resume.clear()
    raw = body() if failure != "oversize" else b" " * (MAX_WEBHOOK_BYTES + 1)
    headers = signed(raw)
    if failure == "signature":
        headers["X-Razorpay-Signature"] = "0" * 64
    if failure == "missing_id":
        headers.pop("x-razorpay-event-id")
    async with httpx.AsyncClient() as remote:
        app = app_for(RazorpayProvider(configuration(), client=remote), publisher)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as api:
            result = await api.post("/v1/payments/provider/webhook", content=raw, headers=headers)
    assert (
        result.status_code
        == dict(send=503, timeout=503, oversize=413, signature=401, missing_id=422)[failure]
    )
    assert not publisher.messages


def refunds(count):
    return [
        dict(
            entity="refund",
            id=f"rfnd_item{i}",
            payment_id="pay_charge",
            amount=1,
            currency="INR",
            status="pending",
            receipt=None,
            notes={},
        )
        for i in range(count)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [0, 99, 100, 101, 203])
async def test_supported_refund_pagination_requires_exhaustion_even_at_exactly_100(count):
    items, calls = refunds(count), []

    def transport(request):
        assert request.method == "GET" and request.url.path == "/v1/payments/pay_charge/refunds"
        assert request.url.params["count"] == "100"
        skip = int(request.url.params["skip"])
        calls.append(skip)
        return httpx.Response(200, json=collection(items[skip : skip + 100]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        result = await RazorpayProvider(configuration(), client=client).inspect_charge_refunds(
            "pay_charge"
        )
    assert len(result) == count
    expected = list(range(0, (count // 100 + 1) * 100, 100))
    assert calls == expected * 2


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["duplicate", "conflict", "oversized", "unstable", "budget"])
async def test_inventory_duplicate_or_incomplete_coverage_never_becomes_failure_proof(mode):
    items = refunds(101)
    items[-1] = dict(items[0], amount=2 if mode == "conflict" else 1)
    iteration = -1

    def transport(request):
        nonlocal iteration
        skip = int(request.url.params["skip"])
        if skip == 0:
            iteration += 1
        page = items[skip : skip + 100]
        if mode == "oversized":
            page = refunds(101)
        if mode == "unstable":
            page = [dict(i, status=["pending", "failed", "processed"][iteration]) for i in page]
        return httpx.Response(200, json=collection(page))

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        provider = RazorpayProvider(
            configuration(financial_inventory_page_budget=1 if mode == "budget" else 5),
            client=client,
        )
        if mode == "duplicate":
            assert len(await provider.inspect_charge_refunds("pay_charge")) == 100
        else:
            with pytest.raises(PaymentProviderUncertainError):
                await provider.inspect_charge_refunds("pay_charge")


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["notes", "receipt"])
async def test_unknown_refund_recovery_requires_both_original_correlators(mismatch):
    identity = new_uuid7()
    item = dict(
        refunds(1)[0], receipt=f"rf_{identity.hex}", notes={"tirodhan_refund_id": str(identity)}
    )
    if mismatch == "notes":
        item["notes"] = {"tirodhan_refund_id": str(new_uuid7())}
    else:
        item["receipt"] = "rf_unrelated"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=collection([item])))
    ) as client:
        with pytest.raises(PaymentProviderUncertainError) as caught:
            await RazorpayProvider(configuration(), client=client).inquire_refund(
                refund_id=identity,
                provider_refund_id=None,
                provider_payment_id="pay_charge",
                amount_minor=1,
                currency="INR",
            )
        assert caught.value.failure_code == "INQUIRY_REFUND_CORRELATION_CONFLICT"
