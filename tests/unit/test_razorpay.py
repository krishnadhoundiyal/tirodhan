from __future__ import annotations

import base64
import hashlib
import hmac
import json

import httpx
import pytest
from pydantic import ValidationError

from tirodhan.core.config import Settings
from tirodhan.db.values import new_uuid7
from tirodhan.main import create_app
from tirodhan.modules.payments.ports import (
    CheckoutConfirmationError,
    PaymentEventOutcome,
    PaymentInitiationOutcome,
    PaymentProviderAuthenticationError,
    PaymentProviderNotConfiguredError,
    PaymentProviderUncertainError,
    RefundInitiationOutcome,
)
from tirodhan.modules.payments.razorpay import RazorpayProvider, razorpay_configured
from tirodhan.modules.payments.runtime import razorpay_runtime


def configured_settings(**overrides):
    return Settings(
        _env_file=None,
        razorpay_key_id="test-key",
        razorpay_key_secret="private-merchant-secret",
        razorpay_webhook_secret="private-webhook-secret",
        razorpay_http_timeout_seconds=5,
        **overrides,
    )


def sign(body):
    return hmac.new(b"private-webhook-secret", body, hashlib.sha256).hexdigest()


def payment_body(event="payment.captured", **fields):
    payment = {
        "entity": "payment",
        "id": "pay_Test",
        "order_id": "order_Test",
        "amount": 500,
        "currency": "INR",
        "status": "captured",
        "captured": True,
    }
    payment.update(fields)
    payload = {"payment": {"entity": payment}}
    if event == "order.paid":
        payload["order"] = {
            "entity": {
                "entity": "order",
                "id": "order_Test",
                "amount": 500,
                "currency": "INR",
                "status": "paid",
                "notes": {},
            }
        }
    return json.dumps({"entity": "event", "event": event, "payload": payload}).encode()


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), 61])
def test_razorpay_timeout_is_finite_positive_bounded(timeout):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, razorpay_http_timeout_seconds=timeout)


def test_missing_partial_and_secret_configuration():
    assert not razorpay_configured(Settings(_env_file=None))
    with pytest.raises(PaymentProviderNotConfiguredError, match="incomplete"):
        create_app(Settings(_env_file=None, razorpay_key_id="test-key"))
    settings = configured_settings()
    assert "private-merchant-secret" not in repr(settings)
    assert "private-webhook-secret" not in repr(settings)


@pytest.mark.parametrize(
    "url",
    [
        "http://api.razorpay.com/v1",
        "https://user:pass@example.com/v1",
        "https://example.com/v1?secret=x",
    ],
)
def test_invalid_provider_url_fails_closed(url):
    with pytest.raises(PaymentProviderNotConfiguredError):
        razorpay_configured(configured_settings(razorpay_api_base_url=url))


@pytest.mark.asyncio
async def test_order_request_response_and_stable_receipt(caplog):
    attempt_id = new_uuid7()
    requests = []

    def transport(request):
        requests.append(request)
        data = json.loads(request.content)
        assert str(request.url) == "https://api.razorpay.com/v1/orders"
        assert request.method == "POST"
        assert (
            request.headers["authorization"]
            == "Basic " + base64.b64encode(b"test-key:private-merchant-secret").decode()
        )
        assert not any("idempotency" in key for key in request.headers)
        assert data == {
            "amount": 12345,
            "currency": "INR",
            "receipt": f"ta_{attempt_id.hex}",
            "partial_payment": False,
            "notes": {"tirodhan_payment_attempt_id": str(attempt_id)},
        }
        assert len(data["receipt"]) == 35
        return httpx.Response(200, json={"entity": "order", "id": "order_Test", **data})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        provider = RazorpayProvider(configured_settings(), client=client)
        result = await provider.initiate_payment(
            payment_attempt_id=attempt_id,
            amount_minor=12345,
            currency="INR",
            provider_idempotency_key=f"payment-attempt:{attempt_id}",
        )
        assert result.outcome == PaymentInitiationOutcome.READY
        assert result.provider_order_id == "order_Test"
        assert result.provider_payment_id is None
        assert len(requests) == 1
        assert "private-merchant-secret" not in caplog.text


