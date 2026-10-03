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
    CheckoutConfirmationError,
    PaymentEventOutcome,
    PaymentInitiationOutcome,
    PaymentInitiationResult,
    PaymentProviderAuthenticationError,
    PaymentProviderNotConfiguredError,
    PaymentProviderUncertainError,
    RefundInitiationOutcome,
    RefundInitiationResult,
)


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
        raise PaymentProviderNotConfiguredError("Razorpay configuration is incomplete")
    assert settings.razorpay_key_secret is not None
    assert settings.razorpay_webhook_secret is not None
    if (
        not settings.razorpay_key_id
        or settings.razorpay_key_id != settings.razorpay_key_id.strip()
        or not settings.razorpay_key_secret.get_secret_value()
        or not settings.razorpay_webhook_secret.get_secret_value()
    ):
        raise PaymentProviderNotConfiguredError("Razorpay configuration is invalid")
    try:
        url = httpx.URL(settings.razorpay_api_base_url)
    except httpx.InvalidURL:
        raise PaymentProviderNotConfiguredError("Razorpay API URL is invalid") from None
    if (
        url.scheme != "https"
        or not url.host
        or url.userinfo
        or url.query
        or url.fragment
        or not url.path.rstrip("/").endswith("/v1")
    ):
        raise PaymentProviderNotConfiguredError("Razorpay API URL is invalid")
    return True


def _reference(value: object, prefix: str) -> str | None:
    if isinstance(value, str) and re.fullmatch(rf"{prefix}_[A-Za-z0-9]{{1,180}}", value):
        return value
    return None


