from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def _utc_identity(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("work-unit timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def work_unit_advisory_lock_key(cell_id: str, slot_start: datetime, slot_end: datetime) -> int:
    """Stable signed bigint key for the shared cell/slot serialization contract."""
    identity = "\x1f".join((cell_id, _utc_identity(slot_start), _utc_identity(slot_end))).encode(
        "utf-8"
    )
    digest = hashlib.sha256(identity).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


async def acquire_work_unit_advisory_lock(
    session: AsyncSession,
    *,
    cell_id: str,
    slot_start: datetime,
    slot_end: datetime,
) -> int:
    """Acquire the transaction-scoped lock shared by payment and planning."""
    key = work_unit_advisory_lock_key(cell_id, slot_start, slot_end)
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
    return key
