from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.collection_requests.service import REQUEST_PRE_PLANNING
from tirodhan.modules.planning.compaction import (
    BOUNDED_GREEDY_DIAMETER_V1,
    PlanningAlgorithmFailure,
    PlanningCandidateEdge,
    PlanningInput,
    PlanningPolicySnapshotError,
    plan_compaction,
)
from tirodhan.modules.planning.models import PlanningBatch, PlanningBatchAttempt
from tirodhan.modules.planning.service import (
    PLANNING_ATTEMPT_STARTED,
    PLANNING_BATCH_READY,
    PlanningCompletionResult,
    PlanningFailureResult,
    PlanningMessage,
    PlanningResult,
    PlanningResultGroup,
    PreparePlanningAttemptResult,
    commit_planning_result,
    prepare_planning_attempt,
    record_planning_technical_failure,
)


class PlanningExecutionStateError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PlanningAttemptExecutionResult:
    preparation: PreparePlanningAttemptResult
    completion: PlanningCompletionResult | None = None
    failure: PlanningFailureResult | None = None


async def load_planning_input(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    planning_batch_id: UUID,
    planning_batch_attempt_id: UUID,
) -> PlanningInput:
    async with session_factory() as session, session.begin():
        batch_row = (
            await session.execute(
                select(
                    PlanningBatch.status,
                    PlanningBatch.algorithm_version,
                    PlanningBatch.compaction_distance_m_snapshot,
                    PlanningBatch.max_group_requests_snapshot,
                ).where(PlanningBatch.planning_batch_id == planning_batch_id)
            )
        ).one_or_none()
        if batch_row is None:
            raise PlanningExecutionStateError("planning batch not found")
        if batch_row.status != PLANNING_BATCH_READY:
            raise PlanningExecutionStateError("planning batch is not READY")
        algorithm_version = batch_row.algorithm_version
        distance_m = batch_row.compaction_distance_m_snapshot
        max_group_requests = batch_row.max_group_requests_snapshot
        if algorithm_version is None:
            raise PlanningPolicySnapshotError("planning algorithm version snapshot is missing")
        if algorithm_version != BOUNDED_GREEDY_DIAMETER_V1:
            raise PlanningPolicySnapshotError("unsupported planning algorithm version")
        if distance_m is None or distance_m <= 0:
            raise PlanningPolicySnapshotError("compaction distance snapshot is missing or invalid")
        if max_group_requests is None or max_group_requests <= 0:
            raise PlanningPolicySnapshotError("max group requests snapshot is missing or invalid")

        attempt_row = (
            await session.execute(
                select(
                    PlanningBatchAttempt.planning_batch_id,
                    PlanningBatchAttempt.outcome,
                ).where(PlanningBatchAttempt.planning_batch_attempt_id == planning_batch_attempt_id)
            )
        ).one_or_none()
        if attempt_row is None:
            raise PlanningExecutionStateError("planning attempt not found")
        if attempt_row.planning_batch_id != planning_batch_id:
            raise PlanningExecutionStateError("planning attempt belongs to another batch")
        if attempt_row.outcome != PLANNING_ATTEMPT_STARTED:
            raise PlanningExecutionStateError("planning attempt is not STARTED")

        population_rows = (
            await session.execute(
                select(CollectionRequest.request_id, CollectionRequest.status)
                .where(CollectionRequest.planning_batch_id == planning_batch_id)
                .order_by(CollectionRequest.request_id)
            )
        ).all()
        if not population_rows:
            raise PlanningExecutionStateError("planning batch has no request population")
        if any(row.status != REQUEST_PRE_PLANNING for row in population_rows):
            raise PlanningExecutionStateError("batch-owned request is not PRE_PLANNING")

        request_a = CollectionRequest.__table__.alias("planning_request_a")
        request_b = CollectionRequest.__table__.alias("planning_request_b")
        edge_rows = (
            await session.execute(
                select(
                    request_a.c.request_id.label("request_id_a"),
                    request_b.c.request_id.label("request_id_b"),
                    func.ST_Distance(
                        request_a.c.pickup_location,
                        request_b.c.pickup_location,
                    ).label("distance_m"),
                )
                .select_from(
                    request_a.join(
                        request_b,
                        and_(
                            request_a.c.request_id < request_b.c.request_id,
                            request_a.c.planning_batch_id == request_b.c.planning_batch_id,
                            func.ST_DWithin(
                                request_a.c.pickup_location,
                                request_b.c.pickup_location,
                                distance_m,
                            ),
                        ),
                    )
                )
                .where(
                    request_a.c.planning_batch_id == planning_batch_id,
                    request_b.c.planning_batch_id == planning_batch_id,
                )
                .order_by(request_a.c.request_id, request_b.c.request_id)
            )
        ).all()

    return PlanningInput(
        planning_batch_id=planning_batch_id,
        planning_batch_attempt_id=planning_batch_attempt_id,
        request_ids=tuple(row.request_id for row in population_rows),
        candidate_edges=tuple(
            PlanningCandidateEdge(
                request_id_a=row.request_id_a,
                request_id_b=row.request_id_b,
                distance_m=float(row.distance_m),
            )
            for row in edge_rows
        ),
        compaction_distance_m=distance_m,
        max_group_requests=max_group_requests,
        algorithm_version=algorithm_version,
    )


async def execute_planning_attempt(
    session_factory: async_sessionmaker[AsyncSession],
    message: PlanningMessage,
) -> PlanningAttemptExecutionResult:
    preparation = await prepare_planning_attempt(session_factory, message)
    if preparation.terminal_noop:
        return PlanningAttemptExecutionResult(preparation=preparation)
    attempt = preparation.attempt
    if attempt is None or attempt.outcome != PLANNING_ATTEMPT_STARTED:
        raise PlanningExecutionStateError("planning preparation did not return a STARTED attempt")

    planning_input = await load_planning_input(
        session_factory,
        planning_batch_id=message.planning_batch_id,
        planning_batch_attempt_id=attempt.planning_batch_attempt_id,
    )
    try:
        planned_groups = plan_compaction(planning_input)
    except PlanningAlgorithmFailure as exc:
        failure = await record_planning_technical_failure(
            session_factory,
            planning_batch_id=message.planning_batch_id,
            planning_batch_attempt_id=attempt.planning_batch_attempt_id,
            failure_code=exc.failure_code,
            message_id=message.message_id,
        )
        return PlanningAttemptExecutionResult(preparation=preparation, failure=failure)

    completion = await commit_planning_result(
        session_factory,
        PlanningResult(
            planning_batch_id=planning_input.planning_batch_id,
            planning_batch_attempt_id=planning_input.planning_batch_attempt_id,
            groups=tuple(
                PlanningResultGroup(
                    planning_mode=group.planning_mode,
                    request_ids=group.request_ids,
                )
                for group in planned_groups
            ),
        ),
        message_id=message.message_id,
    )
    return PlanningAttemptExecutionResult(preparation=preparation, completion=completion)
