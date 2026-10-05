from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from uuid import UUID

import pytest
from geoalchemy2.elements import WKTElement
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.collection_requests.service import REQUEST_PRE_PLANNING
from tirodhan.modules.identity.models import AppUser
from tirodhan.modules.planning.models import (
    CollectionGroup,
    CollectionGroupMember,
    PickupExecution,
    PlanningBatch,
    PlanningBatchAttempt,
)
from tirodhan.modules.planning.service import (
    PICKUP_PENDING_ASSIGNMENT,
    PLANNING_ATTEMPT_FAILED,
    PLANNING_ATTEMPT_REQUESTED_MESSAGE,
    PLANNING_ATTEMPT_STARTED,
    PLANNING_ATTEMPT_SUCCEEDED,
    PLANNING_BATCH_COMPLETED,
    PLANNING_BATCH_COMPLETED_EVENT,
    PLANNING_BATCH_READY,
    PLANNING_COMPLETION_ALGORITHM,
    PLANNING_COMPLETION_FALLBACK,
    PLANNING_MODE_COMPACTED,
    PLANNING_MODE_FALLBACK_SINGLETON,
    PLANNING_MODE_NORMAL_SINGLETON,
    REQUEST_PLANNED,
    PlanningAttemptNotStartableError,
    PlanningInboxNotFoundError,
    PlanningMessage,
    PlanningResult,
    PlanningResultGroup,
    PlanningResultInvalidError,
    commit_planning_result,
    prepare_planning_attempt,
    record_planning_technical_failure,
)
from tirodhan.modules.reliability.models import InboxMessage, OutboxEvent
from tirodhan.modules.reliability.primitives import INBOX_PROCESSED, INBOX_PROCESSING
from tirodhan.modules.serviceability.models import ServiceabilityContext
from tirodhan.modules.serviceability.service import SERVICEABILITY_SERVICEABLE

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def create_started_batch(
    factory: async_sessionmaker[AsyncSession],
    *,
    request_count: int = 3,
    max_attempts: int = 3,
    cell_id: str = "result-cell",
    message_id: str = "planning-result-1",
) -> tuple[PlanningBatch, PlanningBatchAttempt, tuple[UUID, ...], PlanningMessage]:
    now = utc_now().replace(microsecond=0)
    slot_start = now + timedelta(hours=1)
    async with factory() as session, session.begin():
        user = AppUser(status="ACTIVE")
        batch = PlanningBatch(
            planning_batch_id=new_uuid7(),
            cell_id=cell_id,
            slot_start=slot_start,
            slot_end=slot_start + timedelta(minutes=30),
            status=PLANNING_BATCH_READY,
            max_attempts_snapshot=max_attempts,
            created_at=now,
        )
        session.add_all([user, batch])
        await session.flush()
        request_ids: list[UUID] = []
        for index in range(request_count):
            context = ServiceabilityContext(
                serviceability_context_id=new_uuid7(),
                user_id=user.user_id,
                address_snapshot_encrypted=b"test-envelope:planning-result",
                location=WKTElement("POINT(77.2090 28.6139)", srid=4326),
                cell_id=cell_id,
                status=SERVICEABILITY_SERVICEABLE,
                expires_at=now + timedelta(days=1),
                created_at=now,
                resolved_at=now,
            )
            session.add(context)
            await session.flush([context])
            request = CollectionRequest(
                request_id=new_uuid7(),
                client_request_id=new_uuid7(),
                customer_id=user.user_id,
                serviceability_context_id=context.serviceability_context_id,
                pickup_address_snapshot_encrypted=b"test-envelope:planning-result",
                pickup_location=WKTElement("POINT(77.2090 28.6139)", srid=4326),
                cell_id=cell_id,
                slot_start=batch.slot_start,
                slot_end=batch.slot_end,
                quoted_amount_minor=500 + index,
                currency="INR",
                status=REQUEST_PRE_PLANNING,
                payment_expires_at=now + timedelta(hours=2),
                planning_batch_id=batch.planning_batch_id,
                created_at=now,
                accepted_at=now,
            )
            session.add(request)
            request_ids.append(request.request_id)
    message = PlanningMessage(message_id=message_id, planning_batch_id=batch.planning_batch_id)
    prepared = await prepare_planning_attempt(factory, message)
    assert prepared.attempt is not None
    return batch, prepared.attempt, tuple(request_ids), message


