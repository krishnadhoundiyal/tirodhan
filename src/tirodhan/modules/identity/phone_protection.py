from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
from collections.abc import Mapping

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

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
            active_key_id not in encryption_keys
            or len(lookup_hmac_key) != 32
            or not encryption_keys
            or any(len(key) != 32 for key in encryption_keys.values())
            or any(not key_id or len(key_id.encode("utf-8")) > 255 for key_id in encryption_keys)
            or any(hmac.compare_digest(key, lookup_hmac_key) for key in encryption_keys.values())
        ):
            raise PhoneProtectionConfigurationError("phone key configuration is invalid")
        self._active_key_id = active_key_id
        self._keys = {key_id: AESGCM(key) for key_id, key in encryption_keys.items()}
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
        key_id = self._active_key_id.encode("utf-8")
        header = self._magic + bytes([len(key_id)]) + key_id
        nonce = secrets.token_bytes(12)
        ciphertext = self._keys[self._active_key_id].encrypt(
            nonce, normalized_phone.encode("utf-8"), self._aad + header
        )
        return header + nonce + ciphertext

    async def unprotect(self, protected_phone: bytes) -> str:
        try:
            if not protected_phone.startswith(self._magic) or len(protected_phone) < 5:
                raise ValueError
            header_end = 5 + protected_phone[4]
            if protected_phone[4] == 0 or len(protected_phone) < header_end + 12 + 16:
                raise ValueError
            key_id = protected_phone[5:header_end].decode("utf-8")
            key = self._keys.get(key_id)
            if key is None:
                raise ValueError
            plaintext = key.decrypt(
                protected_phone[header_end : header_end + 12],
                protected_phone[header_end + 12 :],
                self._aad + protected_phone[:header_end],
            )
            return plaintext.decode("utf-8")
        except (ValueError, UnicodeError, InvalidTag) as error:
            raise PhoneDecryptionError("phone decryption failed") from error

    async def lookup_hmac(self, normalized_phone: str) -> bytes:
        return hmac.new(self._lookup_key, normalized_phone.encode("utf-8"), hashlib.sha256).digest()
