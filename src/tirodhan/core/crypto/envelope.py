from __future__ import annotations

import secrets
from collections.abc import Mapping

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class EnvelopeDecryptionError(RuntimeError):
    pass


class VersionedAesGcmEnvelope:
    def __init__(
        self,
        *,
        active_key_id: str,
        encryption_keys: Mapping[str, bytes],
        aad: bytes,
        magic: bytes,
    ) -> None:
        if (
            active_key_id not in encryption_keys
            or not encryption_keys
            or any(len(key) != 32 for key in encryption_keys.values())
            or any(not key_id or len(key_id.encode("utf-8")) > 255 for key_id in encryption_keys)
        ):
            raise ValueError("invalid encryption key configuration")

        if len(magic) == 0 or len(magic) > 255:
            raise ValueError("magic header must be between 1 and 255 bytes")

        self._active_key_id = active_key_id
        self._keys = {key_id: AESGCM(key) for key_id, key in encryption_keys.items()}
        self._aad = aad
        self._magic = magic

    def encrypt(self, plaintext: bytes) -> bytes:
        key_id = self._active_key_id.encode("utf-8")
        header = self._magic + bytes([len(key_id)]) + key_id
        nonce = secrets.token_bytes(12)
        ciphertext = self._keys[self._active_key_id].encrypt(nonce, plaintext, self._aad + header)
        return header + nonce + ciphertext

    def decrypt(self, envelope: bytes) -> bytes:
        magic_len = len(self._magic)
        try:
            if not envelope.startswith(self._magic) or len(envelope) < magic_len + 1:
                raise ValueError

            key_id_len = envelope[magic_len]
            header_end = magic_len + 1 + key_id_len

            if key_id_len == 0 or len(envelope) < header_end + 12 + 16:
                raise ValueError

            key_id = envelope[magic_len + 1 : header_end].decode("utf-8")
            key = self._keys.get(key_id)
            if key is None:
                raise ValueError

            plaintext = key.decrypt(
                envelope[header_end : header_end + 12],
                envelope[header_end + 12 :],
                self._aad + envelope[:header_end],
            )
            return plaintext
        except (ValueError, UnicodeError, InvalidTag) as error:
            raise EnvelopeDecryptionError("envelope decryption failed") from error
