from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from datetime import date
from typing import Any
from uuid import UUID

import httpx

from tirodhan.core.config import Settings
from tirodhan.db.values import new_uuid7, utc_now
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
        self.public_key_id = settings.razorpay_key_id or ""
        # Account identity is non-secret. Key rotation needs the same configured account.
        self.account_key = hashlib.sha256(
            (settings.razorpay_account_id or self.public_key_id).encode()
        ).hexdigest()
        self._account_id = settings.razorpay_account_id
        self._refund_finality = settings.razorpay_normal_refund_failure_finality_confirmed
        self.refund_replay_window_seconds = settings.razorpay_refund_replay_window_seconds
        self._page_budget = settings.financial_inventory_page_budget or 20
        if not 1 <= self._page_budget <= 100:
            raise ValueError("Provider page budget must be within 1..100")

    def _observation(
        self,
        entity: dict[str, Any],
        *,
        refund_id: UUID | None = None,
        attempt_id: UUID | None = None,
    ) -> AuthenticatedPaymentEvent:
        refund = entity.get("entity") == "refund"
        kind = "refund" if refund else "payment"
        ref = _reference(entity.get("id"), "rfnd" if refund else "pay")
        pay = _reference(entity.get("payment_id"), "pay") if refund else ref
        order = None if refund else _reference(entity.get("order_id"), "order")
        if entity.get("entity") != kind or not ref or not pay or not _money(entity):
            raise PaymentProviderUncertainError("INQUIRY_FACTS_INVALID")
        state = entity.get("status")
        if not isinstance(state, str):
            raise PaymentProviderUncertainError("INQUIRY_STATE_UNKNOWN")
        if refund:
            outcome = {
                "processed": PaymentEventOutcome.SUCCEEDED,
                "failed": PaymentEventOutcome.FAILED,
                "pending": PaymentEventOutcome.SUBMITTED,
            }.get(state)
        else:
            outcome = {
                "captured": PaymentEventOutcome.SUCCEEDED,
                "refunded": PaymentEventOutcome.SUCCEEDED,
                "failed": PaymentEventOutcome.FAILED,
                "authorized": PaymentEventOutcome.SUBMITTED,
                "created": PaymentEventOutcome.SUBMITTED,
            }.get(state)
            if outcome == PaymentEventOutcome.SUCCEEDED and entity.get("captured") is not True:
                raise PaymentProviderUncertainError("INQUIRY_CAPTURE_UNVERIFIED")
            if outcome == PaymentEventOutcome.FAILED and entity.get("captured") is not False:
                raise PaymentProviderUncertainError("INQUIRY_STATE_CONTRADICTION")
        if outcome is None:
            raise PaymentProviderUncertainError("INQUIRY_STATE_UNKNOWN")
        return AuthenticatedPaymentEvent(
            provider=self.provider_code,
            external_event_id=f"inquiry:{new_uuid7()}",
            event_type=f"{kind}.inquiry",
            outcome=outcome,
            payment_attempt_id=attempt_id,
            refund_id=refund_id,
            provider_refund_id=ref if refund else None,
            provider_payment_id=pay,
            provider_order_id=order,
            amount_minor=entity["amount"],
            currency=entity["currency"],
            evidence_source="API_INQUIRY",
            provider_account_key=self.account_key,
            definitive_non_payable=bool(
                refund
                and state == "failed"
                and self._refund_finality
                and entity.get("speed_requested") == "normal"
            ),
        )

    async def inquire_payment(
        self,
        *,
        attempt_id: UUID,
        order_id: str | None,
        amount_minor: int,
        currency: str,
        reported_payment_id: str | None = None,
    ) -> list[AuthenticatedPaymentEvent]:
        receipt = f"pa_{attempt_id.hex}"
        if order_id is None:
            found = await self._find_order(receipt, amount_minor, currency)
            if found is None:
                return []  # Missing order is uncertainty, never proof of failure.
            order_id = found
        if not _reference(order_id, "order"):
            raise PaymentProviderUncertainError("INQUIRY_ORDER_INVALID")
        order = await self._request("GET", f"/orders/{order_id}")
        assert order is not None
        if self._order(order, receipt, amount_minor, currency) != order_id:
            raise PaymentProviderUncertainError("INQUIRY_ORDER_MISMATCH")
        collection = await self._request("GET", f"/orders/{order_id}/payments")
        assert collection is not None
        items = self._collection(collection)
        if len(items) >= 100:
            # Order payments does not document count/skip. Use the documented
            # account inventory, never send invented pagination to this endpoint.
            items = await self._order_account_inventory(order, order_id)
        if reported_payment_id is not None:
            if not _reference(reported_payment_id, "pay"):
                raise PaymentProviderUncertainError("INQUIRY_PAYMENT_INVALID")
            reported = await self._request("GET", f"/payments/{reported_payment_id}")
            assert reported is not None
            if reported.get("id") != reported_payment_id:
                raise PaymentProviderUncertainError("INQUIRY_PAYMENT_MISMATCH")
            items = [item for item in items if item.get("id") != reported_payment_id] + [reported]
        observations = [self._observation(item, attempt_id=attempt_id) for item in items]
        if observations and all(o.outcome == PaymentEventOutcome.FAILED for o in observations):
            items = await self._order_account_inventory(order, order_id)
            observations = [self._observation(item, attempt_id=attempt_id) for item in items]
        if any(
            o.provider_order_id != order_id
            or o.amount_minor != amount_minor
            or o.currency != currency
            for o in observations
        ):
            raise PaymentProviderUncertainError("INQUIRY_OWNERSHIP_MISMATCH")
        # A failed instrument does not close an order with another unresolved instrument.
        if any(o.outcome == PaymentEventOutcome.SUBMITTED for o in observations):
            observations = [o for o in observations if o.outcome != PaymentEventOutcome.FAILED]
        return sorted(observations, key=lambda o: o.outcome == PaymentEventOutcome.FAILED)

    @staticmethod
    def _collection(value: dict[str, Any]) -> list[dict[str, Any]]:
        items = value.get("items")
        if (
            value.get("entity") != "collection"
            or not isinstance(items, list)
            or type(value.get("count")) is not int
            or value.get("count") != len(items)
            or len(items) > 1000
            or any(not isinstance(i, dict) for i in items)
        ):
            raise PaymentProviderUncertainError("INQUIRY_COLLECTION_INCOMPLETE")
        return items

    @staticmethod
    def minimal_entity(entity: dict[str, Any]) -> dict[str, Any]:
        """Allow-list financial facts before hashing/persistence; never retain PII."""
        keys = (
            "id",
            "entity",
            "payment_id",
            "order_id",
            "amount",
            "currency",
            "status",
            "captured",
            "receipt",
            "speed_requested",
            "created_at",
        )
        result = {k: entity.get(k) for k in keys}
        notes = entity.get("notes")
        note = notes.get("tirodhan_refund_id") if isinstance(notes, dict) else None
        result["tirodhan_refund_id"] = note
        return result

    async def _exhaust_pages(
        self,
        path: str,
        params: dict[str, str | int],
        *,
        stable_required: bool = True,
    ) -> list[dict[str, Any]]:
        previous: str | None = None
        for _ in range(3):  # Require two matching exhaustive bounded observations.
            entities: dict[str, dict[str, Any]] = {}
            for page in range(self._page_budget):
                value = await self._request(
                    "GET", path, params={**params, "count": 100, "skip": page * 100}
                )
                assert value is not None
                items = self._collection(value)
                if len(items) > 100:
                    raise PaymentProviderUncertainError("INQUIRY_PAGE_INVALID")
                for item in items:
                    identity = item.get("id")
                    if not isinstance(identity, str):
                        raise PaymentProviderUncertainError("INQUIRY_IDENTITY_INVALID")
                    if identity in entities and self.minimal_entity(
                        entities[identity]
                    ) != self.minimal_entity(item):
                        raise PaymentProviderUncertainError("INQUIRY_INVENTORY_UNSTABLE")
                    entities[identity] = item
                if len(items) < 100:
                    break
            else:
                raise PaymentProviderUncertainError("INQUIRY_PAGE_BUDGET_EXHAUSTED")
            digest = hashlib.sha256(
                json.dumps(
                    [self.minimal_entity(entities[k]) for k in sorted(entities)], sort_keys=True
                ).encode()
            ).hexdigest()
            if not stable_required or digest == previous:
                return list(entities.values())
            previous = digest
        raise PaymentProviderUncertainError("INQUIRY_INVENTORY_UNSTABLE")

    async def _order_account_inventory(
        self, order: dict[str, Any], order_id: str
    ) -> list[dict[str, Any]]:
        start = order.get("created_at")
        if type(start) is not int or start < 946684800:
            raise PaymentProviderUncertainError("INQUIRY_ORDER_COVERAGE_UNKNOWN")
        items = await self._exhaust_pages(
            "/payments", {"from": start, "to": int(utc_now().timestamp())}
        )
        return [i for i in items if i.get("order_id") == order_id]

    async def inventory_page(
        self, kind: str, start: int, end: int, offset: int
    ) -> list[dict[str, Any]]:
        if kind not in {"PAYMENTS", "REFUNDS"} or offset < 0 or end < start:
            raise ValueError("Invalid financial inventory page")
        value = await self._request(
            "GET",
            "/" + kind.lower(),
            params={
                "from": start,
                "to": end,
                "count": 100,
                "skip": offset,
            },
        )
        assert value is not None
        items = self._collection(value)
        if len(items) > 100:
            raise PaymentProviderUncertainError("INQUIRY_PAGE_INVALID")
        for item in items:
            if type(item.get("created_at")) is not int or not start <= item["created_at"] <= end:
                raise PaymentProviderUncertainError("INQUIRY_WINDOW_MISMATCH")
            self._observation(item)
        return items

    async def settlement_page(self, day: date, offset: int) -> list[dict[str, Any]]:
        value = await self._request(
            "GET",
            "/settlements/recon",
            params={
                "year": day.year,
                "month": day.month,
                "day": day.day,
                "count": 1000,
                "skip": offset,
            },
        )
        assert value is not None
        return self._collection(value)

    async def _charge_refunds(self, provider_payment_id: str) -> list[dict[str, Any]]:
        if not _reference(provider_payment_id, "pay"):
            raise PaymentProviderUncertainError("INQUIRY_PAYMENT_INVALID")
        items = await self._exhaust_pages(f"/payments/{provider_payment_id}/refunds", {})
        if any(
            i.get("entity") != "refund"
            or i.get("payment_id") != provider_payment_id
            or not _money(i)
            or not _reference(i.get("id"), "rfnd")
            for i in items
        ):
            raise PaymentProviderUncertainError("INQUIRY_REFUND_INVALID")
        return items

    async def inspect_charge_refunds(
        self, provider_payment_id: str
    ) -> dict[str, tuple[int, str, str]]:
        items = await self._charge_refunds(provider_payment_id)
        # Historical approval fails closed for any external payout/reservation, even failed
        # operations: those must be mapped and reconciled before recovery can be authorized.
        if len({i["id"] for i in items}) != len(items) or any(
            i.get("status") not in {"pending", "processed", "failed"} for i in items
        ):
            raise PaymentProviderUncertainError("INQUIRY_REFUND_AMBIGUOUS")
        return {i["id"]: (i["amount"], i["currency"], i["status"]) for i in items}

    async def inquire_refund(
        self,
        *,
        refund_id: UUID,
        provider_refund_id: str | None,
        provider_payment_id: str,
        amount_minor: int,
        currency: str,
    ) -> list[AuthenticatedPaymentEvent]:
        if provider_refund_id:
            if not _reference(provider_refund_id, "rfnd"):
                raise PaymentProviderUncertainError("INQUIRY_REFUND_INVALID")
            entity = await self._request("GET", f"/refunds/{provider_refund_id}")
            assert entity is not None
            if entity.get("id") != provider_refund_id:
                raise PaymentProviderUncertainError("INQUIRY_REFUND_MISMATCH")
        else:
            items = await self._charge_refunds(provider_payment_id)
            candidates = [
                i
                for i in items
                if i.get("receipt") == f"rf_{refund_id.hex}"
                and isinstance(i.get("notes"), dict)
                and i["notes"].get("tirodhan_refund_id") == str(refund_id)
            ]
            if not candidates:
                if any(
                    i.get("receipt") == f"rf_{refund_id.hex}"
                    or (
                        isinstance(i.get("notes"), dict)
                        and i["notes"].get("tirodhan_refund_id") == str(refund_id)
                    )
                    for i in items
                ):
                    raise PaymentProviderUncertainError("INQUIRY_REFUND_CORRELATION_CONFLICT")
                return []
            if len(candidates) != 1:
                raise PaymentProviderUncertainError("INQUIRY_REFUND_AMBIGUOUS")
            entity = candidates[0]
        observation = self._observation(entity, refund_id=refund_id)
        if (
            observation.provider_payment_id != provider_payment_id
            or observation.amount_minor != amount_minor
            or observation.currency != currency
        ):
            raise PaymentProviderUncertainError("INQUIRY_REFUND_MISMATCH")
        if entity.get("receipt") not in {None, f"rf_{refund_id.hex}"}:
            raise PaymentProviderUncertainError("INQUIRY_REFUND_MISMATCH")
        notes = entity.get("notes")
        if isinstance(notes, dict) and notes.get("tirodhan_refund_id") not in {
            None,
            str(refund_id),
        }:
            raise PaymentProviderUncertainError("INQUIRY_REFUND_MISMATCH")
        return [observation]

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
        items = await self._exhaust_pages("/orders", {"receipt": receipt}, stable_required=False)
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
        # Compatibility with retained pre-0021 rows: this maps to exactly the wire
        # identity the old adapter used. Any other supplied key must be native-valid.
        key = (
            receipt
            if provider_idempotency_key == f"refund:{refund_id}"
            else provider_idempotency_key
        )
        if re.fullmatch(r"[A-Za-z0-9_-]{10,200}", key) is None:
            raise PaymentProviderNotConfiguredError("Refund provider key is invalid")
        data = await self._request(
            "POST",
            f"/payments/{provider_payment_id}/refund",
            body={
                "amount": amount_minor,
                "speed": "normal",
                "receipt": receipt,
                "notes": {"tirodhan_refund_id": str(refund_id)},
            },
            headers={"X-Refund-Idempotency": key},
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

    def dispute_observation(self, entity: dict[str, Any], **base: Any) -> AuthenticatedPaymentEvent:
        if (
            entity.get("entity") != "dispute"
            or not _reference(entity.get("id"), "disp")
            or not _reference(entity.get("payment_id"), "pay")
            or not _money(entity)
            or entity.get("status") not in {"open", "under_review", "won", "lost", "closed"}
            or type(entity.get("amount_deducted")) is not int
            or not 0 <= entity["amount_deducted"] <= entity["amount"]
        ):
            raise PaymentProviderEventInputError("Dispute facts are invalid")
        return AuthenticatedPaymentEvent(
            **base,
            outcome=PaymentEventOutcome.DISPUTED,
            provider_payment_id=entity["payment_id"],
            provider_dispute_id=entity["id"],
            amount_minor=entity["amount"],
            currency=entity["currency"],
            dispute_status=entity["status"],
            amount_deducted_minor=entity["amount_deducted"],
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
            self._account_id is not None
            and isinstance(data, dict)
            and data.get("account_id") != self._account_id
        ):
            raise PaymentProviderAuthenticationError("Razorpay webhook account mismatch")
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
            provider_account_key=self.account_key,
        )
        if event_type in {
            "payment.dispute.created",
            "payment.dispute.won",
            "payment.dispute.lost",
            "payment.dispute.closed",
            "payment.dispute.under_review",
            "payment.dispute.action_required",
        }:
            payload = data.get("payload")
            wrapper = payload.get("dispute") if isinstance(payload, dict) else None
            entity = wrapper.get("entity") if isinstance(wrapper, dict) else None
            if not isinstance(entity, dict):
                raise PaymentProviderEventInputError("Dispute evidence is malformed")
            return self.dispute_observation(entity, **base)
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
