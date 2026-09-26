from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from hmac import compare_digest
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.reliability.models import (
    IdempotencyRecord,
    InboxMessage,
    OutboxEvent,
)

IDEMPOTENCY_IN_PROGRESS = "IN_PROGRESS"
IDEMPOTENCY_COMPLETED = "COMPLETED"
INBOX_PROCESSING = "PROCESSING"
INBOX_PROCESSED = "PROCESSED"
OUTBOX_PENDING = "PENDING"


class IdempotencyKeyConflictError(Exception):
    """The same scoped key was reused for a different request fingerprint."""


class IdempotencyCompletionConflictError(Exception):
    """A completed command was presented with different result metadata."""


class InboxMessageConflictError(Exception):
    """A transport identity was reused with different message metadata."""


@dataclass(frozen=True, slots=True)
class IdempotencyClaim:
    record: IdempotencyRecord
    created: bool


@dataclass(frozen=True, slots=True)
class IdempotencyResult:
    resource_id: UUID | None
    status_code: int | None


@dataclass(frozen=True, slots=True)
class InboxClaim:
    message: InboxMessage
    claimed: bool


async def claim_idempotency_record(
    session: AsyncSession,
    *,
    scope: str,
    idempotency_key: str,
    request_fingerprint: bytes,
    expires_at: datetime,
    now: datetime | None = None,
) -> IdempotencyClaim:
    """Atomically create or load a scoped command record without committing."""
    created_at = now or utc_now()
    statement = (
        insert(IdempotencyRecord)
        .values(
            idempotency_record_id=new_uuid7(),
            scope=scope,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            status=IDEMPOTENCY_IN_PROGRESS,
            created_at=created_at,
            expires_at=expires_at,
        )
        .on_conflict_do_nothing(index_elements=["scope", "idempotency_key"])
        .returning(IdempotencyRecord)
    )
    record = await session.scalar(statement)
    if record is not None:
        return IdempotencyClaim(record=record, created=True)

    record = await session.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.scope == scope,
            IdempotencyRecord.idempotency_key == idempotency_key,
        )
    )
    if record is None:
        raise RuntimeError("idempotency record conflict did not resolve to a row")
    if not compare_digest(record.request_fingerprint, request_fingerprint):
        raise IdempotencyKeyConflictError(
            "idempotency key is already associated with a different request"
        )
    return IdempotencyClaim(record=record, created=False)


async def complete_idempotency_record(
    session: AsyncSession,
    record: IdempotencyRecord,
    *,
    result_resource_id: UUID | None,
    result_status_code: int | None,
    completed_at: datetime | None = None,
) -> IdempotencyRecord:
    """Conditionally complete a command record without committing the transaction."""
    completed_at = completed_at or utc_now()
    updated_id = await session.scalar(
        update(IdempotencyRecord)
        .where(
            IdempotencyRecord.idempotency_record_id == record.idempotency_record_id,
            IdempotencyRecord.status == IDEMPOTENCY_IN_PROGRESS,
        )
        .values(
            status=IDEMPOTENCY_COMPLETED,
            result_resource_id=result_resource_id,
            result_status_code=result_status_code,
            completed_at=completed_at,
        )
        .returning(IdempotencyRecord.idempotency_record_id)
    )
    await session.refresh(record)
    if updated_id is not None:
        return record

    if (
        record.status != IDEMPOTENCY_COMPLETED
        or record.result_resource_id != result_resource_id
        or record.result_status_code != result_status_code
    ):
        raise IdempotencyCompletionConflictError(
            "idempotency record already has a different terminal result"
        )
    return record


def get_completed_idempotency_result(
    record: IdempotencyRecord,
) -> IdempotencyResult | None:
    if record.status != IDEMPOTENCY_COMPLETED:
        return None
    return IdempotencyResult(
        resource_id=record.result_resource_id,
        status_code=record.result_status_code,
    )


async def claim_inbox_message(
    session: AsyncSession,
    *,
    consumer_name: str,
    message_id: str,
    message_type: str,
    business_key: str | None = None,
    received_at: datetime | None = None,
) -> InboxClaim:
    """Atomically claim a transport message without committing the transaction."""
    first_received_at = received_at or utc_now()
    statement = (
        insert(InboxMessage)
        .values(
            consumer_name=consumer_name,
            message_id=message_id,
            message_type=message_type,
            business_key=business_key,
            status=INBOX_PROCESSING,
            first_received_at=first_received_at,
        )
        .on_conflict_do_nothing(index_elements=["consumer_name", "message_id"])
        .returning(InboxMessage)
    )
    message = await session.scalar(statement)
    if message is not None:
        return InboxClaim(message=message, claimed=True)

    message = await session.scalar(
        select(InboxMessage).where(
            InboxMessage.consumer_name == consumer_name,
            InboxMessage.message_id == message_id,
        )
    )
    if message is None:
        raise RuntimeError("inbox message conflict did not resolve to a row")
    if message.message_type != message_type or message.business_key != business_key:
        raise InboxMessageConflictError(
            "consumer/message identity is already associated with different metadata"
        )
    return InboxClaim(message=message, claimed=False)


async def complete_inbox_message(
    session: AsyncSession,
    message: InboxMessage,
    *,
    processed_at: datetime | None = None,
) -> InboxMessage:
    """Conditionally mark a claimed inbox message processed without committing."""
    processed_at = processed_at or utc_now()
    await session.execute(
        update(InboxMessage)
        .where(
            InboxMessage.consumer_name == message.consumer_name,
            InboxMessage.message_id == message.message_id,
            InboxMessage.status == INBOX_PROCESSING,
        )
        .values(status=INBOX_PROCESSED, processed_at=processed_at)
    )
    await session.refresh(message)
    return message


async def append_outbox_event(
    session: AsyncSession,
    *,
    event_key: str,
    aggregate_type: str,
    aggregate_id: UUID,
    event_type: str,
    payload: Mapping[str, object],
    available_at: datetime | None = None,
) -> OutboxEvent:
    """Append a minimal, PII-free routing event in the caller-owned transaction."""
    now = utc_now()
    event = OutboxEvent(
        outbox_event_id=new_uuid7(),
        event_key=event_key,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        event_type=event_type,
        payload=dict(payload),
        status=OUTBOX_PENDING,
        created_at=now,
        available_at=available_at or now,
        publish_attempt_count=0,
    )
    session.add(event)
    await session.flush([event])
    return event
