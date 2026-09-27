from __future__ import annotations

import re
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
from tirodhan.modules.planning.models import (
    CollectionGroup,
    CollectionGroupMember,
    PickupExecution,
    PlanningBatch,
    PlanningBatchAttempt,
)
from tirodhan.modules.planning.policy import (
    latest_due_slot_start,
    planning_cutoff_reached,
    require_planning_max_attempts,
)
from tirodhan.modules.reliability.models import InboxMessage
from tirodhan.modules.reliability.primitives import (
    INBOX_PROCESSED,
    append_outbox_event,
    claim_inbox_message,
    complete_inbox_message,
)

PLANNING_BATCH_READY = "READY"
PLANNING_BATCH_COMPLETED = "COMPLETED"
PLANNING_ATTEMPT_STARTED = "STARTED"
PLANNING_ATTEMPT_SUCCEEDED = "SUCCEEDED"
PLANNING_ATTEMPT_FAILED = "FAILED"
PLANNING_MODE_COMPACTED = "COMPACTED"
PLANNING_MODE_NORMAL_SINGLETON = "NORMAL_SINGLETON"
PLANNING_MODE_FALLBACK_SINGLETON = "FALLBACK_SINGLETON"
PLANNING_COMPLETION_ALGORITHM = "ALGORITHM_RESULT"
PLANNING_COMPLETION_FALLBACK = "FALLBACK_RESULT"
PICKUP_PENDING_ASSIGNMENT = "PENDING_ASSIGNMENT"
REQUEST_PLANNED = "PLANNED"
PLANNING_WORKER_CONSUMER = "planning-worker"
PLANNING_BATCH_READY_MESSAGE = "PlanningBatchReady"
PLANNING_ATTEMPT_REQUESTED_MESSAGE = "PlanningAttemptRequested"
PLANNING_BATCH_COMPLETED_EVENT = "PlanningBatchCompleted"

_FAILURE_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


class PlanningCutoffNotReachedError(RuntimeError):
    pass


class PlanningBatchNotFoundError(LookupError):
    pass


class PlanningMessageInvalidError(ValueError):
    pass


class PlanningAttemptNotStartableError(RuntimeError):
    pass


class PlanningResultInvalidError(ValueError):
    pass


class PlanningInboxNotFoundError(LookupError):
    pass


class PlanningFailureCodeInvalidError(ValueError):
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
    attempt_number: int = 1
    message_type: str = PLANNING_BATCH_READY_MESSAGE


@dataclass(frozen=True, slots=True)
class PreparePlanningAttemptResult:
    inbox_message: InboxMessage
    attempt: PlanningBatchAttempt | None
    created: bool
    terminal_noop: bool = False


@dataclass(frozen=True, slots=True)
class PlanningResultGroup:
    planning_mode: str
    request_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class PlanningResult:
    planning_batch_id: UUID
    planning_batch_attempt_id: UUID
    groups: tuple[PlanningResultGroup, ...]


@dataclass(frozen=True, slots=True)
class PlanningCompletionResult:
    planning_batch_id: UUID
    completed: bool
    already_completed: bool


@dataclass(frozen=True, slots=True)
class PlanningFailureResult:
    planning_batch_id: UUID
    failed_attempt_number: int | None
    next_attempt_number: int | None
    fallback_completed: bool
    already_completed: bool


def planning_inbox_business_key(batch_id: UUID, attempt_number: int) -> str:
    return f"{batch_id}:{attempt_number}"