def normal_result(
    batch: PlanningBatch, attempt: PlanningBatchAttempt, request_ids: tuple[UUID, ...]
) -> PlanningResult:
    groups = (
        PlanningResultGroup(PLANNING_MODE_COMPACTED, request_ids[:2]),
        PlanningResultGroup(PLANNING_MODE_NORMAL_SINGLETON, request_ids[2:]),
    )
    return PlanningResult(batch.planning_batch_id, attempt.planning_batch_attempt_id, groups)


async def counts(factory: async_sessionmaker[AsyncSession]) -> tuple[int, int, int, int]:
    async with factory() as session:
        return (
            int(await session.scalar(select(func.count(CollectionGroup.collection_group_id))) or 0),
            int(await session.scalar(select(func.count()).select_from(CollectionGroupMember)) or 0),
            int(await session.scalar(select(func.count(PickupExecution.pickup_execution_id))) or 0),
            int(
                await session.scalar(
                    select(func.count(OutboxEvent.outbox_event_id)).where(
                        OutboxEvent.event_type == PLANNING_BATCH_COMPLETED_EVENT
                    )
                )
                or 0
            ),
        )


async def test_normal_result_commits_complete_atomic_partition(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    batch, attempt, request_ids, message = await create_started_batch(database_session_factory)

    committed = await commit_planning_result(
        database_session_factory,
        normal_result(batch, attempt, request_ids),
        message_id=message.message_id,
    )

    async with database_session_factory() as session:
        persisted_batch = await session.get(PlanningBatch, batch.planning_batch_id)
        persisted_attempt = await session.get(
            PlanningBatchAttempt, attempt.planning_batch_attempt_id
        )
        requests = list(
            await session.scalars(
                select(CollectionRequest).where(CollectionRequest.request_id.in_(request_ids))
            )
        )
        groups = list(await session.scalars(select(CollectionGroup)))
        members = list(await session.scalars(select(CollectionGroupMember)))
        pickups = list(await session.scalars(select(PickupExecution)))
        inbox = await session.get(InboxMessage, ("planning-worker", message.message_id))
        completed_event = await session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.event_key == f"planning-batch-completed:{batch.planning_batch_id}"
            )
        )

    assert committed.completed and not committed.already_completed
    assert persisted_batch is not None
    assert (persisted_batch.status, persisted_batch.completion_mode) == (
        PLANNING_BATCH_COMPLETED,
        PLANNING_COMPLETION_ALGORITHM,
    )
    assert persisted_attempt is not None
    assert persisted_attempt.outcome == PLANNING_ATTEMPT_SUCCEEDED
    assert {request.status for request in requests} == {REQUEST_PLANNED}
    assert {group.planning_mode for group in groups} == {
        PLANNING_MODE_COMPACTED,
        PLANNING_MODE_NORMAL_SINGLETON,
    }
    assert len(members) == len(request_ids)
    assert {member.request_id for member in members} == set(request_ids)
    assert len(pickups) == len(request_ids)
    assert {pickup.status for pickup in pickups} == {PICKUP_PENDING_ASSIGNMENT}
    member_groups = {member.request_id: member.collection_group_id for member in members}
    pickup_groups = {pickup.request_id: pickup.collection_group_id for pickup in pickups}
    assert pickup_groups == member_groups
    assert inbox is not None and inbox.status == INBOX_PROCESSED
    assert completed_event is not None
    assert completed_event.payload == {"planning_batch_id": str(batch.planning_batch_id)}


