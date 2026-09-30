from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class IdentityProviderNotConfiguredError(RuntimeError):
    pass


class OtpVerificationError(RuntimeError):
    pass


class OtpInvalidError(OtpVerificationError):
    pass


class OtpExpiredError(OtpVerificationError):
    pass


class OtpAlreadyVerifiedError(OtpVerificationError):
    pass


class OtpAttemptsExceededError(OtpVerificationError):
    pass


class OtpProviderRateLimitedError(RuntimeError):
    pass


class OtpProviderUnavailableError(RuntimeError):
    pass


class OtpProviderConfigurationError(IdentityProviderNotConfiguredError):
    pass


class OtpRequestConflictError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ProviderOtpChallenge:
    provider_reference: str


class OtpProvider(Protocol):
    @property
    def provider_code(self) -> str: ...

    async def start_verification(self, *, normalized_phone: str) -> ProviderOtpChallenge: ...

    async def verify(
        self,
        *,
        provider_reference: str,
        code: str,
    ) -> None: ...


class PhoneIdentityProtector(Protocol):
    async def protect(self, normalized_phone: str) -> bytes: ...

    async def unprotect(self, protected_phone: bytes) -> str: ...

    async def lookup_hmac(self, normalized_phone: str) -> bytes: ...


class UnconfiguredOtpProvider:
    @property
    def provider_code(self) -> str:
        raise OtpProviderConfigurationError("OTP provider is not configured")

    async def start_verification(self, *, normalized_phone: str) -> ProviderOtpChallenge:
        raise OtpProviderConfigurationError("OTP provider is not configured")

    async def verify(
        self,
        *,
        provider_reference: str,
        code: str,
    ) -> None:
        raise OtpProviderConfigurationError("OTP provider is not configured")


class UnconfiguredPhoneIdentityProtector:
    async def protect(self, normalized_phone: str) -> bytes:
        raise IdentityProviderNotConfiguredError("phone identity protection is not configured")

    async def lookup_hmac(self, normalized_phone: str) -> bytes:
        raise IdentityProviderNotConfiguredError("phone identity protection is not configured")

    async def unprotect(self, protected_phone: bytes) -> str:
        raise IdentityProviderNotConfiguredError("phone identity protection is not configured")
