from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from pydantic import TypeAdapter
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.planning.routing.adapters import ORTOOLS_VERSION, VROOM_VERSION
from tirodhan.modules.planning.routing.contracts import (
    OptimizerEngine,
    RouteOptimizer,
    RouteProposal,
    RoutingProblem,
)
from tirodhan.modules.planning.routing.execution import RoutingAdapterError
from tirodhan.modules.planning.routing.validation import (
    ProposalAssessment,
    RoutingValidationError,
    validate_proposal,
)

from .commands import OperationalInputError, require_live_role
from .models import RoutingShadowRun
from .snapshot import MatrixSource, ShadowConfiguration, build_problem, capture_snapshot

PROBLEM_CODEC = TypeAdapter(RoutingProblem)
PROPOSAL_CODEC = TypeAdapter(RouteProposal)
ASSESSMENT_CODEC = TypeAdapter(ProposalAssessment)
TERMINAL = {"SUCCEEDED", "FAILED", "STALE"}


async def request_shadow_run(
    factory: async_sessionmaker[AsyncSession],
    *,
    manager_id: UUID,
    client_command_id: UUID,
    config: ShadowConfiguration,
) -> UUID:
    configuration = config.model_dump(mode="json")
    fingerprint = hashlib.sha256(config.model_dump_json().encode()).digest()
    async with factory() as session, session.begin():
        await require_live_role(session, manager_id, "MANAGER")
        await session.execute(
            insert(RoutingShadowRun)
            .values(
                run_id=new_uuid7(),
                requested_by=manager_id,
                client_command_id=client_command_id,
                configuration=configuration,
                fingerprint=fingerprint,
                status="PENDING",
                attempt=0,
            )
            .on_conflict_do_nothing(index_elements=["requested_by", "client_command_id"])
        )
        run = await session.scalar(
            select(RoutingShadowRun).where(
                RoutingShadowRun.requested_by == manager_id,
                RoutingShadowRun.client_command_id == client_command_id,
            )
        )
        assert run is not None
        if run.fingerprint != fingerprint:
            raise OperationalInputError("RUN_COMMAND_CONFLICT")
        return run.run_id


async def read_shadow_run(
    factory: async_sessionmaker[AsyncSession], *, manager_id: UUID, run_id: UUID
) -> RoutingShadowRun:
    async with factory() as session:
        await require_live_role(session, manager_id, "MANAGER")
        run = await session.get(RoutingShadowRun, run_id)
        if run is None:
            raise OperationalInputError("RUN_NOT_FOUND")
        return run


async def process_shadow_run(
    factory: async_sessionmaker[AsyncSession],
    *,
    run_id: UUID,
    providers: dict[str, MatrixSource],
    optimizers: dict[OptimizerEngine, RouteOptimizer],
    clock: Callable[[], datetime] = utc_now,
) -> RoutingShadowRun:
    """Finite, explicitly invoked evaluation; no dispatch registration or business effects."""
    if set(optimizers) != set(OptimizerEngine):
        raise OperationalInputError("BOTH_ENGINES_REQUIRED")
    token = new_uuid7()
    now = clock()
    async with factory() as session, session.begin():
        run = await session.scalar(
            select(RoutingShadowRun).where(RoutingShadowRun.run_id == run_id).with_for_update()
        )
        if run is None:
            raise OperationalInputError("RUN_NOT_FOUND")
        await require_live_role(session, run.requested_by, "MANAGER")
        if run.status in TERMINAL:
            return run
        if run.status == "RUNNING" and run.lease_until is not None and run.lease_until > now:
            raise OperationalInputError("RUN_ALREADY_LEASED")
        config = ShadowConfiguration.model_validate(run.configuration)
        run.status, run.lease_token = "RUNNING", token
        run.lease_until = now + timedelta(seconds=config.lease_seconds)
        run.attempt += 1
        actor, stored, fingerprint = run.requested_by, run.problem, run.source_fingerprint
    try:
        status, results, failure = await asyncio.wait_for(
            _evaluate(
                factory, run_id, token, config, stored, fingerprint, providers, optimizers, clock
            ),
            timeout=config.lease_seconds - 5,
        )
    except (OperationalInputError, RoutingAdapterError, RoutingValidationError) as error:
        status, results, failure = "FAILED", None, str(error)
    except asyncio.TimeoutError:
        status, results, failure = "FAILED", None, "RUN_DEADLINE_EXCEEDED"
    except Exception:
        # Payloads, coordinates and external exceptions never become durable error metadata.
        status, results, failure = "FAILED", None, "EVALUATION_FAILED"
    # Cancellation/crash intentionally leaves a recoverable RUNNING lease.
    async with factory() as session, session.begin():
        try:
            await require_live_role(session, actor, "MANAGER")
        except OperationalInputError:
            status, failure = "STALE", "RESOURCE_AUTHORIZATION_STALE"
        result = await session.scalar(
            update(RoutingShadowRun)
            .where(
                RoutingShadowRun.run_id == run_id,
                RoutingShadowRun.lease_token == token,
                RoutingShadowRun.status == "RUNNING",
                RoutingShadowRun.lease_until > clock(),
            )
            .values(
                status=status,
                results=results,
                failure_code=failure,
                completed_at=clock(),
                lease_token=None,
                lease_until=None,
            )
            .returning(RoutingShadowRun)
        )
        if result is None:
            raise OperationalInputError("RUN_LEASE_LOST")
        return result


