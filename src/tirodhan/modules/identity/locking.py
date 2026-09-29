from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def phone_identity_advisory_lock_key(phone_lookup_hmac: bytes) -> int:
    if len(phone_lookup_hmac) < 8:
        raise ValueError("phone lookup HMAC must contain at least eight bytes")
    return int.from_bytes(phone_lookup_hmac[:8], byteorder="big", signed=True)


async def acquire_phone_identity_advisory_lock(
    session: AsyncSession, *, phone_lookup_hmac: bytes
) -> int:
    key = phone_identity_advisory_lock_key(phone_lookup_hmac)
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
    return key