@pytest.mark.parametrize(
    "groups_factory",
    [
        lambda ids: (PlanningResultGroup(PLANNING_MODE_COMPACTED, ids[:2]),),
        lambda ids: (
            PlanningResultGroup(PLANNING_MODE_COMPACTED, (ids[0], ids[0], ids[1])),
            PlanningResultGroup(PLANNING_MODE_NORMAL_SINGLETON, (ids[2],)),
        ),
        lambda ids: (
            PlanningResultGroup(PLANNING_MODE_COMPACTED, ids[:2]),
            PlanningResultGroup(PLANNING_MODE_NORMAL_SINGLETON, (ids[1],)),
            PlanningResultGroup(PLANNING_MODE_NORMAL_SINGLETON, (ids[2],)),
        ),
        lambda ids: (
            PlanningResultGroup(PLANNING_MODE_COMPACTED, ()),
            PlanningResultGroup(PLANNING_MODE_COMPACTED, ids),
        ),
        lambda ids: (
            PlanningResultGroup(PLANNING_MODE_COMPACTED, (ids[0],)),
            PlanningResultGroup(PLANNING_MODE_COMPACTED, ids[1:]),
        ),
        lambda ids: (PlanningResultGroup(PLANNING_MODE_NORMAL_SINGLETON, ids),),
        lambda ids: (
            PlanningResultGroup(PLANNING_MODE_FALLBACK_SINGLETON, (ids[0],)),
            PlanningResultGroup(PLANNING_MODE_COMPACTED, ids[1:]),
        ),
        lambda ids: (
            PlanningResultGroup(PLANNING_MODE_COMPACTED, ids[:2]),
            PlanningResultGroup(PLANNING_MODE_NORMAL_SINGLETON, (new_uuid7(),)),
        ),
    ],
    ids=[
        "missing-request",
        "duplicate-within",
        "duplicate-across",
        "empty-group",
        "small-compacted",
        "large-normal-singleton",
        "caller-fallback",
        "foreign-request",
    ],
)
async def test_invalid_result_partitions_are_rejected_without_effects(
    database_session_factory: async_sessionmaker[AsyncSession], groups_factory: object
) -> None:
    batch, attempt, request_ids, message = await create_started_batch(database_session_factory)
    groups = groups_factory(request_ids)  # type: ignore[operator]
    result = PlanningResult(batch.planning_batch_id, attempt.planning_batch_attempt_id, groups)

    with pytest.raises(PlanningResultInvalidError):
        await commit_planning_result(
            database_session_factory, result, message_id=message.message_id
        )

    assert await counts(database_session_factory) == (0, 0, 0, 0)


