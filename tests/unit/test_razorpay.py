from __future__ import annotations

import hashlib
import hmac
import json

import httpx
import pytest

from tirodhan.core.config import Settings
from tirodhan.db.values import new_uuid7
from tirodhan.main import create_app
from tirodhan.modules.payments.ports import (
    PaymentEventOutcome,
    PaymentInitiationOutcome,
    PaymentProviderAuthenticationError,
    PaymentProviderEventInputError,
    PaymentProviderNotConfiguredError,
    PaymentProviderUncertainError,
    UnconfiguredPaymentProvider,
)
from tirodhan.modules.payments.razorpay import RazorpayProvider, razorpay_configured
from tirodhan.modules.payments.runtime import razorpay_runtime


def configuration(**overrides):
    return Settings(
        _env_file=None,
        **(
            {
                "razorpay_key_id": "rzp_test_merchant",
                "razorpay_key_secret": "test-merchant-secret",
                "razorpay_webhook_secret": "test-webhook-secret",
                "razorpay_http_timeout_seconds": 5,
            }
            | overrides
        ),
    )


def signed(body: bytes, event_id="evt_test"):
    return {
        "X-Razorpay-Signature": hmac.new(b"test-webhook-secret", body, hashlib.sha256).hexdigest(),
        "x-razorpay-event-id": event_id,
    }


def order(expected_receipt, **overrides):
    return {
        "entity": "order",
        "id": "order_test",
        "amount": 500,
        "currency": "INR",
        "receipt": expected_receipt,
    } | overrides


def collection(items):
    return {"entity": "collection", "count": len(items), "items": items}


@pytest.mark.parametrize(
    "missing",
    [
        "razorpay_key_id",
        "razorpay_key_secret",
        "razorpay_webhook_secret",
        "razorpay_http_timeout_seconds",
    ],
)
def test_partial_configuration_fails_at_application_construction(missing):
    with pytest.raises(PaymentProviderNotConfiguredError):
        create_app(configuration(**{missing: None}))


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), 61])
def test_timeout_positive_finite_bounded(value):
    with pytest.raises(ValueError):
        configuration(razorpay_http_timeout_seconds=value)


