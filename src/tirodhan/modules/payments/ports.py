from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Protocol
from uuid import UUID


class PaymentInitiationOutcome(str, Enum):
    READY = "READY"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class PaymentInitiationResult:
    outcome: PaymentInitiationOutcome
    provider_order_id: str | None = None
    provider_payment_id: str | None = None
    failure_code: str | None = None


class PaymentEventOutcome(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class AuthenticatedPaymentEvent:
    provider: str
    external_event_id: str
    event_type: str
    outcome: PaymentEventOutcome
    payment_attempt_id: UUID | None
    provider_order_id: str | None = None
    provider_payment_id: str | None = None
    failure_code: str | None = None


class PaymentProvider(Protocol):
    @property
    def provider_code(self) -> str: ...

    async def initiate_payment(
        self,
        *,
        payment_attempt_id: UUID,
        amount_minor: int,
        currency: str,
        provider_idempotency_key: str,
    ) -> PaymentInitiationResult: ...

    async def authenticate_webhook(
        self, *, raw_body: bytes, headers: Mapping[str, str]
    ) -> AuthenticatedPaymentEvent: ...


class PaymentProviderNotConfiguredError(RuntimeError):
    pass


class PaymentProviderAuthenticationError(RuntimeError):
    pass


class PaymentProviderUncertainError(RuntimeError):
    def __init__(self, failure_code: str = "PROVIDER_OUTCOME_UNCERTAIN") -> None:
        super().__init__("payment provider outcome is uncertain")
        self.failure_code = failure_code


class UnconfiguredPaymentProvider:
    @property
    def provider_code(self) -> str:
        raise PaymentProviderNotConfiguredError("production payment provider is not configured")

    async def initiate_payment(
        self,
        *,
        payment_attempt_id: UUID,
        amount_minor: int,
        currency: str,
        provider_idempotency_key: str,
    ) -> PaymentInitiationResult:
        raise PaymentProviderNotConfiguredError("production payment provider is not configured")

    async def authenticate_webhook(
        self, *, raw_body: bytes, headers: Mapping[str, str]
    ) -> AuthenticatedPaymentEvent:
        raise PaymentProviderNotConfiguredError("production payment provider is not configured")
