"""Independent keys/AAD for profile, feedback, push and encrypted replay snapshots."""

import base64
import hashlib
import hmac
import json
from typing import Any

from tirodhan.core.config import Settings
from tirodhan.core.crypto.envelope import VersionedAesGcmEnvelope
from tirodhan.modules.customer_reads.errors import CustomerReadError


class CustomerDataProtection:
    def __init__(self, settings: Settings) -> None:
        try:
            if (
                settings.customer_data_active_key_id is None
                or settings.customer_data_encryption_keys is None
                or settings.customer_data_hmac_key is None
            ):
                raise ValueError()
            keys = json.loads(settings.customer_data_encryption_keys.get_secret_value())
            decoded = {k: base64.b64decode(v, validate=True) for k, v in keys.items()}
            self.envelope = VersionedAesGcmEnvelope(
                active_key_id=settings.customer_data_active_key_id,
                encryption_keys=decoded,
                aad=b"tirodhan:customer-private:v1",
                magic=b"TCP\x01",
            )
            self.key = base64.b64decode(
                settings.customer_data_hmac_key.get_secret_value(), validate=True
            )
            if len(self.key) != 32 or self.key in decoded.values():
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise CustomerReadError(503, "CUSTOMER_DATA_UNAVAILABLE") from None

    def fingerprint(self, purpose: str, value: Any) -> bytes:
        return hmac.digest(self.key, purpose.encode() + b"\0" + canonical(value), hashlib.sha256)

    def encrypt(self, purpose: str, value: Any) -> bytes:
        return self.envelope.encrypt(canonical({"purpose": purpose, "value": value}))

    def decrypt(self, purpose: str, value: bytes) -> Any:
        from tirodhan.core.crypto.envelope import EnvelopeDecryptionError

        try:
            data = json.loads(self.envelope.decrypt(value))
            if data["purpose"] != purpose:
                raise ValueError()
            return data["value"]
        except (EnvelopeDecryptionError, ValueError, KeyError, TypeError):
            raise CustomerReadError(503, "CUSTOMER_DATA_UNAVAILABLE") from None


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
