from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.collection_requests.service import REQUEST_ACCEPTED, REQUEST_PRE_PLANNING
from tirodhan.modules.planning.locking import acquire_work_unit_advisory_lock
from tirodhan.modules.planning.models import PlanningBatch, PlanningBatchAttempt
from tirodhan.modules.planning.policy import (
    latest_due_slot_start,
    planning_cutoff_reached,
    require_planning_max_attempts,
)
from tirodhan.modules.reliability.models import InboxMessage
from tirodhan.modules.reliability.primitives import append_outbox_event, claim_inbox_message

PLANNING_BATCH_READY = "READY"
PLANNING_ATTEMPT_STARTED = "STARTED"
PLANNING_WORKER_CONSUMER = "planning-worker"
PLANNING_BATCH_READY_MESSAGE = "PlanningBatchReady"


class PlanningCutoffNotReachedError(RuntimeError):
    pass


class PlanningBatchNotFoundError(LookupError):
    pass


class PlanningMessageInvalidError(ValueError):
    pass


class PlanningAttemptNotStartableError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PlanningWorkUnit:
    cell_id: str
    slot_start: datetime
    slot_end: datetime


@dataclass(frozen=True, slots=True)
class FreezePlanningResult:
    batch: PlanningBatch | None
    transitioned_request_count: int
    created: bool


@dataclass(frozen=True, slots=True)
class PlanningMessage:
    message_id: str
    planning_batch_id: UUID
    message_type: str = PLANNING_BATCH_READY_MESSAGE


@dataclass(frozen=True, slots=True)
class PreparePlanningAttemptResult:
    inbox_message: InboxMessage
    attempt: PlanningBatchAttempt
    created: bool


async def discover_due_planning_work_units(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    lead_time_minutes: int | None,
    now: datetime | None = None,
) -> tuple[PlanningWorkUnit, ...]:
    discovery_time = now or utc_now()
    latest_due = latest_due_slot_start(discovery_time, lead_time_minutes)
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(
                    CollectionRequest.cell_id,
                    CollectionRequest.slot_start,
                    CollectionRequest.slot_end,
                )
                .where(
                    CollectionRequest.status == REQUEST_ACCEPTED,
                    CollectionRequest.planning_batch_id.is_(None),
                    CollectionRequest.slot_start <= latest_due,
                )
                .distinct()
                .order_by(
                    CollectionRequest.slot_start,
                    CollectionRequest.slot_end,
                    CollectionRequest.cell_id,
                )
            )
        ).all()
    return tuple(
        PlanningWorkUnit(cell_id=row.cell_id, slot_start=row.slot_start, slot_end=row.slot_end)
        for row in rows
    )


