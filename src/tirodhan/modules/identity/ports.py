from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


class IdentityProviderNotConfiguredError(RuntimeError):
    pass


class OtpVerificationError(RuntimeError):
    pass


class OtpRequestConflictError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class OtpChallenge:
    challenge_reference: str


class OtpProvider(Protocol):
    async def start_verification(
        self, *, normalized_phone: str, client_request_id: UUID
    ) -> OtpChallenge: ...

    async def verify(
        self,
        *,
        challenge_reference: str,
        normalized_phone: str,
        code: str,
    ) -> None: ...


class PhoneIdentityProtector(Protocol):
    async def protect(self, normalized_phone: str) -> bytes: ...

    async def lookup_hmac(self, normalized_phone: str) -> bytes: ...


class UnconfiguredOtpProvider:
    async def start_verification(
        self, *, normalized_phone: str, client_request_id: UUID
    ) -> OtpChallenge:
        raise IdentityProviderNotConfiguredError("OTP provider is not configured")

    async def verify(
        self,
        *,
        challenge_reference: str,
        normalized_phone: str,
        code: str,
    ) -> None:
        raise IdentityProviderNotConfiguredError("OTP provider is not configured")


class UnconfiguredPhoneIdentityProtector:
    async def protect(self, normalized_phone: str) -> bytes:
        raise IdentityProviderNotConfiguredError("phone identity protection is not configured")

    async def lookup_hmac(self, normalized_phone: str) -> bytes:
        raise IdentityProviderNotConfiguredError("phone identity protection is not configured")
