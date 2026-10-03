from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from tirodhan.modules.planning.models import PlanningBatch
from tirodhan.modules.reliability.primitives import append_outbox_event

DISPATCH_REQUESTED = "CollectionGroupDispatchRequested"
FLEET_FIRST = "FLEET_FIRST"
INDEPENDENT = "INDEPENDENT"


@dataclass(frozen=True)
class DispatchMessage:
    collection_group_id: UUID
    planning_batch_id: UUID
    cell_id: str
    slot_start: datetime
    slot_end: datetime
    dispatch_stage: str

    def payload(self) -> dict[str, str]:
        return {
            "collection_group_id": str(self.collection_group_id),
            "planning_batch_id": str(self.planning_batch_id),
            "cell_id": self.cell_id,
            "slot_start": self.slot_start.isoformat(),
            "slot_end": self.slot_end.isoformat(),
            "dispatch_stage": self.dispatch_stage,
        }


class InvalidDispatchMessageError(ValueError):
    pass


def parse_dispatch_message(body: bytes) -> DispatchMessage:
    try:
        if len(body) > 2048:
            raise ValueError
        data = json.loads(body)
        if (
            not isinstance(data, dict)
            or set(data)
            != {
                "collection_group_id",
                "planning_batch_id",
                "cell_id",
                "slot_start",
                "slot_end",
                "dispatch_stage",
            }
            or any(not isinstance(value, str) for value in data.values())
        ):
            raise ValueError
        message = DispatchMessage(
            UUID(data["collection_group_id"]),
            UUID(data["planning_batch_id"]),
            data["cell_id"],
            datetime.fromisoformat(data["slot_start"]),
            datetime.fromisoformat(data["slot_end"]),
            data["dispatch_stage"],
        )
        if (
            message.dispatch_stage not in (FLEET_FIRST, INDEPENDENT)
            or not 0 < len(message.cell_id) <= 200
            or message.slot_start.tzinfo is None
            or message.slot_end.tzinfo is None
            or message.slot_start >= message.slot_end
        ):
            raise ValueError
        return message
    except (ValueError, TypeError, AttributeError, UnicodeError):
        raise InvalidDispatchMessageError("invalid dispatch message") from None


async def append_dispatch_event(
    session: AsyncSession, *, group_id: UUID, batch: PlanningBatch, stage: str
) -> None:
    message = DispatchMessage(
        group_id, batch.planning_batch_id, batch.cell_id, batch.slot_start, batch.slot_end, stage
    )
    await append_outbox_event(
        session,
        event_key=f"collection-group-dispatch:{group_id}:{stage}",
        aggregate_type="collection_group",
        aggregate_id=group_id,
        event_type=DISPATCH_REQUESTED,
        payload=message.payload(),
    )
