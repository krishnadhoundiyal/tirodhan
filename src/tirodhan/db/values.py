from datetime import datetime, timezone
from uuid import UUID

from uuid6 import uuid7


def new_uuid7() -> UUID:
    """Return an application-generated RFC 9562 UUIDv7."""
    return uuid7()


def utc_now() -> datetime:
    """Return a timezone-aware current UTC instant."""
    return datetime.now(timezone.utc)
