"""Minimal trusted ingress envelope; queue RBAC is part of this trust boundary."""

import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Annotated
from uuid import UUID

from azure.identity.aio import DefaultAzureCredential
from azure.servicebus.aio import ServiceBusClient
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.core.config import Settings
from tirodhan.db.values import utc_now
from tirodhan.modules.payments.ports import (
    AuthenticatedPaymentEvent,
    PaymentEventOutcome,
    PaymentProviderNotConfiguredError,
)
from tirodhan.modules.payments.service import process_authenticated_payment_event
from tirodhan.modules.reliability.publisher import MessagePublisher, RoutedMessage
from tirodhan.modules.reliability.service_bus import AzureServiceBusPublisher
from tirodhan.modules.serviceability.consumer import ServiceabilityDelivery

MESSAGE_TYPE = "FinancialWebhookAuthenticated"
MAX_ENVELOPE_BYTES = 4096
MAX_WEBHOOK_BYTES = 1024 * 1024
Reference = Annotated[str, Field(max_length=200, pattern=r"^[A-Za-z0-9_-]+$")]
Code = Annotated[str, Field(max_length=64, pattern=r"^[A-Z0-9_]+$")]


class WebhookFacts(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Annotated[str, Field(pattern=r"^RAZORPAY$")]
    external_event_id: Reference
    event_type: Annotated[str, Field(max_length=100, pattern=r"^[a-z][a-z0-9_.]*$")]
    outcome: PaymentEventOutcome
    payment_attempt_id: UUID | None = None
    refund_id: UUID | None = None
    provider_refund_id: Reference | None = None
    provider_order_id: Reference | None = None
    provider_payment_id: Reference | None = None
    failure_code: Code | None = None
    amount_minor: Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)] | None = None
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")] | None = None
    validation_failure_code: Code | None = None
    evidence_source: Annotated[str, Field(pattern=r"^WEBHOOK$")] = "WEBHOOK"
    provider_account_key: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    definitive_non_payable: Annotated[bool, Field(strict=True)] = False
    provider_dispute_id: Reference | None = None
    dispute_status: (
        Annotated[str, Field(pattern=r"^(open|under_review|won|lost|closed)$")] | None
    ) = None
    amount_deducted_minor: Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)] | None = None


class WebhookEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Annotated[int, Field(strict=True, ge=1, le=1)] = 1
    provenance: Annotated[str, Field(pattern=r"^razorpay-hmac-v1$")] = "razorpay-hmac-v1"
    raw_payload_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    facts: WebhookFacts

    def message(self) -> RoutedMessage:
        identity = (
            f"{self.facts.provider}:{self.facts.provider_account_key}:"
            f"{self.facts.external_event_id}:{self.raw_payload_sha256}"
        )
        return RoutedMessage(
            hashlib.sha256(identity.encode()).hexdigest(),
            MESSAGE_TYPE,
            self.model_dump_json().encode(),
        )


def webhook_message(event: AuthenticatedPaymentEvent, raw_body: bytes) -> RoutedMessage:
    return WebhookEnvelope(
        raw_payload_sha256=hashlib.sha256(raw_body).hexdigest(),
        facts=WebhookFacts.model_validate(asdict(event)),
    ).message()


class UnconfiguredWebhookPublisher:
    async def send(self, entity: str, message: RoutedMessage) -> None:
        raise PaymentProviderNotConfiguredError("Financial webhook queue is not configured")


@asynccontextmanager
async def webhook_publisher_runtime(
    settings: Settings, injected: MessagePublisher | None = None
) -> AsyncIterator[MessagePublisher]:
    if injected is not None:
        yield injected
        return
    configured = (
        settings.financial_webhook_queue_name,
        settings.financial_webhook_sender_identity_client_id,
        settings.financial_webhook_send_timeout_seconds,
        settings.service_bus_namespace,
    )
    if all(v is None for v in configured[:3]):
        yield UnconfiguredWebhookPublisher()
        return
    if settings.environment in {"nonprod", "prod"} and not settings.razorpay_account_id:
        raise ValueError(
            "Queued webhook ingress requires stable Razorpay merchant account identity"
        )
    if any(v is None for v in configured) or not (settings.service_bus_namespace or "").endswith(
        ".servicebus.windows.net"
    ):
        raise ValueError("Financial webhook ingress configuration is incomplete")
    assert settings.service_bus_namespace is not None
    assert settings.financial_webhook_send_timeout_seconds is not None
    async with (
        DefaultAzureCredential(
            managed_identity_client_id=settings.financial_webhook_sender_identity_client_id
        ) as credential,
        ServiceBusClient(
            fully_qualified_namespace=settings.service_bus_namespace,
            credential=credential,
            retry_total=0,
            logging_enable=False,
            socket_timeout=settings.financial_webhook_send_timeout_seconds,
        ) as bus,
    ):
        yield AzureServiceBusPublisher(
            bus, timeout_seconds=settings.financial_webhook_send_timeout_seconds
        )


class InvalidFinancialWebhook(ValueError):
    pass


async def process_webhook_message(
    factory: async_sessionmaker[AsyncSession],
    *,
    message_id: str,
    message_type: str,
    body: bytes,
    account_key: str,
    planning_lead_time_minutes: int,
    command_ttl_seconds: int,
) -> None:
    from datetime import timedelta

    try:
        if message_type != MESSAGE_TYPE or len(body) > MAX_ENVELOPE_BYTES:
            raise ValueError
        envelope = WebhookEnvelope.model_validate_json(body)
        if (
            envelope.message().message_id != message_id
            or envelope.facts.provider_account_key != account_key
            or envelope.facts.definitive_non_payable
        ):
            raise ValueError
        event = AuthenticatedPaymentEvent(**envelope.facts.model_dump())
    except (ValidationError, ValueError, TypeError):
        raise InvalidFinancialWebhook("Invalid authenticated financial envelope") from None
    await process_authenticated_payment_event(
        factory,
        event,
        payload_hash=bytes.fromhex(envelope.raw_payload_sha256),
        planning_lead_time_minutes=planning_lead_time_minutes,
        idempotency_expires_at=utc_now() + timedelta(seconds=command_ttl_seconds),
        inbox_message_id=message_id,
    )


async def handle_webhook_delivery(
    delivery: ServiceabilityDelivery,
    factory: async_sessionmaker[AsyncSession],
    *,
    account_key: str,
    planning_lead_time_minutes: int,
    command_ttl_seconds: int,
) -> None:
    try:
        await process_webhook_message(
            factory,
            message_id=delivery.message_id,
            message_type=delivery.message_type,
            body=delivery.body,
            account_key=account_key,
            planning_lead_time_minutes=planning_lead_time_minutes,
            command_ttl_seconds=command_ttl_seconds,
        )
    except InvalidFinancialWebhook:
        await delivery.dead_letter()
        return
    except Exception:
        await delivery.abandon()
        raise
    await delivery.complete()