@pytest.mark.parametrize(
    "mutation",
    [
        {"entity": "payment"},
        {"id": ""},
        {"amount": 501},
        {"amount": True},
        {"currency": "USD"},
        {"receipt": "other"},
    ],
)
@pytest.mark.asyncio
async def test_order_response_invalid_is_uncertain(mutation):
    def transport(request):
        return httpx.Response(
            200,
            json={"entity": "order", "id": "order_Test", **json.loads(request.content), **mutation},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(PaymentProviderUncertainError):
            await RazorpayProvider(configured_settings(), client=client).initiate_payment(
                payment_attempt_id=new_uuid7(),
                amount_minor=500,
                currency="INR",
                provider_idempotency_key="internal",
            )


@pytest.mark.parametrize("operation", ["payment", "refund"])
@pytest.mark.parametrize(
    "failure", ["timeout", "connection", "server", "malformed", "rate_limit", "duplicate"]
)
@pytest.mark.asyncio
async def test_ambiguous_http_failures_are_controlled_uncertain(operation, failure, caplog):
    def transport(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("private-response", request=request)
        if failure == "connection":
            raise httpx.ConnectError("private-response", request=request)
        return httpx.Response(
            {"server": 500, "rate_limit": 429, "duplicate": 400}.get(failure, 200),
            json={"error": {"description": "private-response"}},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        provider = RazorpayProvider(configured_settings(), client=client)
        with pytest.raises(PaymentProviderUncertainError) as error:
            if operation == "payment":
                await provider.initiate_payment(
                    payment_attempt_id=new_uuid7(),
                    amount_minor=500,
                    currency="INR",
                    provider_idempotency_key="key",
                )
            else:
                await provider.initiate_refund(
                    refund_id=new_uuid7(),
                    provider_payment_id="pay_Test",
                    amount_minor=100,
                    currency="INR",
                    provider_idempotency_key="key",
                )
        assert "private-response" not in str(error.value)
        assert "private-response" not in caplog.text


@pytest.mark.parametrize("operation", ["payment", "refund"])
@pytest.mark.parametrize("status", [400, 401, 403])
@pytest.mark.asyncio
async def test_merchant_failure_is_configuration_not_financial_failure(operation, status):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                status,
                json={
                    "error": {"code": "BAD_REQUEST_ERROR", "description": "Authentication failed"}
                },
            )
        )
    ) as client:
        provider = RazorpayProvider(configured_settings(), client=client)
        with pytest.raises(PaymentProviderNotConfiguredError):
            if operation == "payment":
                await provider.initiate_payment(
                    payment_attempt_id=new_uuid7(),
                    amount_minor=500,
                    currency="INR",
                    provider_idempotency_key="key",
                )
            else:
                await provider.initiate_refund(
                    refund_id=new_uuid7(),
                    provider_payment_id="pay_Test",
                    amount_minor=100,
                    currency="INR",
                    provider_idempotency_key="key",
                )


@pytest.mark.asyncio
async def test_definitive_validation_rejection_maps_to_failed():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                400,
                json={
                    "error": {
                        "code": "BAD_REQUEST_ERROR",
                        "reason": "input_validation_failed",
                        "field": "amount",
                        "description": "not retained",
                    }
                },
            )
        )
    ) as client:
        provider = RazorpayProvider(configured_settings(), client=client)
        payment = await provider.initiate_payment(
            payment_attempt_id=new_uuid7(),
            amount_minor=500,
            currency="INR",
            provider_idempotency_key="key",
        )
        refund = await provider.initiate_refund(
            refund_id=new_uuid7(),
            provider_payment_id="pay_Test",
            amount_minor=100,
            currency="INR",
            provider_idempotency_key="key",
        )
        assert payment.outcome == PaymentInitiationOutcome.FAILED
        assert refund.outcome == RefundInitiationOutcome.FAILED
        assert payment.failure_code == refund.failure_code == "RAZORPAY_AMOUNT_REJECTED"


@pytest.mark.parametrize(
    "status,outcome",
    [
        ("processed", RefundInitiationOutcome.SUCCEEDED),
        ("pending", RefundInitiationOutcome.SUBMITTED),
        ("failed", RefundInitiationOutcome.FAILED),
    ],
)
@pytest.mark.asyncio
async def test_refund_exact_request_native_idempotency_and_response(status, outcome):
    refund_id = new_uuid7()
    key = f"refund:{refund_id}"

    def transport(request):
        assert request.url.path == "/v1/payments/pay_Test/refund"
        assert request.headers["x-refund-idempotency"] == hashlib.sha256(key.encode()).hexdigest()
        assert json.loads(request.content) == {
            "amount": 100,
            "speed": "normal",
            "notes": {"tirodhan_refund_id": str(refund_id)},
        }
        return httpx.Response(
            200,
            json={
                "entity": "refund",
                "id": "rfnd_Test",
                "payment_id": "pay_Test",
                "amount": 100,
                "currency": "INR",
                "status": status,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        result = await RazorpayProvider(configured_settings(), client=client).initiate_refund(
            refund_id=refund_id,
            provider_payment_id="pay_Test",
            amount_minor=100,
            currency="INR",
            provider_idempotency_key=key,
        )
    assert result.outcome == outcome
    assert result.provider_refund_id == "rfnd_Test"


@pytest.mark.parametrize(
    "mutation",
    [
        {"entity": "order"},
        {"id": ""},
        {"payment_id": "pay_Other"},
        {"amount": 500},
        {"currency": "USD"},
        {"status": "unknown"},
        {"status": []},
    ],
)
@pytest.mark.asyncio
async def test_invalid_refund_response_is_uncertain(mutation):
    response = {
        "entity": "refund",
        "id": "rfnd_Test",
        "payment_id": "pay_Test",
        "amount": 100,
        "currency": "INR",
        "status": "processed",
        **mutation,
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response))
    ) as client:
        with pytest.raises(PaymentProviderUncertainError):
            await RazorpayProvider(configured_settings(), client=client).initiate_refund(
                refund_id=new_uuid7(),
                provider_payment_id="pay_Test",
                amount_minor=100,
                currency="INR",
                provider_idempotency_key="key",
            )


