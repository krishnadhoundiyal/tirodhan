"""Transactional recovery notification generations and finite publication claims.

Domain rows precede work rows in lock order. Sending holds no database transaction.
An acknowledged publication never expires merely because a consumer has not started.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.reliability.models import OutboxEvent
from tirodhan.modules.reliability.primitives import append_outbox_event

from .capacity_models import RiderReplacement
from .failure_models import RecoveryAssignment, RecoveryWorkRequest


def owner(identity: UUID, kind: str) -> ColumnElement[bool]:
    if kind == "ACTIVATE":
        return RecoveryWorkRequest.assignment_id == identity
    if kind == "RECOMMEND":
        return RecoveryWorkRequest.replacement_id == identity
    raise ValueError("INVALID_RECOVERY_KIND")


async def current_work(
    session: AsyncSession, identity: UUID, kind: str, *, lock: bool = False
) -> RecoveryWorkRequest | None:
    query = select(RecoveryWorkRequest).where(
        owner(identity, kind), RecoveryWorkRequest.state != "CLOSED"
    )
    if lock:
        query = query.with_for_update()
    return (await session.scalars(query)).one_or_none()


async def close_work(
    session: AsyncSession, identity: UUID, kind: str, now: datetime, reason: str
) -> None:
    work = await current_work(session, identity, kind, lock=True)
    if work:
        work.state, work.closed_at, work.close_reason = "CLOSED", now, reason
        work.publication_token = work.publication_until = None
        event = await session.get(OutboxEvent, work.outbox_event_id, with_for_update=True)
        if event and event.status == "PENDING":
            event.status = "CANCELLED"
        await session.flush()


async def enqueue_work(
    session: AsyncSession, identity: UUID, kind: str, now: datetime, *, replace: bool = False
) -> RecoveryWorkRequest:
    """Caller owns the actual domain row; repeated scans reuse its outstanding request."""
    work = await current_work(session, identity, kind, lock=True)
    if work and not replace:
        return work
    if work:
        await close_work(session, identity, kind, now, "EXPLICIT_NEW_REQUEST")
    generation = 1 + (
        await session.scalar(
            select(func.max(RecoveryWorkRequest.generation)).where(owner(identity, kind))
        )
        or 0
    )
    event = await append_outbox_event(
        session,
        event_key=f"recovery:{kind}:{identity}:v2:{generation}",
        event_type="RecoveryWorkRequested",
        aggregate_type="routing_recovery",
        aggregate_id=identity,
        payload={"work_id": str(identity), "kind": kind, "generation": generation},
        available_at=now,
    )
    work = RecoveryWorkRequest(
        outbox_event_id=event.outbox_event_id,
        assignment_id=identity if kind == "ACTIVATE" else None,
        replacement_id=identity if kind == "RECOMMEND" else None,
        generation=generation,
        state="READY",
    )
    session.add(work)
    await session.flush()
    return work


def recommendation_needed(row: RiderReplacement) -> bool:
    return row.status == "REQUESTED" or (
        row.status == "APPROVED" and row.failure_code == "RECOMMENDATION_REQUESTED"
    )


async def finish_work(
    session: AsyncSession, row: RecoveryAssignment | RiderReplacement, now: datetime
) -> None:
    """Same transaction as a durable outcome: close old request and schedule any retry."""
    if isinstance(row, RecoveryAssignment):
        identity, kind = row.assignment_id, "ACTIVATE"
        retry = row.status in ("PENDING", "AWAITING_TRANSFER")
        due = row.due_at
    else:
        identity, kind = row.replacement_id, "RECOMMEND"
        retry = recommendation_needed(row)
        due = row.next_attempt_at
    await close_work(
        session, identity, kind, now, "OPERATIONAL_RETRY" if retry else "OUTCOME_RECORDED"
    )
    if retry:
        await enqueue_work(session, identity, kind, due)


async def begin_work(
    session: AsyncSession, identity: UUID, kind: str, now: datetime, generation: int | None
) -> bool:
    work = await current_work(session, identity, kind, lock=True)
    if work is None:
        if generation is not None:
            return False
        work = await enqueue_work(session, identity, kind, now)
    if generation is not None and generation != work.generation:
        return False
    work.started_at = now
    return True


async def publication_claim(
    factory: async_sessionmaker[AsyncSession], event_id: UUID, now: datetime
) -> UUID | None:
    async with factory() as session, session.begin():
        work = await session.get(RecoveryWorkRequest, event_id, with_for_update=True)
        if (
            work is None
            or work.state in ("PUBLISHED", "CLOSED")
            or (work.publication_until is not None and work.publication_until > now)
        ):
            return None
        event = await session.get(OutboxEvent, event_id, with_for_update=True)
        if event is None or event.status != "PENDING" or event.available_at > now:
            return None
        token = new_uuid7()
        work.state, work.publication_token = "PUBLISHING", token
        work.publication_until = now + timedelta(seconds=60)
        event.publish_attempt_count += 1
        return token


async def publication_finished(
    factory: async_sessionmaker[AsyncSession], event_id: UUID, token: UUID, *, success: bool
) -> None:
    async with factory() as session, session.begin():
        work = await session.get(RecoveryWorkRequest, event_id, with_for_update=True)
        if work is None:
            return
        if work.state == "CLOSED":
            if success:
                event = await session.get(OutboxEvent, event_id, with_for_update=True)
                if event and event.status in ("PENDING", "CANCELLED"):
                    event.status, event.published_at = "PUBLISHED", utc_now()
            return
        if work.publication_token != token:
            return
        event = await session.get(OutboxEvent, event_id, with_for_update=True)
        assert event
        work.publication_token = work.publication_until = None
        if success:
            work.state = event.status = "PUBLISHED"
            event.published_at = utc_now()
        else:
            work.state = "RETRY_DUE"
            event.available_at = utc_now() + timedelta(
                seconds=min(300, 2 ** min(event.publish_attempt_count, 8))
            )


async def delivery_current(
    factory: async_sessionmaker[AsyncSession],
    event_id: UUID,
    identity: UUID,
    kind: str,
    generation: int | None,
) -> int | None:
    """Fence stale/legacy messages before graph startup; execution claims recheck."""
    async with factory() as session:
        work = await session.get(RecoveryWorkRequest, event_id)
        if work is None or work.state == "CLOSED":
            return None
        actual = work.assignment_id if kind == "ACTIVATE" else work.replacement_id
        if actual != identity or (generation is not None and generation != work.generation):
            raise ValueError("RECOVERY_MESSAGE_IDENTITY_MISMATCH")
        return work.generation
