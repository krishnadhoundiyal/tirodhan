"""Finite, advisory replacement recommendations using existing exact insertion and gate.

Rank independently feasible continuations by committed duty then incremental travel.
Search exhaustion is UNKNOWN. Approval and physical transfer are separate commands.
"""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.db.values import new_uuid7
from tirodhan.modules.planning.routing.contracts import (
    OptimizerEngine,
    ProposedRoute,
    ResourceKind,
    RouteOptimizer,
    RouteProposal,
    SelectionEngine,
)
from tirodhan.modules.planning.routing.execution import RoutingAdapterError
from tirodhan.modules.planning.routing.validation import RoutingValidationError, validate_proposal

from .admission import insert_obligation
from .admission_models import AdmissionSourceHead
from .admission_queries import in_ids
from .admission_state import read_cache
from .admission_store import current_proofs
from .capacity_models import RiderReplacement
from .commands import OperationalInputError
from .orchestration_models import CapacityReservation, OperatingRegion
from .regional_planner import regional_ready, subset
from .rolling import FleetPolicy, incumbent_orders
from .snapshot import MatrixSource, OperationalSnapshot, _read_sources, build_problem, source_digest


async def recommend_replacement(
    factory: async_sessionmaker[AsyncSession],
    replacement_id: UUID,
    *,
    providers: dict[str, MatrixSource],
    optimizers: dict[OptimizerEngine, RouteOptimizer],
    now: datetime,
    max_wall_seconds: float = 30,
) -> bool:
    async with factory() as session:
        await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        request = await session.get(RiderReplacement, replacement_id)
        approved = bool(
            request
            and request.status == "APPROVED"
            and request.failure_code == "RECOMMENDATION_REQUESTED"
        )
        if request is None or (request.status != "REQUESTED" and not approved):
            return False
        region = await session.get(OperatingRegion, request.region_id)
        assert region is not None
        policy = FleetPolicy.model_validate(region.policy["fleet"])
        config = policy.snapshot_configuration(now, region_id=region.region_id, admission_mode=True)
        config = config.model_copy(update={"confirmed_shifts": True})
        head = await session.get(AdmissionSourceHead, region.region_id)
        assert head is not None
        source_version, request_version = head.version, request.version
        sources = await _read_sources(session, config)
        proofs = await current_proofs(session, region.region_id)
        aliases = (
            await session.execute(
                select(CapacityReservation.reservation_id, CapacityReservation.request_id).where(
                    CapacityReservation.region_id == region.region_id,
                    CapacityReservation.request_id.is_not(None),
                )
            )
        ).all()
    ready, _ = regional_ready(OperationalSnapshot(sources, source_digest(sources)), config, now)
    original = next((r for r in sources["requests"] if r["request_id"] == request.request_id), None)
    if original is None:
        await record_failure(
            factory, request, request_version, "REPLACEMENT_REQUIRES_RECOVERY", now
        )
        return False
    stop_aliases = {
        uuid5(NAMESPACE_URL, f"tirodhan:{kind}:{reserved}"): uuid5(
            NAMESPACE_URL, f"tirodhan:{kind}:{booked}"
        )
        for reserved, booked in aliases
        for kind in ("pickup", "discharge")
    }
    lots_by_stop = {
        uuid5(NAMESPACE_URL, f"tirodhan:{kind}:{r['request_id']}"): r["request_id"]
        for r in ready.sources["requests"]
        for kind in ("pickup", "discharge")
    }
    roads = read_cache(factory).roads(providers)
    deadline = time.monotonic() + max_wall_seconds
    best: tuple[tuple[int, int, str], UUID] | None = None
    for vehicle in sorted(ready.sources["vehicles"], key=lambda v: str(v["vehicle_id"])):
        if time.monotonic() >= deadline:
            break
        if vehicle["rider_id"] == request.original_rider_id:
            continue
        if vehicle["resource_kind"] != "COMMITTED" and request.request_id not in vehicle.get(
            "accepted_requests", ()
        ):
            continue
        proof = proofs.get(vehicle["vehicle_id"])
        order = (
            tuple(stop_aliases.get(s, s) for s in proof.order)
            if proof
            else incumbent_orders(ready).get(vehicle["vehicle_id"], ())
        )
        ids = {lots_by_stop[s] for s in order if s in lots_by_stop} | {
            r["request_id"]
            for r in ready.sources["requests"]
            if r["owner_id"] == vehicle["rider_id"] or r["collector_id"] == vehicle["rider_id"]
        }
        local = subset(ready, ids, {vehicle["vehicle_id"]})
        row = dict(original)
        row["owner_id"] = None
        row.pop("replacement_shift_id", None)
        if row["pickup_status"] == "COLLECTED":
            row["collector_id"], row["cargo_vehicle_id"] = (
                vehicle["rider_id"],
                vehicle["vehicle_id"],
            )
        local.sources["requests"] = [
            r for r in local.sources["requests"] if r["request_id"] != row["request_id"]
        ] + [row]
        try:
            problem = await build_problem(local, config, new_uuid7(), roads, now=now)
            stops = {s.stop_id for s in problem.stops}
            order = tuple(
                s for s in order if s in stops and lots_by_stop.get(s) != request.request_id
            )
            seed = RouteProposal(
                problem.problem_id,
                problem.snapshot_id,
                problem.contract_version,
                problem.policy_version,
                SelectionEngine.COMPOSITE,
                "REPLACEMENT_INSERTION_V1",
                (ProposedRoute(vehicle["vehicle_id"], order),),
                (),
            )
            candidates = []
            if original["pickup_status"] != "COLLECTED":
                seed = replace(
                    seed,
                    unserved_stop_ids=tuple(
                        s.stop_id for s in problem.stops if s.lot_id == request.request_id
                    ),
                )
                candidate = insert_obligation(
                    problem,
                    seed,
                    request.request_id,
                    max_evaluations=100_000,
                    max_wall_seconds=max(0, deadline - time.monotonic()),
                )
                if candidate:
                    candidates.append(candidate)
            else:
                discharge = next(s.stop_id for s in problem.stops if s.lot_id == request.request_id)
                protected = len(problem.vehicles[0].protected_prefix)
                for gap in range(protected, len(order) + 1):
                    if time.monotonic() >= deadline:
                        break
                    candidate = replace(
                        seed,
                        routes=(
                            ProposedRoute(
                                vehicle["vehicle_id"], order[:gap] + (discharge,) + order[gap:]
                            ),
                        ),
                    )
                    try:
                        validate_proposal(problem, candidate)
                        candidates.append(candidate)
                    except RoutingValidationError:
                        continue
            if not candidates:
                for engine in OptimizerEngine:
                    if time.monotonic() >= deadline:
                        break
                    try:
                        candidate = await optimizers[engine].propose(
                            problem,
                            search_seconds=min(
                                policy.search_seconds, max(1, int(deadline - time.monotonic()))
                            ),
                        )
                        validate_proposal(problem, candidate)
                        candidates.append(candidate)
                    except (RoutingAdapterError, RoutingValidationError):
                        continue
            for candidate in candidates:
                assessment = validate_proposal(problem, candidate)
                travel = sum(r.travel_seconds for r in assessment.routes)
                rank = (
                    int(problem.vehicles[0].resource_kind != ResourceKind.COMMITTED),
                    travel - (proof.assessment.travel_seconds if proof else 0),
                    str(vehicle["vehicle_id"]),
                )
                if best is None or rank < best[0]:
                    best = rank, vehicle["shift_id"]
        except (OperationalInputError, RoutingAdapterError, RoutingValidationError):
            continue
    if best is None:
        await record_failure(factory, request, request_version, "UNKNOWN_REPLACEMENT_SEARCH", now)
        return False
    async with factory() as session, session.begin():
        # Only advisory metadata is published; approval rechecks the source head under locks.
        row = await session.scalar(
            select(RiderReplacement)
            .where(RiderReplacement.replacement_id == replacement_id)
            .with_for_update()
        )
        if row is None or row.status != request.status or row.version != request_version:
            return False
        if approved:
            head = await session.scalar(
                select(AdmissionSourceHead)
                .where(AdmissionSourceHead.region_id == row.region_id)
                .with_for_update()
            )
            if head is None or head.version != source_version:
                return False
        row.recommended_shift_id, row.source_version = best[1], source_version
        row.status, row.recommended_at, row.failure_code = (
            "APPROVED" if approved else "RECOMMENDED",
            now,
            None,
        )
        row.version += 1
        if approved:
            from .orchestration import wake

            await session.flush()
            await wake(
                session, "REPLACEMENT_RECOMMENDED", now, urgent=True, region_id=row.region_id
            )
    return True


