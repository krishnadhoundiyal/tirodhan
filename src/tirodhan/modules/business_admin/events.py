"""Read existing immutable duty/facility facts without copying them into revision tables."""

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tirodhan.modules.routing_operations.capacity_models import SlotAllowanceConfiguration
from tirodhan.modules.routing_operations.fleet_models import FacilityRestriction
from tirodhan.modules.routing_operations.models import ReceivingWindow
from tirodhan.modules.routing_operations.orchestration_models import (
    OperatingBreak,
    ShiftExtension,
    ShiftExtensionConsent,
)
from tirodhan.modules.routing_operations.parallel_models import ShiftConfirmation

from .lineage import row_state

# Explicit relational predicates. No arbitrary table, column or JSON query from clients.
EVENTS: dict[str, tuple[Any, str, str, str, str]] = {
    "receiving_window": (
        ReceivingWindow,
        "receiving_point_id",
        "window_id",
        "recorded_by",
        "created_at",
    ),
    "facility_restriction": (
        FacilityRestriction,
        "receiving_point_id",
        "restriction_id",
        "recorded_by",
        "created_at",
    ),
    "shift_break": (OperatingBreak, "shift_id", "break_id", "recorded_by", "starts_at"),
    "shift_extension": (
        ShiftExtension,
        "shift_id",
        "extension_id",
        "authorized_by",
        "authorized_at",
    ),
    "shift_consent": (ShiftExtensionConsent, "shift_id", "consent_id", "rider_id", "consented_at"),
    "shift_confirmation": (ShiftConfirmation, "shift_id", "shift_id", "rider_id", "confirmed_at"),
    "slot_policy": (
        SlotAllowanceConfiguration,
        "region_id",
        "version",
        "authorized_by",
        "configured_at",
    ),
}


async def events(
    session: AsyncSession, kind: str, identity: UUID, *, after: str | None = None, limit: int = 50
) -> dict[str, Any]:
    if kind not in EVENTS:
        raise ValueError("HISTORY_EVENT_UNAVAILABLE")
    model, owner, key, actor, timestamp = EVENTS[kind]
    column = getattr(model, key)
    query = select(model).where(getattr(model, owner) == identity)
    if after is not None:
        query = query.where(column > (int(after) if key == "version" else UUID(after)))
    rows = list(await session.scalars(query.order_by(column).limit(limit + 1)))
    return {
        "entity_id": identity,
        "event_type": kind,
        "history_semantics": "IMMUTABLE_FACTS",
        "events": [
            {
                "event_id": getattr(r, key),
                "actor_id": getattr(r, actor),
                "recorded_at": r.recorded_at,
                "command_id": r.recorded_command_id,
                "effective_at": getattr(r, timestamp),
                "state": row_state(r),
            }
            for r in rows[:limit]
        ],
        "next_cursor": str(getattr(rows[limit - 1], key)) if len(rows) > limit else None,
    }


async def event(session: AsyncSession, kind: str, identity: UUID, event_id: str) -> dict[str, Any]:
    if kind not in EVENTS:
        raise ValueError("HISTORY_EVENT_UNAVAILABLE")
    model, owner, key, actor, timestamp = EVENTS[kind]
    value = int(event_id) if key == "version" else UUID(event_id)
    row = await session.scalar(
        select(model).where(getattr(model, owner) == identity, getattr(model, key) == value)
    )
    if row is None:
        raise ValueError("HISTORY_EVENT_UNAVAILABLE")
    return {
        "event_id": value,
        "actor_id": getattr(row, actor),
        "recorded_at": row.recorded_at,
        "command_id": row.recorded_command_id,
        "effective_at": getattr(row, timestamp),
        "state": row_state(row),
    }