def test_no_configuration_remains_unconfigured_and_secrets_are_redacted():
    settings = Settings(_env_file=None)
    assert not razorpay_configured(settings)
    assert isinstance(create_app(settings).state.payment_provider, UnconfiguredPaymentProvider)
    assert "test-merchant-secret" not in repr(configuration())
    assert "test-webhook-secret" not in repr(configuration())


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [True, False])
async def test_order_lookup_or_creation_replays_stable_receipt_without_extra_post(existing):
    attempt_id = new_uuid7()
    receipt = f"pa_{attempt_id.hex}"
    remote = [order(receipt)] if existing else []
    calls = []

    def transport(request):
        calls.append(request)
        assert request.url.host == "api.razorpay.com"
        assert request.headers["authorization"].startswith("Basic ")
        assert request.extensions["timeout"]["read"] == 5
        if request.method == "GET":
            assert request.url.params["receipt"] == receipt
            return httpx.Response(200, json=collection(remote))
        body = json.loads(request.content)
        assert body == {
            "amount": 500,
            "currency": "INR",
            "receipt": receipt,
            "partial_payment": False,
            "notes": {"tirodhan_payment_attempt_id": str(attempt_id)},
        }
        remote.append(order(receipt))
        return httpx.Response(200, json=remote[0])

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        provider = RazorpayProvider(configuration(), client=client)
        for _ in range(3):
            result = await provider.initiate_payment(
                payment_attempt_id=attempt_id,
                amount_minor=500,
                currency="INR",
                provider_idempotency_key=f"payment-attempt:{attempt_id}",
            )
            assert result.outcome == PaymentInitiationOutcome.READY
            assert result.provider_order_id == "order_test" and result.provider_payment_id is None
    assert sum(call.method == "POST" for call in calls) == (0 if existing else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "patch",
    [
        {"amount": 1},
        {"amount": True},
        {"currency": "USD"},
        {"receipt": "wrong"},
        {"id": None},
        {"entity": "payment"},
    ],
)
@pytest.mark.parametrize("recovered", [False, True])
async def test_conflicting_created_and_recovered_orders_never_ready(patch, recovered):
    attempt_id = new_uuid7()
    receipt = f"pa_{attempt_id.hex}"

    def transport(request):
        if request.method == "GET":
            # A contradictory exact-receipt record is refused; wrong-receipt lookup is absent.
            return httpx.Response(
                200, json=collection([order(receipt, **patch)] if recovered else [])
            )
        return httpx.Response(200, json=order(receipt, **patch))

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(PaymentProviderUncertainError):
            await RazorpayProvider(configuration(), client=client).initiate_payment(
                payment_attempt_id=attempt_id,
                amount_minor=500,
                currency="INR",
                provider_idempotency_key="stable_operation",
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "connection", "duplicate", 429, 500, "malformed"])
@pytest.mark.parametrize("recoverable", [True, False])
async def test_ambiguous_post_recovers_by_receipt_or_remains_uncertain(failure, recoverable):
    attempt_id = new_uuid7()
    receipt = f"pa_{attempt_id.hex}"
    calls = []

    def transport(request):
        calls.append(request.method)
        if request.method == "GET":
            return httpx.Response(
                200, json=collection([order(receipt)] if len(calls) > 1 and recoverable else [])
            )
        if failure == "timeout":
            raise httpx.ReadTimeout("untrusted-secret-text", request=request)
        if failure == "connection":
            raise httpx.ConnectError("untrusted-secret-text", request=request)
        if failure == "duplicate":
            return httpx.Response(
                400,
                json={
                    "error": {
                        "code": "BAD_REQUEST_ERROR",
                        "description": (
                            "Duplicate request. This request has already been processed."
                        ),
                    }
                },
            )
        if failure == "malformed":
            return httpx.Response(200, text="not json")
        return httpx.Response(failure, json={"error": {}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        operation = RazorpayProvider(configuration(), client=client).initiate_payment(
            payment_attempt_id=attempt_id,
            amount_minor=500,
            currency="INR",
            provider_idempotency_key="stable_operation",
        )
        if recoverable:
            assert (await operation).provider_order_id == "order_test"
        else:
            with pytest.raises(PaymentProviderUncertainError) as error:
                await operation
            assert "untrusted-secret-text" not in str(error.value)
    assert calls == ["GET", "POST", "GET"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403])
async def test_merchant_authentication_not_commercial_failure(status):
    def transport(request):
        return httpx.Response(
            status, json={"error": {"description": "The api key provided is invalid"}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(PaymentProviderNotConfiguredError):
            await RazorpayProvider(configuration(), client=client).initiate_payment(
                payment_attempt_id=new_uuid7(),
                amount_minor=500,
                currency="INR",
                provider_idempotency_key="stable_operation",
            )


@pytest.mark.asyncio
async def test_definitive_order_validation_rejection_is_failed():
    def transport(request):
        if request.method == "GET":
            return httpx.Response(200, json=collection([]))
        return httpx.Response(
            400,
            json={
                "error": {
                    "code": "BAD_REQUEST_ERROR",
                    "reason": "input_validation_failed",
                    "field": "amount",
                    "description": "private provider text",
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        result = await RazorpayProvider(configuration(), client=client).initiate_payment(
            payment_attempt_id=new_uuid7(),
            amount_minor=500,
            currency="INR",
            provider_idempotency_key="stable_operation",
        )
        assert result.outcome == PaymentInitiationOutcome.FAILED
        assert result.failure_code == "RAZORPAY_ORDER_REJECTED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state,outcome", [("processed", "SUCCEEDED"), ("pending", "SUBMITTED"), ("failed", "FAILED")]
)
async def test_refund_native_key_body_and_result_are_stable(state, outcome):
    refund_id = new_uuid7()
    requests = []

    def transport(request):
        requests.append(request)
        assert request.url.path == "/v1/payments/pay_canonical/refund"
        assert request.headers["x-refund-idempotency"] == f"rf_{refund_id.hex}"
        body = json.loads(request.content)
        assert body == {
            "amount": 500,
            "speed": "normal",
            "receipt": f"rf_{refund_id.hex}",
            "notes": {"tirodhan_refund_id": str(refund_id)},
        }
        return httpx.Response(
            200,
            json={
                "entity": "refund",
                "id": "rfnd_stable",
                "payment_id": "pay_canonical",
                "currency": "INR",
                "status": state,
                **body,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        provider = RazorpayProvider(configuration(), client=client)
        for _ in range(2):
            result = await provider.initiate_refund(
                refund_id=refund_id,
                provider_payment_id="pay_canonical",
                amount_minor=500,
                currency="INR",
                provider_idempotency_key=f"refund:{refund_id}",
            )
            assert result.outcome.value == outcome and result.provider_refund_id == "rfnd_stable"
    assert requests[0].content == requests[1].content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "problem",
    [
        409,
        429,
        500,
        "timeout",
        {"payment_id": "pay_wrong"},
        {"amount": 1},
        {"currency": "USD"},
        {"receipt": "wrong"},
        {"notes": {"tirodhan_refund_id": "wrong"}},
        {"status": "mystery"},
        {"id": None},
    ],
)
async def test_refund_ambiguous_or_conflicting_response_is_uncertain(problem):
    def transport(request):
        if problem == "timeout":
            raise httpx.ReadTimeout("secret provider body", request=request)
        if isinstance(problem, int):
            return httpx.Response(problem, json={})
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "entity": "refund",
                "id": "rfnd_test",
                "payment_id": "pay_canonical",
                "currency": "INR",
                "status": "processed",
                **body,
                **problem,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(PaymentProviderUncertainError):
            await RazorpayProvider(configuration(), client=client).initiate_refund(
                refund_id=new_uuid7(),
                provider_payment_id="pay_canonical",
                amount_minor=500,
                currency="INR",
                provider_idempotency_key="stable_operation",
            )


@pytest.mark.asyncio
async def test_exact_raw_webhook_authentication_precedes_json_and_requires_event_id():
    body = b'{ "event" : "payment.authorized", "payload":{} }'
    async with httpx.AsyncClient() as client:
        provider = RazorpayProvider(configuration(), client=client)
        result = await provider.authenticate_webhook(raw_body=body, headers=signed(body))
        assert result.outcome == PaymentEventOutcome.IGNORED
        for changed, headers in [
            (json.dumps(json.loads(body)).encode(), signed(body)),
            (body, {}),
            (body, {**signed(body), "X-Razorpay-Signature": "0" * 64}),
        ]:
            with pytest.raises(PaymentProviderAuthenticationError):
                await provider.authenticate_webhook(raw_body=changed, headers=headers)
        with pytest.raises(PaymentProviderEventInputError):
            await provider.authenticate_webhook(raw_body=body, headers=signed(body, event_id=""))


@pytest.mark.asyncio
async def test_runtime_client_ownership_and_no_transport_retries(monkeypatch):
    async with httpx.AsyncClient() as injected:
        async with razorpay_runtime(configuration(), client=injected) as provider:
            assert isinstance(provider, RazorpayProvider)
        assert not injected.is_closed
    real_transport = httpx.AsyncHTTPTransport
    calls = []

    def transport(**kwargs):
        calls.append(kwargs)
        return real_transport(**kwargs)

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", transport)
    async with razorpay_runtime(configuration()) as provider:
        owned = provider._client
        assert not owned.is_closed
    assert owned.is_closed and calls == [{"retries": 0}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "problem",
    [
        {"entity": "wrong", "count": 0, "items": []},
        {"entity": "collection", "count": 1, "items": []},
        {"entity": "collection", "count": True, "items": []},
        {"entity": "collection", "count": 1, "items": ["invalid"]},
        {"entity": "collection", "count": 100, "items": [{}] * 100},
    ],
)
async def test_malformed_or_saturated_lookup_never_creates_order(problem):
    calls = []

    def transport(request):
        calls.append(request.method)
        return httpx.Response(200, json=problem)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(PaymentProviderUncertainError):
            await RazorpayProvider(configuration(), client=client).initiate_payment(
                payment_attempt_id=new_uuid7(),
                amount_minor=500,
                currency="INR",
                provider_idempotency_key="stable_operation",
            )
    assert calls == ["GET"]


@pytest.mark.asyncio
async def test_multiple_exact_receipt_matches_fail_closed():
    attempt_id = new_uuid7()
    receipt = f"pa_{attempt_id.hex}"

    def transport(request):
        assert request.method == "GET"
        return httpx.Response(
            200, json=collection([order(receipt), order(receipt, id="order_other")])
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(PaymentProviderUncertainError):
            await RazorpayProvider(configuration(), client=client).initiate_payment(
                payment_attempt_id=attempt_id,
                amount_minor=500,
                currency="INR",
                provider_idempotency_key="stable_operation",
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,outcome", [(400, "FAILED"), (401, "unavailable"), (403, "unavailable")]
)
async def test_refund_rejection_and_authentication_are_distinct(status, outcome):
    def transport(request):
        return httpx.Response(
            status,
            json={
                "error": {
                    "code": "BAD_REQUEST_ERROR",
                    "reason": "input_validation_failed",
                    "field": "amount",
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        operation = RazorpayProvider(configuration(), client=client).initiate_refund(
            refund_id=new_uuid7(),
            provider_payment_id="pay_canonical",
            amount_minor=500,
            currency="INR",
            provider_idempotency_key="stable_operation",
        )
        if outcome == "unavailable":
            with pytest.raises(PaymentProviderNotConfiguredError):
                await operation
        else:
            assert (await operation).outcome.value == outcome


@pytest.mark.asyncio
@pytest.mark.parametrize("event_id", ["", "has spaces", "x" * 201])
async def test_signed_webhook_requires_usable_event_identity(event_id):
    body = b'{"event":"unrelated.event"}'
    async with httpx.AsyncClient() as client:
        with pytest.raises(PaymentProviderEventInputError):
            await RazorpayProvider(configuration(), client=client).authenticate_webhook(
                raw_body=body, headers=signed(body, event_id)
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body", [b"not json", b"[]", b"{}", b'{"event":"payment.captured","payload":{}}']
)
async def test_malformed_authenticated_actionable_event_fails_controlled(body):
    async with httpx.AsyncClient() as client:
        with pytest.raises(PaymentProviderEventInputError):
            await RazorpayProvider(configuration(), client=client).authenticate_webhook(
                raw_body=body, headers=signed(body)
            )