async def record_failure(
    factory: async_sessionmaker[AsyncSession],
    request: RiderReplacement,
    version: int,
    code: str,
    now: datetime,
) -> None:
    async with factory() as session, session.begin():
        row = await session.scalar(
            select(RiderReplacement)
            .where(RiderReplacement.replacement_id == request.replacement_id)
            .with_for_update()
        )
        if row is not None and row.version == version and row.status in ("REQUESTED", "APPROVED"):
            row.failure_code = code if row.status == "REQUESTED" else "RECOMMENDATION_REQUESTED"
            row.attempt += 1
            row.next_attempt_at = now + timedelta(seconds=min(60, 5 * 2 ** min(row.attempt, 4)))
            row.version += 1


async def replacement_targets(
    session: AsyncSession,
    requests: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Approved uncollected replacements constrain normal regional optimization/activation."""
    rows = list(
        await session.scalars(
            select(RiderReplacement).where(
                in_ids(RiderReplacement.request_id, [r["request_id"] for r in requests]),
                RiderReplacement.status == "APPROVED",
                RiderReplacement.transfer_intent_id.is_(None),
            )
        )
    )
    target = {r.request_id: r.recommended_shift_id for r in rows}
    for request in requests:
        if request["request_id"] in target and request["pickup_status"] != "COLLECTED":
            request["replacement_shift_id"] = target[request["request_id"]]
    return [
        {"replacement_id": r.replacement_id, "version": r.version, "target": r.recommended_shift_id}
        for r in rows
    ]
