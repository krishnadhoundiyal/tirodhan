import hashlib
import hmac
import json
from datetime import timedelta

import httpx
from test_collection_payment import create_context, create_request, create_user

from tirodhan.core.config import Settings
from tirodhan.db.values import utc_now
from tirodhan.modules.payments.service import (
    InitiatePaymentAttemptCommand,
    initiate_payment_attempt,
    process_authenticated_payment_event,
)


def settings(**extra):
    return Settings(
        _env_file=None,
        razorpay_key_id="rzp_test",
        razorpay_key_secret="test-secret",
        razorpay_webhook_secret="webhook-secret",
        razorpay_http_timeout_seconds=5,
        **extra,
    )


def payment_body(attempt, *, payment_id="pay_success", event_type="payment.captured", **extra):
    return json.dumps(
        {
            "event": event_type,
            "payload": {
                "payment": {
                    "entity": {
                        "entity": "payment",
                        "id": payment_id,
                        "order_id": attempt.provider_order_id,
                        "amount": 500,
                        "currency": "INR",
                        "status": event_type.split(".")[1],
                        "captured": event_type == "payment.captured",
                        **extra,
                    }
                }
            },
        }
    ).encode()


def signed(body, event_id="evt_test"):
    return {
        "x-razorpay-signature": hmac.new(b"webhook-secret", body, hashlib.sha256).hexdigest(),
        "x-razorpay-event-id": event_id,
    }


async def process(factory, provider, body, event_id="evt_test", lead=30):
    event = await provider.authenticate_webhook(raw_body=body, headers=signed(body, event_id))
    return await process_authenticated_payment_event(
        factory,
        event,
        payload_hash=hashlib.sha256(body).digest(),
        planning_lead_time_minutes=lead,
        idempotency_expires_at=utc_now() + timedelta(days=1),
    )


class Orders:
    def __init__(self):
        self.orders = {}
        self.posts = []

    def __call__(self, request):
        if request.method == "GET":
            item = self.orders.get(request.url.params["receipt"])
            return httpx.Response(
                200,
                json={
                    "entity": "collection",
                    "count": 1 if item else 0,
                    "items": [item] if item else [],
                },
            )
        data = json.loads(request.content)
        self.posts.append(data)
        if data["receipt"] in self.orders:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "description": "Duplicate request. This request has already been processed."
                    }
                },
            )
        item = {
            "entity": "order",
            "id": "order_" + data["receipt"][3:],
            "created_at": int(utc_now().timestamp()) - 180,
            **data,
        }
        self.orders[data["receipt"]] = item
        return httpx.Response(200, json=item)


async def booking(factory):
    user = await create_user(factory)
    context = await create_context(factory, user.user_id)
    result = await create_request(factory, user.user_id, context.serviceability_context_id)
    return user, result


async def initiate(factory, user, result, provider, key="attempt"):
    return await initiate_payment_attempt(
        factory,
        InitiatePaymentAttemptCommand(user.user_id, result.request.request_id, key),
        provider,
        idempotency_expires_at=utc_now() + timedelta(days=1),
    )
