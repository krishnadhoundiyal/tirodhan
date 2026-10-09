"""Signed, owner/view-bound keyset cursors with a fixed first-page expiry."""

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from tirodhan.modules.customer_reads.errors import CustomerReadError


@dataclass(frozen=True)
class Cursor:
    owner: UUID
    view: str
    snapshot: datetime
    expires_at: datetime
    created_at: datetime
    request_id: UUID


class CursorCodec:
    def __init__(self, key: bytes) -> None:
        if len(key) < 32:
            raise ValueError("cursor signing key requires at least 256 bits")
        self.key = key

    def encode(self, value: Cursor) -> str:
        payload = json.dumps(
            {
                "v": 1,
                "owner": str(value.owner),
                "view": value.view,
                "snapshot": value.snapshot.isoformat(),
                "expires_at": value.expires_at.isoformat(),
                "created_at": value.created_at.isoformat(),
                "request_id": str(value.request_id),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        signature = hmac.digest(self.key, payload, hashlib.sha256)
        return base64.urlsafe_b64encode(signature + payload).rstrip(b"=").decode()

    def decode(self, token: str, *, owner: UUID, view: str, now: datetime) -> Cursor:
        try:
            if len(token) > 2048:
                raise ValueError()
            raw = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
            signature, payload = raw[:32], raw[32:]
            if not hmac.compare_digest(signature, hmac.digest(self.key, payload, hashlib.sha256)):
                raise ValueError()
            data = json.loads(payload)
            if data["v"] != 1 or data["owner"] != str(owner) or data["view"] != view:
                raise ValueError()
            value = Cursor(
                owner,
                view,
                datetime.fromisoformat(data["snapshot"]),
                datetime.fromisoformat(data["expires_at"]),
                datetime.fromisoformat(data["created_at"]),
                UUID(data["request_id"]),
            )
            if any(x.tzinfo is None for x in (value.snapshot, value.expires_at, value.created_at)):
                raise ValueError()
            if value.created_at > value.snapshot or value.expires_at <= value.snapshot:
                raise ValueError()
        except (ValueError, KeyError, TypeError, UnicodeError) as error:
            raise CustomerReadError(422, "NOT_ELIGIBLE") from error
        if now >= value.expires_at:
            raise CustomerReadError(409, "CURSOR_EXPIRED")
        return value
