from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from typing import Any
from uuid import UUID

import httpx

from tirodhan.core.config import Settings
from tirodhan.modules.payments.ports import (
    AuthenticatedPaymentEvent,
    PaymentEventOutcome,
    PaymentInitiationOutcome,
    PaymentInitiationResult,
    PaymentProviderAuthenticationError,
    PaymentProviderEventInputError,
    PaymentProviderNotConfiguredError,
    PaymentProviderUncertainError,
    RefundInitiationOutcome,
    RefundInitiationResult,
)

API_URL = "https://api.razorpay.com/v1"


def razorpay_configured(settings: Settings) -> bool:
    values = (
        settings.razorpay_key_id,
        settings.razorpay_key_secret,
        settings.razorpay_webhook_secret,
        settings.razorpay_http_timeout_seconds,
    )
    if all(value is None for value in values):
        return False
    if any(value is None for value in values):
        raise PaymentProviderNotConfiguredError("Razorpay runtime configuration is incomplete")
    assert settings.razorpay_key_secret is not None
    assert settings.razorpay_webhook_secret is not None
    if (
        not settings.razorpay_key_id
        or not settings.razorpay_key_id.strip()
        or not settings.razorpay_key_secret.get_secret_value().strip()
        or not settings.razorpay_webhook_secret.get_secret_value().strip()
    ):
        raise PaymentProviderNotConfiguredError("Razorpay credentials must not be empty")
    return True


def _reference(value: object, prefix: str) -> str | None:
    return (
        value
        if isinstance(value, str) and re.fullmatch(prefix + r"_[A-Za-z0-9]{1,190}", value)
        else None
    )


def _money(entity: Mapping[str, Any]) -> bool:
    return (
        type(entity.get("amount")) is int
        and entity["amount"] > 0
        and isinstance(entity.get("currency"), str)
        and re.fullmatch(r"[A-Z]{3}", entity["currency"]) is not None
    )


