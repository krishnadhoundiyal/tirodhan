from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.db.values import utc_now
from tirodhan.modules.reliability.models import OutboxEvent


@dataclass(frozen=True)
class RoutedMessage:
    message_id: str
    message_type: str
    body: bytes


class MessagePublisher(Protocol):
    async def send(self, entity: str, message: RoutedMessage) -> None: ...


def serviceability_message(event: OutboxEvent) -> RoutedMessage:
    """Allow-list the registered envelope; never forward arbitrary outbox JSON."""
    if event.aggregate_type != "serviceability_context" or set(event.payload) != {
        "serviceability_context_id"
    }:
        raise ValueError("invalid serviceability outbox metadata")
    try:
        context_id = UUID(event.payload["serviceability_context_id"])
    except (ValueError, TypeError, AttributeError):
        raise ValueError("invalid serviceability outbox metadata") from None
    if context_id != event.aggregate_id:
        raise ValueError("inconsistent serviceability outbox identity")
    return RoutedMessage(
        str(event.outbox_event_id),
        "ServiceabilityRequested",
        json.dumps({"serviceability_context_id": str(context_id)}).encode(),
    )


async def publish_outbox_batch(
    session_factory: async_sessionmaker[AsyncSession],
    publisher: MessagePublisher,
    *,
    serviceability_entity: str,
    batch_size: int,
) -> int:
    """Finite explicit route. Duplicate sends across Jobs are intentionally tolerated."""
    if batch_size <= 0:
        raise ValueError("outbox batch size must be positive")
    # No persistent claim/lease is invented: a crash leaves PENDING recoverable.
    async with session_factory() as session, session.begin():
        events = list(
            await session.scalars(
                select(OutboxEvent)
                .where(
                    OutboxEvent.status == "PENDING",
                    OutboxEvent.available_at <= utc_now(),
                    OutboxEvent.event_type == "ServiceabilityRequested",
                )
                .order_by(OutboxEvent.available_at, OutboxEvent.outbox_event_id)
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )
        )
        for event in events:
            event.publish_attempt_count += 1
        await session.flush()

    published = 0
    for event in events:
        try:
            message = serviceability_message(event)
            await publisher.send(serviceability_entity, message)
        except Exception:
            # Never log SDK exception text/envelope. Leave the event recoverably pending.
            continue
        async with session_factory() as session, session.begin():
            await session.execute(
                update(OutboxEvent)
                .where(
                    OutboxEvent.outbox_event_id == event.outbox_event_id,
                    OutboxEvent.status == "PENDING",
                )
                .values(status="PUBLISHED", published_at=utc_now())
            )
        published += 1
    return published
