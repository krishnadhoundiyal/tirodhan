from __future__ import annotations

from datetime import timedelta
from uuid import UUID

import pytest
from geoalchemy2.elements import WKTElement
from sqlalchemy import func, select, true, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.collection_requests.service import REQUEST_ACCEPTED
from tirodhan.modules.identity.models import AppUser
from tirodhan.modules.planning.compaction import (
    BOUNDED_GREEDY_DIAMETER_V1,
    PlanningAlgorithmFailure,
    PlanningInput,
    PlanningPolicySnapshotError,
    plan_compaction,
)
from tirodhan.modules.planning.execution import (
    execute_planning_attempt,
    load_planning_input,
)
from tirodhan.modules.planning.models import (
    CollectionGroup,
    PlanningBatch,
    PlanningBatchAttempt,
)
from tirodhan.modules.planning.service import (
    PLANNING_ATTEMPT_FAILED,
    PLANNING_ATTEMPT_REQUESTED_MESSAGE,
    PLANNING_ATTEMPT_STARTED,
    PLANNING_BATCH_COMPLETED,
    PLANNING_COMPLETION_ALGORITHM,
    REQUEST_PLANNED,
    PlanningMessage,
    PlanningResultInvalidError,
    PlanningWorkUnit,
    freeze_planning_batch,
    prepare_planning_attempt,
    record_planning_technical_failure,
)
from tirodhan.modules.reliability.models import InboxMessage, OutboxEvent
from tirodhan.modules.reliability.primitives import INBOX_PROCESSING
from tirodhan.modules.serviceability.models import ServiceabilityContext
from tirodhan.modules.serviceability.service import SERVICEABILITY_SERVICEABLE

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

LEAD_TIME_MINUTES = 30
MAX_ATTEMPTS = 3
DISTANCE_M = 100
MAX_GROUP_REQUESTS = 3


async def create_frozen_batch(
    factory: async_sessionmaker[AsyncSession],
    offsets_m: tuple[float, ...],
    *,
    cell_id: str = "compaction-cell",
    distance_m: int = DISTANCE_M,
    max_group_requests: int = MAX_GROUP_REQUESTS,
    max_attempts: int = MAX_ATTEMPTS,
) -> tuple[PlanningBatch, tuple[UUID, ...]]:
    now = utc_now().replace(microsecond=0)
    slot_start = now + timedelta(minutes=10)
    slot_end = slot_start + timedelta(minutes=30)
    async with factory() as session, session.begin():
        user = AppUser(status="ACTIVE")
        session.add(user)
        await session.flush([user])
        request_ids: list[UUID] = []
        for index, offset_m in enumerate(offsets_m):
            context = ServiceabilityContext(
                serviceability_context_id=new_uuid7(),
                user_id=user.user_id,
                address_snapshot_encrypted=b"test-envelope:compaction",
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
                pickup_address_snapshot_encrypted=b"test-envelope:compaction",
                pickup_location=WKTElement("POINT(77.2090 28.6139)", srid=4326),
                cell_id=cell_id,
                slot_start=slot_start,
                slot_end=slot_end,
                quoted_amount_minor=500 + index,
                currency="INR",
                status=REQUEST_ACCEPTED,
                payment_expires_at=now + timedelta(hours=2),
                created_at=now,
                accepted_at=now,
            )
            session.add(request)
            await session.flush([request])
            await session.execute(
                update(CollectionRequest)
                .where(CollectionRequest.request_id == request.request_id)
                .values(
                    pickup_location=func.ST_Project(
                        func.ST_GeogFromText("SRID=4326;POINT(77.2090 28.6139)"),
                        offset_m,
                        0.0,
                    )
                )
            )
            request_ids.append(request.request_id)

    frozen = await freeze_planning_batch(
        factory,
        PlanningWorkUnit(cell_id=cell_id, slot_start=slot_start, slot_end=slot_end),
        lead_time_minutes=LEAD_TIME_MINUTES,
        max_attempts=max_attempts,
        compaction_distance_m=distance_m,
        max_group_requests=max_group_requests,
        now=now,
    )
    assert frozen.batch is not None
    return frozen.batch, tuple(request_ids)