def _validate_message(message: PlanningMessage) -> None:
    if message.message_type == PLANNING_BATCH_READY_MESSAGE and message.attempt_number == 1:
        return
    if message.message_type == PLANNING_ATTEMPT_REQUESTED_MESSAGE and message.attempt_number > 1:
        return
    raise PlanningMessageInvalidError("planning message type does not match attempt number")


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
            .values(status=REQUEST_PRE_PLANNING, planning_batch_id=batch.planning_batch_id)
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
                "attempt_number": 1,
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
            business_key=planning_inbox_business_key(
                message.planning_batch_id, message.attempt_number
            ),
        )
        if claim.message.status == INBOX_PROCESSED:
            return PreparePlanningAttemptResult(claim.message, None, False, True)

        batch = await _lock_batch(session, message.planning_batch_id)
        if batch.status == PLANNING_BATCH_COMPLETED:
            await complete_inbox_message(session, claim.message)
            return PreparePlanningAttemptResult(claim.message, None, False, True)
        if batch.status != PLANNING_BATCH_READY:
            raise PlanningAttemptNotStartableError("planning batch is not READY")
        _validate_message(message)
        if message.attempt_number > batch.max_attempts_snapshot:
            raise PlanningAttemptNotStartableError("requested attempt exceeds batch maximum")

        attempt = await session.scalar(
            select(PlanningBatchAttempt).where(
                PlanningBatchAttempt.planning_batch_id == batch.planning_batch_id,
                PlanningBatchAttempt.attempt_number == message.attempt_number,
            )
        )
        if attempt is not None:
            if attempt.outcome == PLANNING_ATTEMPT_STARTED:
                return PreparePlanningAttemptResult(claim.message, attempt, False)
            await complete_inbox_message(session, claim.message)
            return PreparePlanningAttemptResult(claim.message, None, False, True)

        if message.attempt_number > 1:
            previous = await session.scalar(
                select(PlanningBatchAttempt).where(
                    PlanningBatchAttempt.planning_batch_id == batch.planning_batch_id,
                    PlanningBatchAttempt.attempt_number == message.attempt_number - 1,
                )
            )
            if previous is None or previous.outcome != PLANNING_ATTEMPT_FAILED:
                raise PlanningAttemptNotStartableError("previous planning attempt is not FAILED")

        attempt = PlanningBatchAttempt(
            planning_batch_attempt_id=new_uuid7(),
            planning_batch_id=batch.planning_batch_id,
            attempt_number=message.attempt_number,
            outcome=PLANNING_ATTEMPT_STARTED,
            failure_code=None,
            started_at=utc_now(),
            completed_at=None,
        )
        session.add(attempt)
        await session.flush([attempt])
        return PreparePlanningAttemptResult(claim.message, attempt, True)


async def commit_planning_result(
    session_factory: async_sessionmaker[AsyncSession],
    result: PlanningResult,
    *,
    message_id: str,
) -> PlanningCompletionResult:
    async with session_factory() as session, session.begin():
        batch = await _lock_batch(session, result.planning_batch_id)
        if batch.status == PLANNING_BATCH_COMPLETED:
            await _complete_terminal_replay_inbox(session, batch.planning_batch_id, message_id)
            return PlanningCompletionResult(batch.planning_batch_id, False, True)
        if batch.status != PLANNING_BATCH_READY:
            raise PlanningResultInvalidError("planning batch is not READY")

        attempt = await _lock_attempt(session, result.planning_batch_attempt_id)
        if attempt.planning_batch_id != batch.planning_batch_id:
            raise PlanningResultInvalidError("planning attempt belongs to another batch")
        if attempt.outcome != PLANNING_ATTEMPT_STARTED:
            raise PlanningResultInvalidError("planning attempt is not STARTED")
        inbox = await _require_planning_inbox(
            session, batch.planning_batch_id, attempt.attempt_number, message_id
        )
        population = await _lock_batch_population(session, batch.planning_batch_id)
        _validate_normal_result(result.groups, population)
        now = utc_now()
        await _persist_groups(session, batch.planning_batch_id, result.groups, now=now)
        await _transition_population(session, batch.planning_batch_id, len(population))

        attempt.outcome = PLANNING_ATTEMPT_SUCCEEDED
        attempt.failure_code = None
        attempt.completed_at = now
        batch.status = PLANNING_BATCH_COMPLETED
        batch.completion_mode = PLANNING_COMPLETION_ALGORITHM
        batch.completed_at = now
        await _append_completion_event(session, batch.planning_batch_id)
        await complete_inbox_message(session, inbox, processed_at=now)
        return PlanningCompletionResult(batch.planning_batch_id, True, False)