@pytest.mark.parametrize(
    "event,status,captured,outcome",
    [
        ("payment.captured", "captured", True, PaymentEventOutcome.SUCCEEDED),
        ("order.paid", "captured", True, PaymentEventOutcome.SUCCEEDED),
        ("payment.failed", "failed", False, PaymentEventOutcome.FAILED),
        ("payment.authorized", "authorized", False, PaymentEventOutcome.IGNORED),
        ("unhandled.event", "captured", True, PaymentEventOutcome.IGNORED),
    ],
)
@pytest.mark.asyncio
async def test_raw_webhook_signature_and_event_mapping(event, status, captured, outcome):
    body = payment_body(event, status=status, captured=captured)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: pytest.fail("webhook must not call provider"))
    ) as client:
        provider = RazorpayProvider(configured_settings(), client=client)
        mapped = await provider.authenticate_webhook(
            raw_body=body,
            headers={"X-Razorpay-Signature": sign(body), "x-razorpay-event-id": "event_Test"},
        )
        assert mapped.external_event_id == "event_Test"
        assert mapped.outcome == outcome
        for modified, signature in [(body, ""), (body, "0" * 64), (body + b" ", sign(body))]:
            with pytest.raises(PaymentProviderAuthenticationError):
                await provider.authenticate_webhook(
                    raw_body=modified, headers={"x-razorpay-signature": signature}
                )
        fallback = await provider.authenticate_webhook(
            raw_body=body, headers={"x-razorpay-signature": sign(body)}
        )
        assert fallback.external_event_id == hashlib.sha256(body).hexdigest()


@pytest.mark.asyncio
async def test_invalid_authenticated_json_is_deduplicable_not_success():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: pytest.fail("unexpected network"))
    ) as client:
        body = b"invalid JSON"
        mapped = await RazorpayProvider(configured_settings(), client=client).authenticate_webhook(
            raw_body=body, headers={"x-razorpay-signature": sign(body)}
        )
    assert mapped.outcome == PaymentEventOutcome.IGNORED
    assert mapped.validation_failure_code == "INVALID_PROVIDER_EVENT"


@pytest.mark.asyncio
async def test_checkout_uses_stored_order_and_constant_time_signature():
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: pytest.fail("unexpected network"))
    )
    provider = RazorpayProvider(configured_settings(), client=client)
    signature = hmac.new(
        b"private-merchant-secret", b"order_Test|pay_Test", hashlib.sha256
    ).hexdigest()
    provider.verify_checkout_signature(
        stored_order_id="order_Test", payment_id="pay_Test", signature=signature
    )
    with pytest.raises(CheckoutConfirmationError):
        provider.verify_checkout_signature(
            stored_order_id="order_Other", payment_id="pay_Test", signature=signature
        )
    await client.aclose()


@pytest.mark.asyncio
async def test_runtime_closes_owned_client_but_not_injected_client(monkeypatch):
    real_client = httpx.AsyncClient
    owned = real_client(
        transport=httpx.MockTransport(lambda r: pytest.fail("startup must not call provider"))
    )
    monkeypatch.setattr(
        "tirodhan.modules.payments.runtime.httpx.AsyncClient", lambda **kwargs: owned
    )
    async with razorpay_runtime(configured_settings()) as provider:
        assert isinstance(provider, RazorpayProvider)
        assert not owned.is_closed
    assert owned.is_closed
    async with real_client(
        transport=httpx.MockTransport(lambda r: pytest.fail("startup must not call provider"))
    ) as injected:
        async with razorpay_runtime(configured_settings(), client=injected):
            pass
        assert not injected.is_closed