async def prepare_and_load(
    factory: async_sessionmaker[AsyncSession],
    batch: PlanningBatch,
    *,
    message_id: str = "compaction-attempt-1",
) -> tuple[PlanningMessage, PlanningBatchAttempt, PlanningInput]:
    message = PlanningMessage(message_id=message_id, planning_batch_id=batch.planning_batch_id)
    prepared = await prepare_planning_attempt(factory, message)
    assert prepared.attempt is not None
    planning_input = await load_planning_input(
        factory,
        planning_batch_id=batch.planning_batch_id,
        planning_batch_attempt_id=prepared.attempt.planning_batch_attempt_id,
    )
    return message, prepared.attempt, planning_input


async def test_postgis_candidate_projection_uses_meter_threshold_and_batch_scope(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    batch, request_ids = await create_frozen_batch(
        database_session_factory,
        (0.0, 99.0, 100.0, 101.0, 1_000.0),
    )
    other_batch, other_ids = await create_frozen_batch(
        database_session_factory,
        (0.0,),
        cell_id="other-cell",
    )
    _message, _attempt, planning_input = await prepare_and_load(database_session_factory, batch)
    edges = {
        frozenset((edge.request_id_a, edge.request_id_b)): edge.distance_m
        for edge in planning_input.candidate_edges
    }

    async with database_session_factory() as session:
        origin = CollectionRequest.__table__.alias("origin")
        threshold = CollectionRequest.__table__.alias("threshold")
        threshold_row = (
            await session.execute(
                select(
                    func.ST_DWithin(
                        origin.c.pickup_location,
                        threshold.c.pickup_location,
                        DISTANCE_M,
                    ),
                    func.ST_Distance(
                        origin.c.pickup_location,
                        threshold.c.pickup_location,
                    ),
                )
                .select_from(origin.join(threshold, true()))
                .where(
                    origin.c.request_id == request_ids[0],
                    threshold.c.request_id == request_ids[2],
                )
            )
        ).one()

    assert set(planning_input.request_ids) == set(request_ids)
    assert frozenset((request_ids[0], request_ids[1])) in edges
    assert frozenset((request_ids[0], request_ids[2])) in edges
    assert frozenset((request_ids[0], request_ids[3])) not in edges
    assert all(other_id not in endpoint for other_id in other_ids for endpoint in edges)
    assert all(request_ids[4] not in edge for edge in edges)
    assert threshold_row[0] is True
    assert float(threshold_row[1]) == pytest.approx(100.0, abs=0.01)
    assert (
        edges[frozenset((request_ids[0], request_ids[1]))]
        < edges[frozenset((request_ids[0], request_ids[2]))]
    )
    assert other_batch.planning_batch_id != batch.planning_batch_id


async def test_postgis_distance_controls_deterministic_candidate_order(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    batch, request_ids = await create_frozen_batch(
        database_session_factory,
        (0.0, 80.0, 20.0),
        max_group_requests=2,
    )
    _message, _attempt, planning_input = await prepare_and_load(database_session_factory, batch)

    groups = plan_compaction(planning_input)

    assert groups[0].request_ids == (request_ids[0], request_ids[2])
    assert groups[1].request_ids == (request_ids[1],)


async def test_freeze_snapshots_policy_once_and_replay_does_not_overwrite(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    batch, _request_ids = await create_frozen_batch(
        database_session_factory,
        (0.0,),
        distance_m=250,
        max_group_requests=7,
    )
    replay = await freeze_planning_batch(
        database_session_factory,
        PlanningWorkUnit(batch.cell_id, batch.slot_start, batch.slot_end),
        lead_time_minutes=LEAD_TIME_MINUTES,
        max_attempts=9,
        compaction_distance_m=999,
        max_group_requests=99,
        now=batch.slot_start - timedelta(minutes=LEAD_TIME_MINUTES),
    )
    async with database_session_factory() as session:
        persisted = await session.get(PlanningBatch, batch.planning_batch_id)

    assert not replay.created
    assert persisted is not None
    assert persisted.algorithm_version == BOUNDED_GREEDY_DIAMETER_V1
    assert persisted.compaction_distance_m_snapshot == 250
    assert persisted.max_group_requests_snapshot == 7
    assert persisted.max_attempts_snapshot == MAX_ATTEMPTS


async def test_attempt_two_reuses_attempt_one_policy_snapshots(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    batch, _request_ids = await create_frozen_batch(
        database_session_factory,
        (0.0, 50.0),
        distance_m=321,
        max_group_requests=6,
        max_attempts=2,
    )
    message1, attempt1, input1 = await prepare_and_load(database_session_factory, batch)
    await record_planning_technical_failure(
        database_session_factory,
        planning_batch_id=batch.planning_batch_id,
        planning_batch_attempt_id=attempt1.planning_batch_attempt_id,
        failure_code="ALGORITHM_TIMEOUT",
        message_id=message1.message_id,
    )
    message2 = PlanningMessage(
        message_id="compaction-attempt-2",
        planning_batch_id=batch.planning_batch_id,
        attempt_number=2,
        message_type=PLANNING_ATTEMPT_REQUESTED_MESSAGE,
    )
    prepared2 = await prepare_planning_attempt(database_session_factory, message2)
    assert prepared2.attempt is not None
    input2 = await load_planning_input(
        database_session_factory,
        planning_batch_id=batch.planning_batch_id,
        planning_batch_attempt_id=prepared2.attempt.planning_batch_attempt_id,
    )

    assert input2.algorithm_version == input1.algorithm_version
    assert input2.compaction_distance_m == input1.compaction_distance_m == 321
    assert input2.max_group_requests == input1.max_group_requests == 6
    executed = await execute_planning_attempt(database_session_factory, message2)
    assert executed.completion is not None and executed.completion.completed


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("algorithm_version", None),
        ("algorithm_version", "UNSUPPORTED_V2"),
        ("compaction_distance_m_snapshot", None),
        ("max_group_requests_snapshot", None),
    ],
)
async def test_missing_or_unsupported_policy_propagates_without_consuming_attempt(
    database_session_factory: async_sessionmaker[AsyncSession],
    field_name: str,
    value: object,
) -> None:
    batch, _request_ids = await create_frozen_batch(database_session_factory, (0.0,))
    message = PlanningMessage(
        message_id=f"invalid-policy-{field_name}-{value}",
        planning_batch_id=batch.planning_batch_id,
    )
    async with database_session_factory() as session, session.begin():
        await session.execute(
            update(PlanningBatch)
            .where(PlanningBatch.planning_batch_id == batch.planning_batch_id)
            .values({field_name: value})
        )

    with pytest.raises(PlanningPolicySnapshotError):
        await execute_planning_attempt(database_session_factory, message)

    async with database_session_factory() as session:
        attempt = await session.scalar(
            select(PlanningBatchAttempt).where(
                PlanningBatchAttempt.planning_batch_id == batch.planning_batch_id
            )
        )
        inbox = await session.get(InboxMessage, ("planning-worker", message.message_id))
    assert attempt is not None and attempt.outcome == PLANNING_ATTEMPT_STARTED
    assert inbox is not None and inbox.status == INBOX_PROCESSING


async def test_orchestration_executes_planner_and_existing_commit_boundary(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    batch, request_ids = await create_frozen_batch(
        database_session_factory,
        (0.0, 50.0, 500.0),
    )
    message = PlanningMessage(
        message_id="normal-compaction-execution",
        planning_batch_id=batch.planning_batch_id,
    )

    result = await execute_planning_attempt(database_session_factory, message)

    async with database_session_factory() as session:
        persisted_batch = await session.get(PlanningBatch, batch.planning_batch_id)
        statuses = set(
            await session.scalars(
                select(CollectionRequest.status).where(
                    CollectionRequest.request_id.in_(request_ids)
                )
            )
        )
        modes = set(await session.scalars(select(CollectionGroup.planning_mode)))
    assert result.completion is not None and result.completion.completed
    assert persisted_batch is not None
    assert (persisted_batch.status, persisted_batch.completion_mode) == (
        PLANNING_BATCH_COMPLETED,
        PLANNING_COMPLETION_ALGORITHM,
    )
    assert statuses == {REQUEST_PLANNED}
    assert modes == {"COMPACTED", "NORMAL_SINGLETON"}


async def test_terminal_noop_does_not_load_or_run_planner(
    database_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tirodhan.modules.planning import execution

    batch, _request_ids = await create_frozen_batch(database_session_factory, (0.0,))
    message = PlanningMessage(
        message_id="terminal-noop",
        planning_batch_id=batch.planning_batch_id,
    )
    await execute_planning_attempt(database_session_factory, message)

    def unexpected(*args: object, **kwargs: object) -> object:
        raise AssertionError("terminal replay reached planner data/algorithm")

    monkeypatch.setattr(execution, "load_planning_input", unexpected)
    monkeypatch.setattr(execution, "plan_compaction", unexpected)
    replay = await execute_planning_attempt(database_session_factory, message)

    assert replay.preparation.terminal_noop
    assert replay.completion is None and replay.failure is None


async def test_controlled_algorithm_failure_records_exactly_one_failed_attempt(
    database_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tirodhan.modules.planning import execution

    batch, _request_ids = await create_frozen_batch(
        database_session_factory, (0.0, 50.0), max_attempts=2
    )
    message = PlanningMessage(
        message_id="controlled-algorithm-failure",
        planning_batch_id=batch.planning_batch_id,
    )

    def fail_deliberately(planning_input: object) -> object:
        raise PlanningAlgorithmFailure("ALGORITHM_RESOURCE_LIMIT")

    monkeypatch.setattr(execution, "plan_compaction", fail_deliberately)
    result = await execute_planning_attempt(database_session_factory, message)
    replay = await execute_planning_attempt(database_session_factory, message)

    async with database_session_factory() as session:
        attempts = list(
            await session.scalars(
                select(PlanningBatchAttempt).where(
                    PlanningBatchAttempt.planning_batch_id == batch.planning_batch_id
                )
            )
        )
        retry_events = list(
            await session.scalars(
                select(OutboxEvent).where(
                    OutboxEvent.event_type == PLANNING_ATTEMPT_REQUESTED_MESSAGE
                )
            )
        )
    assert result.failure is not None
    assert len(attempts) == 1 and attempts[0].outcome == PLANNING_ATTEMPT_FAILED
    assert attempts[0].failure_code == "ALGORITHM_RESOURCE_LIMIT"
    assert len(retry_events) == 1
    assert replay.preparation.terminal_noop


async def test_commit_validation_failure_propagates_without_consuming_attempt(
    database_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tirodhan.modules.planning import execution

    batch, _request_ids = await create_frozen_batch(database_session_factory, (0.0,))
    message = PlanningMessage(
        message_id="commit-validation-failure",
        planning_batch_id=batch.planning_batch_id,
    )

    async def reject_result(*args: object, **kwargs: object) -> object:
        raise PlanningResultInvalidError("simulated commit rejection")

    monkeypatch.setattr(execution, "commit_planning_result", reject_result)
    with pytest.raises(PlanningResultInvalidError, match="simulated"):
        await execute_planning_attempt(database_session_factory, message)

    async with database_session_factory() as session:
        attempt = await session.scalar(
            select(PlanningBatchAttempt).where(
                PlanningBatchAttempt.planning_batch_id == batch.planning_batch_id
            )
        )
        inbox = await session.get(InboxMessage, ("planning-worker", message.message_id))
    assert attempt is not None and attempt.outcome == PLANNING_ATTEMPT_STARTED
    assert inbox is not None and inbox.status == INBOX_PROCESSING
