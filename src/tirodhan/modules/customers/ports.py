from __future__ import annotations

from typing import Protocol


class AddressProtector(Protocol):
    """Application-level protection for full household addresses.

    A production implementation must use the separately approved, rotation-capable
    encryption envelope. This port deliberately does not choose that format.
    """

    async def protect(self, plaintext: str) -> bytes: ...

    async def unprotect(self, protected: bytes) -> str: ...


class AddressProtectionNotConfiguredError(RuntimeError):
    """No approved production address-protection implementation is configured."""


class UnconfiguredAddressProtector:
    async def protect(self, plaintext: str) -> bytes:
        raise AddressProtectionNotConfiguredError(
            "address encryption is not configured; an approved envelope is required"
        )

    async def unprotect(self, protected: bytes) -> str:
        raise AddressProtectionNotConfiguredError(
            "address encryption is not configured; an approved envelope is required"
        )