async def test_invalid_population_attempt_and_missing_inbox_are_rejected(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    batch, attempt, request_ids, message = await create_started_batch(database_session_factory)
    result = normal_result(batch, attempt, request_ids)
    async with database_session_factory() as session, session.begin():
        await session.execute(
            update(CollectionRequest)
            .where(CollectionRequest.request_id == request_ids[0])
            .values(status=REQUEST_PLANNED)
        )
    with pytest.raises(PlanningResultInvalidError, match="not PRE_PLANNING"):
        await commit_planning_result(
            database_session_factory, result, message_id=message.message_id
        )

    async with database_session_factory() as session, session.begin():
        await session.execute(
            update(CollectionRequest)
            .where(CollectionRequest.request_id == request_ids[0])
            .values(status=REQUEST_PRE_PLANNING)
        )
    with pytest.raises(PlanningInboxNotFoundError):
        await commit_planning_result(database_session_factory, result, message_id="missing-inbox")

    batch2, attempt2, ids2, message2 = await create_started_batch(
        database_session_factory, cell_id="other-cell", message_id="other-message"
    )
    wrong_attempt = replace(result, planning_batch_attempt_id=attempt2.planning_batch_attempt_id)
    with pytest.raises(PlanningResultInvalidError, match="another batch"):
        await commit_planning_result(
            database_session_factory, wrong_attempt, message_id=message.message_id
        )
    async with database_session_factory() as session, session.begin():
        terminal_attempt = await session.get(
            PlanningBatchAttempt, attempt2.planning_batch_attempt_id
        )
        assert terminal_attempt is not None
        terminal_attempt.outcome = PLANNING_ATTEMPT_FAILED
    with pytest.raises(PlanningResultInvalidError, match="not STARTED"):
        await commit_planning_result(
            database_session_factory,
            normal_result(batch2, attempt2, ids2),
            message_id=message2.message_id,
        )


async def test_completed_batch_replays_and_competing_stale_result_noop(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    batch, attempt, request_ids, message = await create_started_batch(database_session_factory)
    result = normal_result(batch, attempt, request_ids)
    first = await commit_planning_result(
        database_session_factory, result, message_id=message.message_id
    )
    exact = await commit_planning_result(
        database_session_factory, result, message_id=message.message_id
    )
    stale = replace(
        result,
        groups=(PlanningResultGroup(PLANNING_MODE_FALLBACK_SINGLETON, (new_uuid7(),)),),
    )
    competing = await commit_planning_result(
        database_session_factory, stale, message_id="message-that-never-existed"
    )

    assert first.completed
    assert exact.already_completed and competing.already_completed
    assert await counts(database_session_factory) == (2, 3, 3, 1)


async def test_concurrent_different_results_have_one_authoritative_winner(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    batch, attempt, request_ids, message = await create_started_batch(database_session_factory)
    result_a = normal_result(batch, attempt, request_ids)
    result_b = PlanningResult(
        batch.planning_batch_id,
        attempt.planning_batch_attempt_id,
        (
            PlanningResultGroup(PLANNING_MODE_NORMAL_SINGLETON, (request_ids[0],)),
            PlanningResultGroup(PLANNING_MODE_COMPACTED, request_ids[1:]),
        ),
    )
    outcomes = await asyncio.gather(
        commit_planning_result(database_session_factory, result_a, message_id=message.message_id),
        commit_planning_result(database_session_factory, result_b, message_id=message.message_id),
    )

    assert sum(outcome.completed for outcome in outcomes) == 1
    assert sum(outcome.already_completed for outcome in outcomes) == 1
    assert (await counts(database_session_factory))[1:] == (3, 3, 1)


async def test_concurrent_same_result_has_one_durable_completion(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    batch, attempt, request_ids, message = await create_started_batch(database_session_factory)
    result = normal_result(batch, attempt, request_ids)

    outcomes = await asyncio.gather(
        commit_planning_result(database_session_factory, result, message_id=message.message_id),
        commit_planning_result(database_session_factory, result, message_id=message.message_id),
    )

    assert sum(outcome.completed for outcome in outcomes) == 1
    assert sum(outcome.already_completed for outcome in outcomes) == 1
    assert await counts(database_session_factory) == (2, 3, 3, 1)


async def test_technical_failure_requests_explicit_next_attempt_and_old_delivery_noops(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    batch, attempt1, request_ids, message1 = await create_started_batch(database_session_factory)
    failure = await record_planning_technical_failure(
        database_session_factory,
        planning_batch_id=batch.planning_batch_id,
        planning_batch_attempt_id=attempt1.planning_batch_attempt_id,
        failure_code="PLANNER_TIMEOUT",
        message_id=message1.message_id,
    )
    message2 = PlanningMessage(
        message_id="planning-result-2",
        planning_batch_id=batch.planning_batch_id,
        attempt_number=2,
        message_type=PLANNING_ATTEMPT_REQUESTED_MESSAGE,
    )
    two_prepares = await asyncio.gather(
        prepare_planning_attempt(database_session_factory, message2),
        prepare_planning_attempt(database_session_factory, message2),
    )
    old = await prepare_planning_attempt(database_session_factory, message1)

    async with database_session_factory() as session:
        durable_batch = await session.get(PlanningBatch, batch.planning_batch_id)
        attempts = list(
            await session.scalars(
                select(PlanningBatchAttempt)
                .where(PlanningBatchAttempt.planning_batch_id == batch.planning_batch_id)
                .order_by(PlanningBatchAttempt.attempt_number)
            )
        )
        inboxes = list(
            await session.scalars(
                select(InboxMessage).where(InboxMessage.consumer_name == "planning-worker")
            )
        )
        retry_events = list(
            await session.scalars(
                select(OutboxEvent).where(
                    OutboxEvent.event_type == PLANNING_ATTEMPT_REQUESTED_MESSAGE
                )
            )
        )
        statuses = set(
            await session.scalars(
                select(CollectionRequest.status).where(
                    CollectionRequest.request_id.in_(request_ids)
                )
            )
        )

    assert failure.next_attempt_number == 2
    assert durable_batch is not None and durable_batch.status == PLANNING_BATCH_READY
    assert [(item.attempt_number, item.outcome) for item in attempts] == [
        (1, PLANNING_ATTEMPT_FAILED),
        (2, PLANNING_ATTEMPT_STARTED),
    ]
    assert sum(item.created for item in two_prepares) == 1
    assert old.terminal_noop and old.attempt is None
    assert {item.business_key for item in inboxes} == {
        f"{batch.planning_batch_id}:1",
        f"{batch.planning_batch_id}:2",
    }
    assert len(retry_events) == 1
    assert retry_events[0].payload == {
        "planning_batch_id": str(batch.planning_batch_id),
        "attempt_number": 2,
        "cell_id": "result-cell",
    }
    assert statuses == {REQUEST_PRE_PLANNING}


async def test_attempt_two_requires_failed_predecessor_and_respects_maximum(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    batch, _attempt, _ids, _message = await create_started_batch(
        database_session_factory, max_attempts=2
    )
    premature = PlanningMessage(
        "premature-2",
        batch.planning_batch_id,
        2,
        PLANNING_ATTEMPT_REQUESTED_MESSAGE,
    )
    with pytest.raises(PlanningAttemptNotStartableError, match="not FAILED"):
        await prepare_planning_attempt(database_session_factory, premature)
    excessive = PlanningMessage(
        "attempt-3",
        batch.planning_batch_id,
        3,
        PLANNING_ATTEMPT_REQUESTED_MESSAGE,
    )
    with pytest.raises(PlanningAttemptNotStartableError, match="maximum"):
        await prepare_planning_attempt(database_session_factory, excessive)


async def test_exhausted_attempt_creates_fallback_singletons(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    batch, attempt, request_ids, message = await create_started_batch(
        database_session_factory, max_attempts=1
    )
    outcome = await record_planning_technical_failure(
        database_session_factory,
        planning_batch_id=batch.planning_batch_id,
        planning_batch_attempt_id=attempt.planning_batch_attempt_id,
        failure_code="ALGORITHM_FAILED",
        message_id=message.message_id,
    )
    async with database_session_factory() as session:
        durable_batch = await session.get(PlanningBatch, batch.planning_batch_id)
        durable_attempt = await session.get(PlanningBatchAttempt, attempt.planning_batch_attempt_id)
        groups = list(await session.scalars(select(CollectionGroup)))
        statuses = set(
            await session.scalars(
                select(CollectionRequest.status).where(
                    CollectionRequest.request_id.in_(request_ids)
                )
            )
        )
        retry_count = await session.scalar(
            select(func.count(OutboxEvent.outbox_event_id)).where(
                OutboxEvent.event_type == PLANNING_ATTEMPT_REQUESTED_MESSAGE
            )
        )
        inbox = await session.get(InboxMessage, ("planning-worker", message.message_id))

    assert outcome.fallback_completed and outcome.next_attempt_number is None
    assert durable_attempt is not None and durable_attempt.outcome == PLANNING_ATTEMPT_FAILED
    assert durable_batch is not None
    assert (durable_batch.status, durable_batch.completion_mode) == (
        PLANNING_BATCH_COMPLETED,
        PLANNING_COMPLETION_FALLBACK,
    )
    assert len(groups) == len(request_ids)
    assert {group.planning_mode for group in groups} == {PLANNING_MODE_FALLBACK_SINGLETON}
    assert statuses == {REQUEST_PLANNED}
    assert await counts(database_session_factory) == (3, 3, 3, 1)
    assert retry_count == 0
    assert inbox is not None and inbox.status == INBOX_PROCESSED


@pytest.mark.parametrize("operation", ["success", "fallback"])
async def test_late_failure_rolls_back_entire_result_boundary(
    database_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    from tirodhan.modules.planning import service as planning_service

    batch, attempt, request_ids, message = await create_started_batch(
        database_session_factory, max_attempts=1
    )

    async def fail_event(session: AsyncSession, batch_id: UUID) -> None:
        raise RuntimeError("simulated persistence failure")

    monkeypatch.setattr(planning_service, "_append_completion_event", fail_event)
    with pytest.raises(RuntimeError, match="simulated"):
        if operation == "success":
            await commit_planning_result(
                database_session_factory,
                normal_result(batch, attempt, request_ids),
                message_id=message.message_id,
            )
        else:
            await record_planning_technical_failure(
                database_session_factory,
                planning_batch_id=batch.planning_batch_id,
                planning_batch_attempt_id=attempt.planning_batch_attempt_id,
                failure_code="ALGORITHM_FAILED",
                message_id=message.message_id,
            )

    async with database_session_factory() as session:
        durable_batch = await session.get(PlanningBatch, batch.planning_batch_id)
        durable_attempt = await session.get(PlanningBatchAttempt, attempt.planning_batch_attempt_id)
        inbox = await session.get(InboxMessage, ("planning-worker", message.message_id))
        statuses = set(
            await session.scalars(
                select(CollectionRequest.status).where(
                    CollectionRequest.request_id.in_(request_ids)
                )
            )
        )
    assert await counts(database_session_factory) == (0, 0, 0, 0)
    assert durable_batch is not None and durable_batch.status == PLANNING_BATCH_READY
    assert durable_attempt is not None and durable_attempt.outcome == PLANNING_ATTEMPT_STARTED
    assert inbox is not None and inbox.status == INBOX_PROCESSING
    assert statuses == {REQUEST_PRE_PLANNING}


async def test_success_and_failure_race_has_one_terminal_winner(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    batch, attempt, request_ids, message = await create_started_batch(
        database_session_factory, max_attempts=1
    )
    outcomes = await asyncio.gather(
        commit_planning_result(
            database_session_factory,
            normal_result(batch, attempt, request_ids),
            message_id=message.message_id,
        ),
        record_planning_technical_failure(
            database_session_factory,
            planning_batch_id=batch.planning_batch_id,
            planning_batch_attempt_id=attempt.planning_batch_attempt_id,
            failure_code="ALGORITHM_FAILED",
            message_id=message.message_id,
        ),
        return_exceptions=True,
    )
    async with database_session_factory() as session:
        durable_batch = await session.get(PlanningBatch, batch.planning_batch_id)
        durable_attempt = await session.get(PlanningBatchAttempt, attempt.planning_batch_attempt_id)
    assert durable_batch is not None and durable_batch.status == PLANNING_BATCH_COMPLETED
    assert durable_attempt is not None
    assert durable_attempt.outcome in {PLANNING_ATTEMPT_SUCCEEDED, PLANNING_ATTEMPT_FAILED}
    assert not any(isinstance(outcome, Exception) for outcome in outcomes)
    assert (await counts(database_session_factory))[1:] == (3, 3, 1)


async def test_planning_reliability_metadata_is_control_only(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    batch, attempt, _request_ids, message = await create_started_batch(database_session_factory)
    await record_planning_technical_failure(
        database_session_factory,
        planning_batch_id=batch.planning_batch_id,
        planning_batch_attempt_id=attempt.planning_batch_attempt_id,
        failure_code="PLANNER_TIMEOUT",
        message_id=message.message_id,
    )
    async with database_session_factory() as session:
        inboxes = list(await session.scalars(select(InboxMessage)))
        outbox = list(await session.scalars(select(OutboxEvent)))
    metadata = " ".join(
        [
            *(f"{item.message_type} {item.business_key}" for item in inboxes),
            *(f"{item.event_type} {item.event_key} {item.payload}" for item in outbox),
        ]
    ).lower()
    for prohibited in (
        "address",
        "coordinate",
        "latitude",
        "longitude",
        "77.2090",
        "28.6139",
        "payment",
        "token",
        "phone",
    ):
        assert prohibited not in metadata
