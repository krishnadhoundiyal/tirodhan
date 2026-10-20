"""Restricted, bounded manager history; tokens and customer PII are never captured."""

from typing import Any
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from .models import REVISIONS


async def attribute_change(
    session: AsyncSession, actor: UUID, command_id: UUID, reason: str
) -> None:
    # SET LOCAL semantics: pooled connections cannot inherit the next command's actor.
    await session.execute(
        text(
            "SELECT set_config('tirodhan.actor', :actor, true), "
            "set_config('tirodhan.command', :command, true), "
            "set_config('tirodhan.reason', :reason, true)"
        ),
        {"actor": str(actor), "command": str(command_id), "reason": reason},
    )


def history_table(kind: str) -> Any:
    if kind not in REVISIONS:
        raise ValueError("HISTORY_ENTITY_UNAVAILABLE")
    return REVISIONS[kind]


def history_identity(kind: str, identity: UUID | str) -> UUID | str:
    return str(identity) if kind == "fleet_control" else UUID(str(identity))


async def current_version(session: AsyncSession, kind: str, identity: UUID | str) -> int:
    table = history_table(kind)
    return int(
        await session.scalar(
            select(func.coalesce(func.max(table.c.revision), 0)).where(
                table.c.entity_id == history_identity(kind, identity)
            )
        )
        or 0
    )


async def versions(
    session: AsyncSession,
    kind: str,
    identity: UUID | str,
    *,
    after: int = 0,
    limit: int = 50,
) -> dict[str, Any]:
    table = history_table(kind)
    current = await current_version(session, kind, identity)
    rows = (
        (
            await session.execute(
                select(table)
                .where(
                    table.c.entity_id == history_identity(kind, identity), table.c.revision > after
                )
                .order_by(table.c.revision)
                .limit(min(max(limit, 1), 100) + 1)
            )
        )
        .mappings()
        .all()
    )
    return {
        "entity_type": kind,
        "entity_id": identity,
        "current_revision": current,
        "versions": [
            dict(
                r,
                is_current=r["revision"] == current,
                provenance="ATTRIBUTED" if r["actor_id"] else "LEGACY_OR_SYSTEM",
            )
            for r in rows[:limit]
        ],
        "next_cursor": rows[limit - 1]["revision"] if len(rows) > limit else None,
    }


async def version(
    session: AsyncSession, kind: str, identity: UUID | str, revision: int
) -> dict[str, Any]:
    table = history_table(kind)
    row = (
        (
            await session.execute(
                select(table).where(
                    table.c.entity_id == history_identity(kind, identity),
                    table.c.revision == revision,
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise ValueError("HISTORY_VERSION_UNAVAILABLE")
    return dict(row, is_current=revision == await current_version(session, kind, identity))


async def compare(
    session: AsyncSession,
    kind: str,
    identity: UUID | str,
    before: int,
    after: int,
) -> dict[str, Any]:
    old, new = (
        await version(session, kind, identity, before),
        await version(session, kind, identity, after),
    )
    a, b = old["resulting_state"], new["resulting_state"]
    return {
        "entity_type": kind,
        "entity_id": identity,
        "before": old,
        "after": new,
        "changes": {
            key: {"before": a.get(key), "after": b.get(key)}
            for key in sorted(a.keys() | b.keys())
            if a.get(key) != b.get(key)
        },
    }
