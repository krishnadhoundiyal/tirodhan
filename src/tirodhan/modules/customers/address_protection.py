from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping

from tirodhan.core.crypto.envelope import EnvelopeDecryptionError, VersionedAesGcmEnvelope
from tirodhan.modules.customers.ports import AddressProtectionNotConfiguredError


class AddressProtectionConfigurationError(AddressProtectionNotConfiguredError):
    pass


class AddressDecryptionError(RuntimeError):
    pass


def decode_key(value: str) -> bytes:
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as error:
        raise AddressProtectionConfigurationError("address key configuration is invalid") from error
    if len(decoded) != 32:
        raise AddressProtectionConfigurationError("address keys must contain 32 bytes")
    return decoded


class AesGcmAddressProtector:
    _magic = b"TAD\x01"
    _aad = b"tirodhan:customer-address:v1"

    def __init__(self, *, active_key_id: str, encryption_keys: Mapping[str, bytes]) -> None:
        try:
            self._envelope = VersionedAesGcmEnvelope(
                active_key_id=active_key_id,
                encryption_keys=encryption_keys,
                aad=self._aad,
                magic=self._magic,
            )
        except ValueError as error:
            raise AddressProtectionConfigurationError(
                "address key configuration is invalid"
            ) from error

    @classmethod
    def from_configuration(
        cls, *, active_key_id: str, encryption_keys_json: str
    ) -> AesGcmAddressProtector:
        try:
            raw = json.loads(encryption_keys_json)
            if not isinstance(raw, dict) or not all(
                isinstance(key, str) and isinstance(value, str) for key, value in raw.items()
            ):
                raise ValueError
            return cls(
                active_key_id=active_key_id,
                encryption_keys={key: decode_key(value) for key, value in raw.items()},
            )
        except (ValueError, TypeError) as error:
            raise AddressProtectionConfigurationError(
                "address key configuration is invalid"
            ) from error

    async def protect(self, plaintext: str) -> bytes:
        return self._envelope.encrypt(plaintext.encode("utf-8"))

    async def unprotect(self, protected: bytes) -> str:
        try:
            plaintext = self._envelope.decrypt(protected)
            return plaintext.decode("utf-8")
        except (EnvelopeDecryptionError, UnicodeError) as error:
            raise AddressDecryptionError("address decryption failed") from error