def _integer(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _note(entity: dict[str, Any], key: str) -> UUID | None:
    notes = entity.get("notes")
    value = notes.get(key) if isinstance(notes, dict) else None
    if value is None:
        return None
    try:
        return UUID(value)
    except (ValueError, TypeError, AttributeError):
        raise ValueError("invalid internal provider reference") from None


def _entity(body: dict[str, Any], kind: str) -> dict[str, Any]:
    payload = body.get("payload")
    wrapper = payload.get(kind) if isinstance(payload, dict) else None
    entity = wrapper.get("entity") if isinstance(wrapper, dict) else None
    if not isinstance(entity, dict) or entity.get("entity") != kind:
        raise ValueError("invalid provider entity")
    return entity


class RazorpayProvider:
    provider_code = "RAZORPAY"
    reconcile_uncertain_initiation = True

    def __init__(self, settings: Settings, *, client: httpx.AsyncClient) -> None:
        if not razorpay_configured(settings):
            raise PaymentProviderNotConfiguredError("Razorpay is not configured")
        assert settings.razorpay_key_secret is not None
        assert settings.razorpay_webhook_secret is not None
        assert settings.razorpay_key_id is not None
        assert settings.razorpay_http_timeout_seconds is not None
        self._client = client
        self._base_url = settings.razorpay_api_base_url.rstrip("/")
        self._auth = httpx.BasicAuth(
            settings.razorpay_key_id, settings.razorpay_key_secret.get_secret_value()
        )
        self._key_secret = settings.razorpay_key_secret.get_secret_value().encode()
        self._webhook_secret = settings.razorpay_webhook_secret.get_secret_value().encode()
        self._timeout = settings.razorpay_http_timeout_seconds

    async def _post(
        self, path: str, payload: dict[str, object], *, headers: dict[str, str] | None = None
    ) -> dict[str, Any] | None:
        try:
            response = await self._client.post(
                self._base_url + path,
                auth=self._auth,
                json=payload,
                headers=headers,
                timeout=self._timeout,
                follow_redirects=False,
            )
        except httpx.TransportError:
            raise PaymentProviderUncertainError("RAZORPAY_TRANSPORT_UNCERTAIN") from None
        try:
            data = response.json()
        except ValueError:
            data = None
        if response.status_code in {401, 403}:
            raise PaymentProviderNotConfiguredError("Razorpay merchant configuration unavailable")
        if isinstance(data, dict) and response.status_code == 400:
            error = data.get("error")
            if isinstance(error, dict):
                # Error descriptions are examined transiently, never returned/logged/persisted.
                if error.get("description") in {
                    "Authentication failed",
                    "Authentication failed.",
                    "The API key/secret provided is invalid",
                    "The API key/secret provided is invalid.",
                }:
                    raise PaymentProviderNotConfiguredError(
                        "Razorpay merchant configuration unavailable"
                    )
                if (
                    error.get("code") == "BAD_REQUEST_ERROR"
                    and error.get("reason") == "input_validation_failed"
                    and error.get("field") == "amount"
                ):
                    return None  # A definitive validation rejection, not an ambiguous duplicate.
        if not 200 <= response.status_code < 300 or not isinstance(data, dict):
            raise PaymentProviderUncertainError("RAZORPAY_RESPONSE_UNCERTAIN")
        return data

    async def initiate_payment(
        self,
        *,
        payment_attempt_id: UUID,
        amount_minor: int,
        currency: str,
        provider_idempotency_key: str,
    ) -> PaymentInitiationResult:
        receipt = f"ta_{payment_attempt_id.hex}"
        result = await self._post(
            "/orders",
            {
                "amount": amount_minor,
                "currency": currency,
                "receipt": receipt,
                "partial_payment": False,
                "notes": {"tirodhan_payment_attempt_id": str(payment_attempt_id)},
            },
        )
        if result is None:
            return PaymentInitiationResult(
                PaymentInitiationOutcome.FAILED, failure_code="RAZORPAY_AMOUNT_REJECTED"
            )
        order_id = _reference(result.get("id"), "order")
        if (
            result.get("entity") != "order"
            or order_id is None
            or _integer(result.get("amount")) != amount_minor
            or result.get("currency") != currency
            or result.get("receipt") != receipt
        ):
            raise PaymentProviderUncertainError("RAZORPAY_ORDER_RESPONSE_INVALID")
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
            raise PaymentProviderNotConfiguredError("Refund payment reference is invalid")
        # Existing internal keys contain ':'. SHA-256 yields a stable allowed header alphabet.
        key = hashlib.sha256(provider_idempotency_key.encode()).hexdigest()
        result = await self._post(
            f"/payments/{provider_payment_id}/refund",
            {
                "amount": amount_minor,
                "speed": "normal",
                "notes": {"tirodhan_refund_id": str(refund_id)},
            },
            headers={"X-Refund-Idempotency": key},
        )
        if result is None:
            return RefundInitiationResult(
                RefundInitiationOutcome.FAILED, failure_code="RAZORPAY_AMOUNT_REJECTED"
            )
        refund_reference = _reference(result.get("id"), "rfnd")
        outcomes = {
            "processed": RefundInitiationOutcome.SUCCEEDED,
            "pending": RefundInitiationOutcome.SUBMITTED,
            "failed": RefundInitiationOutcome.FAILED,
        }
        status = result.get("status")
        outcome = outcomes.get(status) if isinstance(status, str) else None
        if (
            result.get("entity") != "refund"
            or refund_reference is None
            or result.get("payment_id") != provider_payment_id
            or _integer(result.get("amount")) != amount_minor
            or result.get("currency") != currency
            or outcome is None
        ):
            raise PaymentProviderUncertainError("RAZORPAY_REFUND_RESPONSE_INVALID")
        return RefundInitiationResult(
            outcome,
            provider_refund_id=refund_reference,
            failure_code="RAZORPAY_REFUND_FAILED"
            if outcome == RefundInitiationOutcome.FAILED
            else None,
        )

    def verify_checkout_signature(
        self, *, stored_order_id: str, payment_id: str, signature: str
    ) -> None:
        if _reference(payment_id, "pay") is None:
            raise CheckoutConfirmationError("Invalid checkout confirmation")
        expected = hmac.new(
            self._key_secret, f"{stored_order_id}|{payment_id}".encode(), hashlib.sha256
        ).hexdigest()
        if not re.fullmatch(r"[0-9a-f]{64}", signature) or not hmac.compare_digest(
            expected, signature
        ):
            raise CheckoutConfirmationError("Invalid checkout confirmation")

    async def authenticate_webhook(
        self, *, raw_body: bytes, headers: Mapping[str, str]
    ) -> AuthenticatedPaymentEvent:
        normalized = {key.lower(): value for key, value in headers.items()}
        signature = normalized.get("x-razorpay-signature", "")
        expected = hmac.new(self._webhook_secret, raw_body, hashlib.sha256).hexdigest()
        if not re.fullmatch(r"[0-9a-f]{64}", signature) or not hmac.compare_digest(
            expected, signature
        ):
            raise PaymentProviderAuthenticationError("Invalid provider webhook")
        header_id = normalized.get("x-razorpay-event-id", "")
        identity = (
            header_id
            if re.fullmatch(r"[A-Za-z0-9_-]{1,200}", header_id)
            else hashlib.sha256(raw_body).hexdigest()
        )
        event_type = "INVALID"
        try:
            body = json.loads(raw_body)
            if not isinstance(body, dict) or not isinstance(body.get("event"), str):
                raise ValueError
            if not re.fullmatch(r"[a-z][a-z0-9_.]{0,99}", body["event"]):
                raise ValueError
            event_type = body["event"]
            return self._map_event(body, event_type, identity)
        except (ValueError, TypeError, AttributeError, UnicodeError):
            return AuthenticatedPaymentEvent(
                self.provider_code,
                identity,
                event_type,
                PaymentEventOutcome.IGNORED,
                None,
                validation_failure_code="INVALID_PROVIDER_EVENT",
            )

    def _map_event(
        self, body: dict[str, Any], event_type: str, identity: str
    ) -> AuthenticatedPaymentEvent:
        if event_type in {"refund.created", "refund.processed", "refund.failed"}:
            entity = _entity(body, "refund")
            status = entity.get("status")
            outcome = (
                {
                    "processed": PaymentEventOutcome.SUCCEEDED,
                    "pending": PaymentEventOutcome.SUBMITTED,
                    "failed": PaymentEventOutcome.FAILED,
                }.get(status)
                if isinstance(status, str)
                else None
            )
            expected_state = {"refund.processed": "processed", "refund.failed": "failed"}.get(
                event_type
            )
            valid = (
                outcome is not None
                and _reference(entity.get("id"), "rfnd") is not None
                and _reference(entity.get("payment_id"), "pay") is not None
                and _integer(entity.get("amount")) is not None
                and isinstance(entity.get("currency"), str)
                and (expected_state is None or entity.get("status") == expected_state)
            )
            return AuthenticatedPaymentEvent(
                self.provider_code,
                identity,
                event_type,
                outcome or PaymentEventOutcome.IGNORED,
                None,
                refund_id=_note(entity, "tirodhan_refund_id"),
                provider_refund_id=_reference(entity.get("id"), "rfnd"),
                provider_payment_id=_reference(entity.get("payment_id"), "pay"),
                amount_minor=_integer(entity.get("amount")),
                currency=entity.get("currency") if valid else None,
                validation_failure_code=None if valid else "INVALID_REFUND_FACTS",
                failure_code="RAZORPAY_REFUND_FAILED"
                if outcome == PaymentEventOutcome.FAILED
                else None,
            )
        if event_type not in {"payment.captured", "payment.failed", "order.paid"}:
            return AuthenticatedPaymentEvent(
                self.provider_code, identity, event_type, PaymentEventOutcome.IGNORED, None
            )
        entity = _entity(body, "payment")
        success = event_type != "payment.failed"
        valid = (
            _reference(entity.get("id"), "pay") is not None
            and _reference(entity.get("order_id"), "order") is not None
            and _integer(entity.get("amount")) is not None
            and isinstance(entity.get("currency"), str)
            and entity.get("status") == ("captured" if success else "failed")
            and (not success or entity.get("captured") is True)
        )
        attempt_id = _note(entity, "tirodhan_payment_attempt_id")
        if event_type == "order.paid":
            order = _entity(body, "order")
            order_attempt = _note(order, "tirodhan_payment_attempt_id")
            valid = valid and (
                order.get("status") == "paid"
                and order.get("id") == entity.get("order_id")
                and _integer(order.get("amount")) == _integer(entity.get("amount"))
                and order.get("currency") == entity.get("currency")
                and (attempt_id is None or order_attempt is None or attempt_id == order_attempt)
            )
            attempt_id = attempt_id or order_attempt
        return AuthenticatedPaymentEvent(
            self.provider_code,
            identity,
            event_type,
            PaymentEventOutcome.SUCCEEDED if success else PaymentEventOutcome.FAILED,
            attempt_id,
            provider_order_id=_reference(entity.get("order_id"), "order"),
            provider_payment_id=_reference(entity.get("id"), "pay"),
            amount_minor=_integer(entity.get("amount")),
            currency=entity.get("currency") if valid else None,
            validation_failure_code=None if valid else "INVALID_PAYMENT_FACTS",
            failure_code=None if success else "RAZORPAY_PAYMENT_FAILED",
        )