async def _evaluate(
    factory: async_sessionmaker[AsyncSession],
    run_id: UUID,
    token: UUID,
    config: ShadowConfiguration,
    stored: dict[str, Any] | None,
    fingerprint: bytes | None,
    providers: dict[str, MatrixSource],
    optimizers: dict[OptimizerEngine, RouteOptimizer],
    clock: Callable[[], datetime],
) -> tuple[str, dict[str, Any] | None, str | None]:
    snapshot = await capture_snapshot(factory, config)
    if stored is None:
        try:
            problem = await build_problem(snapshot, config, run_id, providers, now=clock())
        except (OperationalInputError, RoutingAdapterError, RoutingValidationError) as error:
            return (
                "FAILED",
                {
                    "readiness": {
                        "failure_code": str(error),
                        "request_ids": [str(r["request_id"]) for r in snapshot.sources["requests"]],
                        "vehicle_ids": [str(v["vehicle_id"]) for v in snapshot.sources["vehicles"]],
                    }
                },
                str(error),
            )
        fingerprint = snapshot.fingerprint
        async with factory() as session, session.begin():
            saved = await session.scalar(
                update(RoutingShadowRun)
                .where(
                    RoutingShadowRun.run_id == run_id,
                    RoutingShadowRun.lease_token == token,
                    RoutingShadowRun.lease_until > clock(),
                )
                .values(
                    problem=PROBLEM_CODEC.dump_python(problem, mode="json"),
                    source_fingerprint=fingerprint,
                )
                .returning(RoutingShadowRun.run_id)
            )
            if saved is None:
                raise OperationalInputError("RUN_LEASE_LOST")
    else:
        if snapshot.fingerprint != fingerprint:
            return "STALE", None, "SOURCE_CHANGED"
        problem = PROBLEM_CODEC.validate_python(stored)
    now = clock()
    if now >= config.horizon_end or any(
        v["observed_at"] is None
        or not 0 <= (now - v["observed_at"]).total_seconds() <= config.max_position_age_seconds
        for v in snapshot.sources["vehicles"]
    ):
        return "STALE", None, "POSITION_OR_HORIZON_EXPIRED"
    results: dict[str, Any] = {}
    versions = {OptimizerEngine.VROOM: VROOM_VERSION, OptimizerEngine.OR_TOOLS: ORTOOLS_VERSION}
    for engine in OptimizerEngine:
        try:
            candidate = await optimizers[engine].propose(
                problem, search_seconds=config.search_seconds
            )
            if candidate.engine != engine or candidate.engine_version != versions[engine]:
                raise OperationalInputError("ENGINE_IDENTITY_MISMATCH")
            assessment = validate_proposal(problem, candidate)
            results[engine.value] = {
                "proposal": PROPOSAL_CODEC.dump_python(candidate, mode="json"),
                "assessment": ASSESSMENT_CODEC.dump_python(assessment, mode="json"),
                "travel_seconds": sum(r.travel_seconds for r in assessment.routes),
                "distance_m": sum(r.distance_m for r in assessment.routes),
            }
        except (OperationalInputError, RoutingAdapterError, RoutingValidationError) as error:
            results[engine.value] = {"failure_code": str(error)}
        except Exception:
            results[engine.value] = {"failure_code": "ENGINE_FAILED"}
    # This detects known drift, not a dispatch lock/fence or a promise of current feasibility.
    current = await capture_snapshot(factory, config)
    if current.fingerprint != fingerprint:
        return "STALE", results, "SOURCE_CHANGED"
    now = clock()
    if now >= config.horizon_end or any(
        (now - v["observed_at"]).total_seconds() > config.max_position_age_seconds
        for v in current.sources["vehicles"]
    ):
        return "STALE", results, "POSITION_OR_HORIZON_EXPIRED"
    failed = any("failure_code" in result for result in results.values())
    return (
        ("FAILED", results, "ENGINE_EVALUATION_FAILED") if failed else ("SUCCEEDED", results, None)
    )