class RazorpayProvider:
    """Real Orders/normal-refunds HTTP adapter; owns no injected client or DB session."""

    provider_code = "RAZORPAY"

    def __init__(self, settings: Settings, *, client: httpx.AsyncClient) -> None:
        if not razorpay_configured(settings):
            raise PaymentProviderNotConfiguredError("Razorpay runtime is not configured")
        assert settings.razorpay_key_secret is not None
        assert settings.razorpay_webhook_secret is not None
        self._client = client
        self._auth = httpx.BasicAuth(
            settings.razorpay_key_id or "", settings.razorpay_key_secret.get_secret_value()
        )
        self._webhook_secret = settings.razorpay_webhook_secret.get_secret_value().encode()
        self._timeout = settings.razorpay_http_timeout_seconds

    async def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, object] | None = None,
        params: dict[str, str | int] | None = None,
        headers: dict[str, str] | None = None,
        allow_rejection: bool = False,
    ) -> dict[str, Any] | None:
        try:
            response = await self._client.request(
                method,
                API_URL + path,
                auth=self._auth,
                json=body,
                params=params,
                headers=headers,
                timeout=self._timeout,
                follow_redirects=False,
            )
        except httpx.TransportError:
            raise PaymentProviderUncertainError("RAZORPAY_TRANSPORT_UNCERTAIN") from None
        try:
            data = response.json()
        except (ValueError, UnicodeError):
            data = None
        error = data.get("error") if isinstance(data, dict) else None
        description = error.get("description") if isinstance(error, dict) else None
        if response.status_code in {401, 403} or description in (
            "The api key provided is invalid",
            "The API key provided is invalid",
            "Authentication failed",
            "Merchant id not found in authentication",
        ):
            raise PaymentProviderNotConfiguredError(
                "Razorpay merchant authentication is unavailable"
            )
        # Only explicit validation rejection, not duplicate/in-progress or unclassified errors.
        if (
            allow_rejection
            and response.status_code == 400
            and isinstance(error, dict)
            and error.get("code") == "BAD_REQUEST_ERROR"
            and error.get("reason") == "input_validation_failed"
            and error.get("field") in ("amount", "currency")
        ):
            return None
        if not 200 <= response.status_code < 300 or not isinstance(data, dict):
            raise PaymentProviderUncertainError("RAZORPAY_RESPONSE_UNCERTAIN")
        return data

    @staticmethod
    def _order(data: dict[str, Any], receipt: str, amount: int, currency: str) -> str:
        order_id = _reference(data.get("id"), "order")
        if (
            data.get("entity") != "order"
            or order_id is None
            or data.get("receipt") != receipt
            or type(data.get("amount")) is not int
            or data["amount"] != amount
            or data.get("currency") != currency
        ):
            raise PaymentProviderUncertainError("RAZORPAY_ORDER_CONFLICT")
        return order_id

    async def _find_order(self, receipt: str, amount: int, currency: str) -> str | None:
        data = await self._request("GET", "/orders", params={"receipt": receipt, "count": 100})
        assert data is not None
        items = data.get("items")
        if (
            data.get("entity") != "collection"
            or not isinstance(items, list)
            or type(data.get("count")) is not int
            or data["count"] != len(items)
            or len(items) >= 100
            or any(not isinstance(item, dict) for item in items)
        ):
            raise PaymentProviderUncertainError("RAZORPAY_ORDER_LOOKUP_UNCERTAIN")
        matches = [item for item in items if item.get("receipt") == receipt]
        if len(matches) > 1:
            raise PaymentProviderUncertainError("RAZORPAY_ORDER_IDENTITY_CONFLICT")
        return self._order(matches[0], receipt, amount, currency) if matches else None

    async def initiate_payment(
        self,
        *,
        payment_attempt_id: UUID,
        amount_minor: int,
        currency: str,
        provider_idempotency_key: str,
    ) -> PaymentInitiationResult:
        receipt = f"pa_{payment_attempt_id.hex}"
        order_id = await self._find_order(receipt, amount_minor, currency)
        if order_id is None:
            try:
                data = await self._request(
                    "POST",
                    "/orders",
                    body={
                        "amount": amount_minor,
                        "currency": currency,
                        "receipt": receipt,
                        "partial_payment": False,
                        "notes": {"tirodhan_payment_attempt_id": str(payment_attempt_id)},
                    },
                    allow_rejection=True,
                )
                if data is None:
                    return PaymentInitiationResult(
                        PaymentInitiationOutcome.FAILED, failure_code="RAZORPAY_ORDER_REJECTED"
                    )
                order_id = self._order(data, receipt, amount_minor, currency)
            except PaymentProviderUncertainError:
                # Lost response, duplicate receipt, rate limit or ambiguous provider result:
                # one bounded read-only recovery, never another POST in this invocation.
                order_id = await self._find_order(receipt, amount_minor, currency)
                if order_id is None:
                    raise PaymentProviderUncertainError("RAZORPAY_ORDER_NOT_RECOVERED") from None
        return PaymentInitiationResult(PaymentInitiationOutcome.READY, provider_order_id=order_id)

    async def initiate_refund(
        self,
        *,
        refund_id: UUID,
        provider_payment_id: str,
        amount_minor: int,
        currency: str,
        provider_idempotency_key: str,
    ) -> RefundInitiationResult:
        if _reference(provider_payment_id, "pay") is None:
            raise PaymentProviderNotConfiguredError(
                "Razorpay canonical payment reference is invalid"
            )
        receipt = f"rf_{refund_id.hex}"
        data = await self._request(
            "POST",
            f"/payments/{provider_payment_id}/refund",
            body={
                "amount": amount_minor,
                "speed": "normal",
                "receipt": receipt,
                "notes": {"tirodhan_refund_id": str(refund_id)},
            },
            headers={"X-Refund-Idempotency": receipt},
            allow_rejection=True,
        )
        if data is None:
            return RefundInitiationResult(
                RefundInitiationOutcome.FAILED, failure_code="RAZORPAY_REFUND_REJECTED"
            )
        ref = _reference(data.get("id"), "rfnd")
        notes = data.get("notes")
        note = notes.get("tirodhan_refund_id") if isinstance(notes, dict) else None
        if (
            data.get("entity") != "refund"
            or ref is None
            or data.get("payment_id") != provider_payment_id
            or type(data.get("amount")) is not int
            or data["amount"] != amount_minor
            or data.get("currency") != currency
            or data.get("receipt") not in (None, receipt)
            or note not in (None, str(refund_id))
        ):
            raise PaymentProviderUncertainError("RAZORPAY_REFUND_CONFLICT")
        states = {
            "processed": RefundInitiationOutcome.SUCCEEDED,
            "pending": RefundInitiationOutcome.SUBMITTED,
            "failed": RefundInitiationOutcome.FAILED,
        }
        state = data.get("status")
        outcome = states.get(state) if isinstance(state, str) else None
        if outcome is None:
            raise PaymentProviderUncertainError("RAZORPAY_REFUND_STATE_UNCERTAIN")
        return RefundInitiationResult(
            outcome,
            provider_refund_id=ref,
            failure_code="RAZORPAY_REFUND_FAILED"
            if outcome == RefundInitiationOutcome.FAILED
            else None,
        )

    async def authenticate_webhook(
        self,
        *,
        raw_body: bytes,
        headers: Mapping[str, str],
    ) -> AuthenticatedPaymentEvent:
        normalized = {key.lower(): value for key, value in headers.items()}
        signature = normalized.get("x-razorpay-signature", "")
        expected = hmac.new(self._webhook_secret, raw_body, hashlib.sha256).hexdigest()
        if re.fullmatch(r"[0-9a-f]{64}", signature) is None or not hmac.compare_digest(
            expected, signature
        ):
            raise PaymentProviderAuthenticationError("Invalid Razorpay webhook signature")
        event_id = normalized.get("x-razorpay-event-id", "")
        if re.fullmatch(r"[A-Za-z0-9_-]{1,200}", event_id) is None:
            raise PaymentProviderEventInputError("Razorpay event identity is missing or invalid")
        try:
            data = json.loads(raw_body)
        except (ValueError, UnicodeError):
            raise PaymentProviderEventInputError("Razorpay event envelope is malformed") from None
        event_type = data.get("event") if isinstance(data, dict) else None
        if (
            not isinstance(event_type, str)
            or re.fullmatch(r"[a-z][a-z0-9_.]{0,99}", event_type) is None
        ):
            raise PaymentProviderEventInputError("Razorpay event type is missing or invalid")
        base: dict[str, Any] = dict(
            provider=self.provider_code,
            external_event_id=event_id,
            event_type=event_type,
            payment_attempt_id=None,
        )
        if event_type not in {
            "payment.captured",
            "payment.failed",
            "refund.processed",
            "refund.failed",
            "refund.created",
        }:
            return AuthenticatedPaymentEvent(**base, outcome=PaymentEventOutcome.IGNORED)
        kind = "refund" if event_type.startswith("refund.") else "payment"
        payload = data.get("payload")
        wrapper = payload.get(kind) if isinstance(payload, dict) else None
        entity = wrapper.get("entity") if isinstance(wrapper, dict) else None
        if not isinstance(entity, dict) or entity.get("entity") != kind or not _money(entity):
            raise PaymentProviderEventInputError("Razorpay actionable event is malformed")
        payment_id = _reference(entity.get("payment_id" if kind == "refund" else "id"), "pay")
        if payment_id is None:
            raise PaymentProviderEventInputError("Razorpay payment reference is malformed")
        facts = dict(
            amount_minor=entity["amount"],
            currency=entity["currency"],
            provider_payment_id=payment_id,
        )
        if kind == "payment":
            order_id = _reference(entity.get("order_id"), "order")
            if entity.get("order_id") is not None and order_id is None:
                raise PaymentProviderEventInputError("Razorpay order reference is malformed")
            success = event_type == "payment.captured"
            if entity.get("status") != ("captured" if success else "failed") or (
                success and entity.get("captured") is not True
            ):
                raise PaymentProviderEventInputError("Razorpay payment state is contradictory")
            # Provider notes are deliberately not mapped to internal payment identities.
            return AuthenticatedPaymentEvent(
                **base,
                **facts,
                provider_order_id=order_id,
                outcome=PaymentEventOutcome.SUCCEEDED if success else PaymentEventOutcome.FAILED,
                failure_code=None if success else "RAZORPAY_PAYMENT_FAILED",
            )
        refund_ref = _reference(entity.get("id"), "rfnd")
        if refund_ref is None:
            raise PaymentProviderEventInputError("Razorpay refund reference is malformed")
        notes = entity.get("notes")
        note = notes.get("tirodhan_refund_id") if isinstance(notes, dict) else None
        receipt = entity.get("receipt")
        refund_id = None
        conflict = None
        try:
            from_note = UUID(note) if note is not None else None
            from_receipt = (
                UUID(hex=receipt[3:])
                if isinstance(receipt, str) and re.fullmatch(r"rf_[0-9a-f]{32}", receipt)
                else None
            )
            refund_id = from_note or from_receipt
            if receipt is not None and from_receipt is None:
                conflict = "REFUND_IDENTITY_CONFLICT"
            if from_note is not None and from_receipt is not None and from_note != from_receipt:
                conflict = "REFUND_IDENTITY_CONFLICT"
            if refund_id is not None and receipt is not None and receipt != f"rf_{refund_id.hex}":
                conflict = "REFUND_IDENTITY_CONFLICT"
        except (ValueError, TypeError, AttributeError):
            conflict = "REFUND_IDENTITY_CONFLICT"
        if event_type == "refund.created":
            outcome = PaymentEventOutcome.SUBMITTED
        elif event_type == "refund.processed" and entity.get("status") == "processed":
            outcome = PaymentEventOutcome.SUCCEEDED
        elif event_type == "refund.failed" and entity.get("status") == "failed":
            outcome = PaymentEventOutcome.FAILED
        else:
            raise PaymentProviderEventInputError("Razorpay refund state is contradictory")
        return AuthenticatedPaymentEvent(
            **base,
            **facts,
            refund_id=refund_id,
            provider_refund_id=refund_ref,
            outcome=outcome,
            validation_failure_code=conflict,
            failure_code="RAZORPAY_REFUND_FAILED"
            if outcome == PaymentEventOutcome.FAILED
            else None,
        )