async def record_planning_technical_failure(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    planning_batch_id: UUID,
    planning_batch_attempt_id: UUID,
    failure_code: str,
    message_id: str,
) -> PlanningFailureResult:
    if not _FAILURE_CODE.fullmatch(failure_code):
        raise PlanningFailureCodeInvalidError("failure code must be controlled and bounded")
    async with session_factory() as session, session.begin():
        batch = await _lock_batch(session, planning_batch_id)
        if batch.status == PLANNING_BATCH_COMPLETED:
            await _complete_terminal_replay_inbox(session, planning_batch_id, message_id)
            return PlanningFailureResult(planning_batch_id, None, None, False, True)
        if batch.status != PLANNING_BATCH_READY:
            raise PlanningResultInvalidError("planning batch is not READY")

        attempt = await _lock_attempt(session, planning_batch_attempt_id)
        if attempt.planning_batch_id != batch.planning_batch_id:
            raise PlanningResultInvalidError("planning attempt belongs to another batch")
        if attempt.outcome != PLANNING_ATTEMPT_STARTED:
            raise PlanningResultInvalidError("planning attempt is not STARTED")
        inbox = await _require_planning_inbox(
            session, batch.planning_batch_id, attempt.attempt_number, message_id
        )
        now = utc_now()
        attempt.outcome = PLANNING_ATTEMPT_FAILED
        attempt.failure_code = failure_code
        attempt.completed_at = now

        if attempt.attempt_number < batch.max_attempts_snapshot:
            next_attempt = attempt.attempt_number + 1
            await append_outbox_event(
                session,
                event_key=f"planning-attempt-requested:{batch.planning_batch_id}:{next_attempt}",
                aggregate_type="planning_batch",
                aggregate_id=batch.planning_batch_id,
                event_type=PLANNING_ATTEMPT_REQUESTED_MESSAGE,
                payload={
                    "planning_batch_id": str(batch.planning_batch_id),
                    "attempt_number": next_attempt,
                },
            )
            await complete_inbox_message(session, inbox, processed_at=now)
            return PlanningFailureResult(
                batch.planning_batch_id, attempt.attempt_number, next_attempt, False, False
            )
        if attempt.attempt_number != batch.max_attempts_snapshot:
            raise PlanningResultInvalidError("attempt number exceeds batch maximum")

        population = await _lock_batch_population(session, batch.planning_batch_id)
        fallback_groups = tuple(
            PlanningResultGroup(PLANNING_MODE_FALLBACK_SINGLETON, (request_id,))
            for request_id, _status in population
        )
        await _persist_groups(session, batch.planning_batch_id, fallback_groups, now=now)
        await _transition_population(session, batch.planning_batch_id, len(population))
        batch.status = PLANNING_BATCH_COMPLETED
        batch.completion_mode = PLANNING_COMPLETION_FALLBACK
        batch.completed_at = now
        await _append_completion_event(session, batch.planning_batch_id)
        await complete_inbox_message(session, inbox, processed_at=now)
        return PlanningFailureResult(
            batch.planning_batch_id, attempt.attempt_number, None, True, False
        )


async def _lock_batch(session: AsyncSession, batch_id: UUID) -> PlanningBatch:
    batch = await session.scalar(
        select(PlanningBatch).where(PlanningBatch.planning_batch_id == batch_id).with_for_update()
    )
    if batch is None:
        raise PlanningBatchNotFoundError("planning batch not found")
    return batch


async def _lock_attempt(session: AsyncSession, attempt_id: UUID) -> PlanningBatchAttempt:
    attempt = await session.scalar(
        select(PlanningBatchAttempt)
        .where(PlanningBatchAttempt.planning_batch_attempt_id == attempt_id)
        .with_for_update()
    )
    if attempt is None:
        raise PlanningResultInvalidError("planning attempt not found")
    return attempt


async def _require_planning_inbox(
    session: AsyncSession,
    batch_id: UUID,
    attempt_number: int,
    message_id: str,
) -> InboxMessage:
    inbox = await session.scalar(
        select(InboxMessage)
        .where(
            InboxMessage.consumer_name == PLANNING_WORKER_CONSUMER,
            InboxMessage.message_id == message_id,
        )
        .with_for_update()
    )
    if inbox is None:
        raise PlanningInboxNotFoundError("planning inbox message not found")
    if inbox.business_key != planning_inbox_business_key(batch_id, attempt_number):
        raise PlanningMessageInvalidError("planning inbox business key does not match attempt")
    expected_type = (
        PLANNING_BATCH_READY_MESSAGE if attempt_number == 1 else PLANNING_ATTEMPT_REQUESTED_MESSAGE
    )
    if inbox.message_type != expected_type:
        raise PlanningMessageInvalidError("planning inbox message type does not match attempt")
    if inbox.status == INBOX_PROCESSED:
        raise PlanningMessageInvalidError("planning inbox message is already processed")
    return inbox


