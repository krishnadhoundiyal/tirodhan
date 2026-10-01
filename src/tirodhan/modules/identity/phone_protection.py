from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from collections.abc import Mapping

from tirodhan.core.crypto.envelope import EnvelopeDecryptionError, VersionedAesGcmEnvelope
from tirodhan.modules.identity.ports import IdentityProviderNotConfiguredError


class PhoneProtectionConfigurationError(IdentityProviderNotConfiguredError):
    pass


class PhoneDecryptionError(RuntimeError):
    pass


def decode_key(value: str) -> bytes:
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as error:
        raise PhoneProtectionConfigurationError("phone key configuration is invalid") from error
    if len(decoded) != 32:
        raise PhoneProtectionConfigurationError("phone keys must contain 32 bytes")
    return decoded


class AesGcmPhoneIdentityProtector:
    _magic = b"TPH\x01"
    _aad = b"tirodhan:user-phone:v1"

    def __init__(
        self, *, active_key_id: str, encryption_keys: Mapping[str, bytes], lookup_hmac_key: bytes
    ) -> None:
        if (
            len(lookup_hmac_key) != 32
            or not encryption_keys
            or any(hmac.compare_digest(key, lookup_hmac_key) for key in encryption_keys.values())
        ):
            raise PhoneProtectionConfigurationError("phone key configuration is invalid")

        try:
            self._envelope = VersionedAesGcmEnvelope(
                active_key_id=active_key_id,
                encryption_keys=encryption_keys,
                aad=self._aad,
                magic=self._magic,
            )
        except ValueError as error:
            raise PhoneProtectionConfigurationError("phone key configuration is invalid") from error

        self._lookup_key = lookup_hmac_key

    @classmethod
    def from_configuration(
        cls, *, active_key_id: str, encryption_keys_json: str, lookup_hmac_key_base64: str
    ) -> AesGcmPhoneIdentityProtector:
        try:
            raw = json.loads(encryption_keys_json)
            if not isinstance(raw, dict) or not all(
                isinstance(key, str) and isinstance(value, str) for key, value in raw.items()
            ):
                raise ValueError
            return cls(
                active_key_id=active_key_id,
                encryption_keys={key: decode_key(value) for key, value in raw.items()},
                lookup_hmac_key=decode_key(lookup_hmac_key_base64),
            )
        except (ValueError, TypeError) as error:
            raise PhoneProtectionConfigurationError("phone key configuration is invalid") from error

    async def protect(self, normalized_phone: str) -> bytes:
        return self._envelope.encrypt(normalized_phone.encode("utf-8"))

    async def unprotect(self, protected_phone: bytes) -> str:
        try:
            plaintext = self._envelope.decrypt(protected_phone)
            return plaintext.decode("utf-8")
        except (EnvelopeDecryptionError, UnicodeError) as error:
            raise PhoneDecryptionError("phone decryption failed") from error

    async def lookup_hmac(self, normalized_phone: str) -> bytes:
        return hmac.new(self._lookup_key, normalized_phone.encode("utf-8"), hashlib.sha256).digest()
