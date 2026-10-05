from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.db.values import utc_now
from tirodhan.modules.dispatch.events import DISPATCH_REQUESTED, parse_dispatch_message
from tirodhan.modules.reliability.models import OutboxEvent


@dataclass(frozen=True)
class RoutedMessage:
    message_id: str
    message_type: str
    body: bytes
    session_id: str | None = None


class MessagePublisher(Protocol):
    async def send(self, entity: str, message: RoutedMessage) -> None: ...


def dispatch_message(event: OutboxEvent) -> RoutedMessage:
    message = parse_dispatch_message(json.dumps(event.payload).encode())
    if (
        event.aggregate_type != "collection_group"
        or event.aggregate_id != message.collection_group_id
    ):
        raise ValueError("inconsistent dispatch outbox identity")
    return RoutedMessage(
        str(event.outbox_event_id), DISPATCH_REQUESTED, json.dumps(message.payload()).encode()
    )


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
    rider_notification_entity: str | None = None,
    refund_entity: str | None = None,
    planning_entity: str | None = None,
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
                    OutboxEvent.event_type.in_(
                        ["ServiceabilityRequested"]
                        + ([DISPATCH_REQUESTED] if rider_notification_entity else [])
                        + (["RefundRequested"] if refund_entity else [])
                        + (
                            ["PlanningBatchReady", "PlanningAttemptRequested"]
                            if planning_entity
                            else []
                        )
                    ),
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
            if event.event_type == "ServiceabilityRequested":
                message = serviceability_message(event)
                await publisher.send(serviceability_entity, message)
            elif event.event_type == DISPATCH_REQUESTED and rider_notification_entity:
                message = dispatch_message(event)
                await publisher.send(rider_notification_entity, message)
            elif event.event_type == "RefundRequested" and refund_entity:
                message = refund_message(event)
                await publisher.send(refund_entity, message)
            elif (
                event.event_type in ("PlanningBatchReady", "PlanningAttemptRequested")
                and planning_entity
            ):
                message = planning_message(event)
                await publisher.send(planning_entity, message)
            else:
                continue
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


def refund_message(event: OutboxEvent) -> RoutedMessage:
    if event.aggregate_type != "refund" or set(event.payload) != {"refund_id"}:
        raise ValueError("invalid refund outbox metadata")
    try:
        refund_id = UUID(event.payload["refund_id"])
    except (ValueError, TypeError, AttributeError):
        raise ValueError("invalid refund outbox metadata") from None
    if refund_id != event.aggregate_id:
        raise ValueError("inconsistent refund outbox identity")
    return RoutedMessage(
        str(event.outbox_event_id),
        "RefundRequested",
        json.dumps({"refund_id": str(refund_id)}).encode(),
    )


def planning_message(event: OutboxEvent) -> RoutedMessage:
    if event.aggregate_type != "planning_batch":
        raise ValueError("invalid planning outbox metadata")
    if (
        "planning_batch_id" not in event.payload
        or "cell_id" not in event.payload
        or "attempt_number" not in event.payload
    ):
        raise ValueError("invalid planning outbox metadata")

    try:
        batch_id = UUID(event.payload["planning_batch_id"])
    except (ValueError, TypeError, AttributeError):
        raise ValueError("invalid planning outbox metadata") from None

    if batch_id != event.aggregate_id:
        raise ValueError("inconsistent planning outbox identity")

    return RoutedMessage(
        str(event.outbox_event_id),
        event.event_type,
        json.dumps(event.payload).encode(),
        session_id=str(event.payload["cell_id"]),
    )