async def freeze_planning_batch(
    session_factory: async_sessionmaker[AsyncSession],
    work_unit: PlanningWorkUnit,
    *,
    lead_time_minutes: int | None,
    max_attempts: int | None,
    now: datetime | None = None,
) -> FreezePlanningResult:
    async with session_factory() as session, session.begin():
        await acquire_work_unit_advisory_lock(
            session,
            cell_id=work_unit.cell_id,
            slot_start=work_unit.slot_start,
            slot_end=work_unit.slot_end,
        )
        existing = await session.scalar(
            select(PlanningBatch).where(
                PlanningBatch.cell_id == work_unit.cell_id,
                PlanningBatch.slot_start == work_unit.slot_start,
                PlanningBatch.slot_end == work_unit.slot_end,
            )
        )
        if existing is not None:
            return FreezePlanningResult(
                batch=existing,
                transitioned_request_count=0,
                created=False,
            )

        freeze_time = now or utc_now()
        if not planning_cutoff_reached(
            work_unit.slot_start,
            lead_time_minutes,
            now=freeze_time,
        ):
            raise PlanningCutoffNotReachedError("planning cutoff has not been reached")
        max_attempts_snapshot = require_planning_max_attempts(max_attempts)

        batch = PlanningBatch(
            planning_batch_id=new_uuid7(),
            cell_id=work_unit.cell_id,
            slot_start=work_unit.slot_start,
            slot_end=work_unit.slot_end,
            status=PLANNING_BATCH_READY,
            completion_mode=None,
            max_attempts_snapshot=max_attempts_snapshot,
            algorithm_version=None,
            created_at=freeze_time,
            completed_at=None,
        )
        session.add(batch)
        await session.flush([batch])

        transition_result = await session.execute(
            update(CollectionRequest)
            .where(
                CollectionRequest.cell_id == work_unit.cell_id,
                CollectionRequest.slot_start == work_unit.slot_start,
                CollectionRequest.slot_end == work_unit.slot_end,
                CollectionRequest.status == REQUEST_ACCEPTED,
                CollectionRequest.planning_batch_id.is_(None),
            )
            .values(
                status=REQUEST_PRE_PLANNING,
                planning_batch_id=batch.planning_batch_id,
            )
        )
        transitioned_count = cast(CursorResult[Any], transition_result).rowcount
        if transitioned_count == 0:
            await session.execute(
                delete(PlanningBatch).where(
                    PlanningBatch.planning_batch_id == batch.planning_batch_id
                )
            )
            return FreezePlanningResult(
                batch=None,
                transitioned_request_count=0,
                created=False,
            )

        await append_outbox_event(
            session,
            event_key=f"planning-batch-ready:{batch.planning_batch_id}",
            aggregate_type="planning_batch",
            aggregate_id=batch.planning_batch_id,
            event_type=PLANNING_BATCH_READY_MESSAGE,
            payload={
                "planning_batch_id": str(batch.planning_batch_id),
                "cell_id": batch.cell_id,
            },
        )
        return FreezePlanningResult(
            batch=batch,
            transitioned_request_count=transitioned_count,
            created=True,
        )


async def prepare_planning_attempt(
    session_factory: async_sessionmaker[AsyncSession],
    message: PlanningMessage,
) -> PreparePlanningAttemptResult:
    async with session_factory() as session, session.begin():
        claim = await claim_inbox_message(
            session,
            consumer_name=PLANNING_WORKER_CONSUMER,
            message_id=message.message_id,
            message_type=message.message_type,
            business_key=str(message.planning_batch_id),
        )
        if message.message_type != PLANNING_BATCH_READY_MESSAGE:
            raise PlanningMessageInvalidError("unexpected planning message type")

        batch = await session.scalar(
            select(PlanningBatch)
            .where(PlanningBatch.planning_batch_id == message.planning_batch_id)
            .with_for_update()
        )
        if batch is None:
            raise PlanningBatchNotFoundError("planning batch not found")

        attempts = tuple(
            await session.scalars(
                select(PlanningBatchAttempt)
                .where(PlanningBatchAttempt.planning_batch_id == batch.planning_batch_id)
                .order_by(PlanningBatchAttempt.attempt_number)
            )
        )
        started = next(
            (attempt for attempt in attempts if attempt.outcome == PLANNING_ATTEMPT_STARTED),
            None,
        )
        if started is not None:
            return PreparePlanningAttemptResult(
                inbox_message=claim.message,
                attempt=started,
                created=False,
            )
        if attempts:
            raise PlanningAttemptNotStartableError(
                "Phase 1E does not allocate another logical planning attempt"
            )

        attempt = PlanningBatchAttempt(
            planning_batch_attempt_id=new_uuid7(),
            planning_batch_id=batch.planning_batch_id,
            attempt_number=1,
            outcome=PLANNING_ATTEMPT_STARTED,
            failure_code=None,
            started_at=utc_now(),
            completed_at=None,
        )
        session.add(attempt)
        await session.flush([attempt])
        return PreparePlanningAttemptResult(
            inbox_message=claim.message,
            attempt=attempt,
            created=True,
        )