async def _complete_terminal_replay_inbox(
    session: AsyncSession, batch_id: UUID, message_id: str
) -> None:
    inbox = await session.scalar(
        select(InboxMessage)
        .where(
            InboxMessage.consumer_name == PLANNING_WORKER_CONSUMER,
            InboxMessage.message_id == message_id,
        )
        .with_for_update()
    )
    if inbox is None or inbox.status == INBOX_PROCESSED:
        return
    prefix = f"{batch_id}:"
    if inbox.business_key is not None and inbox.business_key.startswith(prefix):
        await complete_inbox_message(session, inbox)


async def _lock_batch_population(
    session: AsyncSession, batch_id: UUID
) -> tuple[tuple[UUID, str], ...]:
    population = tuple(
        (row.request_id, row.status)
        for row in (
            await session.execute(
                select(CollectionRequest.request_id, CollectionRequest.status)
                .where(CollectionRequest.planning_batch_id == batch_id)
                .order_by(CollectionRequest.request_id)
                .with_for_update()
            )
        ).all()
    )
    if not population:
        raise PlanningResultInvalidError("planning batch has no request population")
    if any(status != REQUEST_PRE_PLANNING for _request_id, status in population):
        raise PlanningResultInvalidError("batch-owned request is not PRE_PLANNING")
    return population


def _validate_normal_result(
    groups: tuple[PlanningResultGroup, ...], population: tuple[tuple[UUID, str], ...]
) -> None:
    submitted: list[UUID] = []
    for group in groups:
        size = len(group.request_ids)
        if group.planning_mode == PLANNING_MODE_COMPACTED:
            if size < 2:
                raise PlanningResultInvalidError("COMPACTED group requires at least two requests")
        elif group.planning_mode == PLANNING_MODE_NORMAL_SINGLETON:
            if size != 1:
                raise PlanningResultInvalidError("NORMAL_SINGLETON group requires one request")
        else:
            raise PlanningResultInvalidError("normal result contains an unsupported planning mode")
        if len(set(group.request_ids)) != size:
            raise PlanningResultInvalidError("duplicate request within planning group")
        submitted.extend(group.request_ids)
    if len(set(submitted)) != len(submitted):
        raise PlanningResultInvalidError("duplicate request across planning groups")
    expected = {request_id for request_id, _status in population}
    if set(submitted) != expected:
        raise PlanningResultInvalidError("planning result is not the exact batch partition")


async def _persist_groups(
    session: AsyncSession,
    batch_id: UUID,
    groups: tuple[PlanningResultGroup, ...],
    *,
    now: datetime,
) -> None:
    for result_group in groups:
        group_id = new_uuid7()
        session.add(
            CollectionGroup(
                collection_group_id=group_id,
                planning_batch_id=batch_id,
                planning_mode=result_group.planning_mode,
                created_at=now,
            )
        )
        for request_id in result_group.request_ids:
            session.add(
                CollectionGroupMember(
                    collection_group_id=group_id,
                    request_id=request_id,
                    created_at=now,
                )
            )
            session.add(
                PickupExecution(
                    pickup_execution_id=new_uuid7(),
                    request_id=request_id,
                    collection_group_id=group_id,
                    status=PICKUP_PENDING_ASSIGNMENT,
                    created_at=now,
                    collected_at=None,
                    completed_at=None,
                    updated_at=now,
                )
            )
    await session.flush()


async def _transition_population(session: AsyncSession, batch_id: UUID, expected: int) -> None:
    result = await session.execute(
        update(CollectionRequest)
        .where(
            CollectionRequest.planning_batch_id == batch_id,
            CollectionRequest.status == REQUEST_PRE_PLANNING,
        )
        .values(status=REQUEST_PLANNED)
    )
    if cast(CursorResult[Any], result).rowcount != expected:
        raise PlanningResultInvalidError("planning population changed during commit")


async def _append_completion_event(session: AsyncSession, batch_id: UUID) -> None:
    await append_outbox_event(
        session,
        event_key=f"planning-batch-completed:{batch_id}",
        aggregate_type="planning_batch",
        aggregate_id=batch_id,
        event_type=PLANNING_BATCH_COMPLETED_EVENT,
        payload={"planning_batch_id": str(batch_id)},
    )
