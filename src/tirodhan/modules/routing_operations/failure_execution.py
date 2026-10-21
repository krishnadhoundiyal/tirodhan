"""Claimed localized recovery with the same authoritative route writer as planning."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from datetime import datetime, timedelta
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import or_, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.planning.models import PickupExecution
from tirodhan.modules.planning.routing.contracts import OptimizerEngine, RouteOptimizer
from tirodhan.modules.planning.routing.execution import RoutingAdapterError
from tirodhan.modules.planning.routing.validation import RoutingValidationError, validate_proposal

from .authority import RouteAuthorityError, commit_routes, lock_booking_population, lock_resources
from .capacity_models import RiderReplacement
from .commands import OperationalInputError, require_live_role
from .dispatch_models import RouteActivation, RouteTask, VehicleRoute
from .failure_models import RecoveryAssignment, RecoveryAssignmentItem, RecoveryWitness
from .models import OperatingShift
from .orchestration_models import CargoTransferIntent, OperatingRegion, RegionVehicle
from .recovery_planning import local_sources, repair
from .replacement_options import check_scope_budget, coordinate, pin_existing
from .rolling import FleetPolicy, incumbent_orders
from .service import PROBLEM_CODEC, PROPOSAL_CODEC
from .snapshot import MatrixSource, OperationalSnapshot, _read_sources, build_problem, source_digest


async def assignment_rows(session: AsyncSession, identity: UUID) -> list[RiderReplacement]:
    return list(
        await session.scalars(
            select(RiderReplacement)
            .join(
                RecoveryAssignmentItem,
                RecoveryAssignmentItem.replacement_id == RiderReplacement.replacement_id,
            )
            .where(
                RecoveryAssignmentItem.assignment_id == identity,
                RecoveryAssignmentItem.released_at.is_(None),
            )
            .order_by(RiderReplacement.replacement_id)
        )
    )


async def claim_activation(
    factory: async_sessionmaker[AsyncSession],
    identity: UUID,
    now: datetime,
    generation: int | None = None,
) -> UUID | None:
    async with factory() as session, session.begin():
        row = await session.scalar(
            select(RecoveryAssignment)
            .where(
                RecoveryAssignment.assignment_id == identity,
                RecoveryAssignment.due_at <= now,
                RecoveryAssignment.status.in_(("PENDING", "RUNNING", "AWAITING_TRANSFER")),
                or_(
                    RecoveryAssignment.lease_until.is_(None), RecoveryAssignment.lease_until <= now
                ),
            )
            .with_for_update(skip_locked=True)
        )
        if row is None:
            if generation is not None:
                from .recovery_work import current_work

                pending = await current_work(session, identity, "ACTIVATE")
                if pending and pending.generation == generation:
                    raise RuntimeError("RECOVERY_WORK_NOT_CLAIMABLE")
            return None
        from .recovery_work import begin_work

        if not await begin_work(session, identity, "ACTIVATE", now, generation):
            return None
        token = new_uuid7()
        row.lease_token, row.lease_until = token, now + timedelta(seconds=120)
        row.status = "RUNNING"
        row.attempt += 1
        row.version += 1
        return token


async def finish_attempt(
    factory: async_sessionmaker[AsyncSession],
    identity: UUID,
    token: UUID,
    now: datetime,
    code: str,
    *,
    attention: bool = False,
) -> None:
    async with factory() as session, session.begin():
        row = await session.get(RecoveryAssignment, identity, with_for_update=True)
        if row and row.lease_token == token and row.lease_until and row.lease_until > now:
            row.status = (
                "ATTENTION"
                if attention
                else "AWAITING_TRANSFER"
                if code == "AWAITING_PHYSICAL_TRANSFER"
                else "PENDING"
            )
            row.failure_code = code
            row.lease_token = row.lease_until = None
            row.due_at = now + timedelta(seconds=min(300, 5 * 2 ** min(row.attempt, 6)))
            row.version += 1
            from .recovery_work import finish_work

            await finish_work(session, row, now)


async def resolve_terminal_obligations(
    factory: async_sessionmaker[AsyncSession], identity: UUID, token: UUID, now: datetime
) -> bool:

    async with factory() as session, session.begin():
        rows = await assignment_rows(session, identity)
        requests = await lock_booking_population(session, [r.request_id for r in rows])
        assignment = await session.get(RecoveryAssignment, identity, with_for_update=True)
        if not assignment or assignment.lease_token != token:
            raise RouteAuthorityError("RECOVERY_LEASE_LOST")
        cancelled = {key for key, r in requests.items() if r.status in ("CANCELLED", "COMPLETED")}
        for row in rows:
            if row.request_id not in cancelled:
                continue
            pickup = await session.scalar(
                select(PickupExecution).where(PickupExecution.request_id == row.request_id)
            )
            if (
                pickup
                and pickup.status == "COLLECTED"
                and requests[row.request_id].status == "CANCELLED"
            ):
                raise RouteAuthorityError("CANCELLED_CUSTODY_REQUIRES_ATTENTION")
            row.status = "REJECTED"
            row.failure_code = (
                "BOOKING_CANCELLED"
                if requests[row.request_id].status == "CANCELLED"
                else "BOOKING_COMPLETED"
            )
            row.version += 1
            item = await session.get(RecoveryAssignmentItem, (identity, row.replacement_id))
            assert item
            item.released_at = now
        if rows and len(cancelled) == len(rows):
            assignment.status = "SUPERSEDED"
            assignment.failure_code = "TERMINAL_BOOKING_OUTCOME"
            assignment.lease_token = assignment.lease_until = None
            assignment.version += 1
            from .recovery_work import finish_work

            await finish_work(session, assignment, now)
            return True
        return False


async def activate_recovery(
    factory: async_sessionmaker[AsyncSession],
    identity: UUID,
    *,
    providers: dict[str, MatrixSource],
    optimizers: dict[OptimizerEngine, RouteOptimizer],
    now: datetime | None = None,
    generation: int | None = None,
) -> UUID | None:
    now = now or utc_now()
    started = time.monotonic()
    token = await claim_activation(factory, identity, now, generation)
    if token is None:
        async with factory() as session:
            existing = await session.get(RecoveryAssignment, identity)
            return existing.activation_id if existing and existing.status == "ACTIVATED" else None
    try:
        if await resolve_terminal_obligations(factory, identity, token, now):
            return None
        async with factory() as session:
            await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            assignment = await session.get(RecoveryAssignment, identity)
            assert assignment
            rows = await assignment_rows(session, identity)
            if not rows:
                raise RouteAuthorityError("RECOVERY_OBLIGATION_CHANGED")
            shift = await session.get(OperatingShift, assignment.shift_id)
            if shift is None:
                raise RouteAuthorityError("ACTIVE_SHIFT_REQUIRED")
            from .regional_projection import effective_shift_end

            if shift.closed_at or not shift.starts_at <= now < await effective_shift_end(
                session, shift.shift_id, shift.ends_at
            ):
                raise RouteAuthorityError("ACTIVE_SHIFT_REQUIRED")
            for row in rows:
                pickup = await session.scalar(
                    select(PickupExecution).where(PickupExecution.request_id == row.request_id)
                )
                if pickup and pickup.status == "COLLECTED":
                    intent = (
                        await session.get(CargoTransferIntent, row.transfer_intent_id)
                        if row.transfer_intent_id
                        else None
                    )
                    if intent is not None and intent.resolution == "ABORTED":
                        raise RouteAuthorityError("PHYSICAL_TRANSFER_ABORTED")
                    if intent is None or intent.resolution != "TRANSFERRED":
                        if intent is None:
                            from .operational_commands import execute_operational
                            from .replacement_commands import PrepareReplacementTransfer

                            await execute_operational(
                                factory,
                                actor_id=assignment.approved_by,
                                command=PrepareReplacementTransfer(
                                    client_command_id=uuid5(
                                        NAMESPACE_URL,
                                        f"recovery-transfer:{identity}:{row.replacement_id}:{row.version}",
                                    ),
                                    replacement_id=row.replacement_id,
                                    expected_version=row.version,
                                ),
                                expires_at=now + timedelta(days=1),
                                now=now,
                            )
                        await finish_attempt(
                            factory, identity, token, now, "AWAITING_PHYSICAL_TRANSFER"
                        )
                        return None
            region = await session.get(OperatingRegion, rows[0].region_id)
            assert region
            policy = FleetPolicy.model_validate(region.policy["fleet"])
            config = policy.snapshot_configuration(
                now, region_id=region.region_id, admission_mode=True
            ).model_copy(update={"confirmed_shifts": True})
            sources = await _read_sources(session, config)
            rescued = {r.request_id for r in rows}
            scoped = await local_sources(session, sources, shift.vehicle_id, rescued)
            if not rescued.issubset({r["request_id"] for r in scoped["requests"]}):
                raise RouteAuthorityError("RECOVERY_OBLIGATION_CHANGED")
            stamp = source_digest(scoped)
        target_state = scoped["vehicles"][0]
        if target_state.get("transfer_in_progress"):
            raise RouteAuthorityError("PHYSICAL_TRANSFER_IN_PROGRESS")
        if target_state["blocked_reason"] is not None or (
            any(r["status"] == "RECOVERY_REQUIRED" for r in scoped["routes"])
            and not target_state.get("resume_authorized")
        ):
            raise RouteAuthorityError("CONTROLLED_RECOVERY_REQUIRED")
        if target_state.get("legacy_assignment"):
            raise RouteAuthorityError("LEGACY_ASSIGNMENT_MUST_DRAIN")
        if target_state["resource_kind"] != "COMMITTED" and not rescued.issubset(
            set(target_state.get("accepted_requests", ()))
        ):
            raise RouteAuthorityError("VOLUNTARY_ACCEPTANCE_REQUIRED")
        projected = dict(scoped)
        projected["requests"] = [dict(r) for r in scoped["requests"]]
        for request in projected["requests"]:
            if request["request_id"] in rescued and request["pickup_status"] != "COLLECTED":
                request["owner_id"] = None
        check_scope_budget(projected, policy)
        problem = await asyncio.wait_for(
            build_problem(
                OperationalSnapshot(projected, stamp), config, new_uuid7(), providers, now=now
            ),
            30,
        )
        problem = pin_existing(problem, projected, rescued, shift.vehicle_id)
        try:
            proposal = repair(problem, projected, rescued, policy)
        except RoutingValidationError:
            proposal = None
            for engine in OptimizerEngine:
                try:
                    trial = await asyncio.wait_for(
                        optimizers[engine].propose(
                            problem, search_seconds=min(policy.search_seconds, 10)
                        ),
                        12,
                    )
                    validate_proposal(problem, trial)
                    coordinate(
                        problem,
                        trial,
                        projected,
                        policy,
                        incumbent_orders(OperationalSnapshot(projected, stamp)),
                    )
                    proposal = trial
                    break
                except (RoutingValidationError, RoutingAdapterError, asyncio.TimeoutError):
                    continue
            if proposal is None:
                raise RouteAuthorityError("UNKNOWN_RECOVERY_CONTINUATION") from None
        assert proposal is not None
        live_now = now + timedelta(seconds=time.monotonic() - started)
        async with factory() as session:
            await session.connection(execution_options={"isolation_level": "SERIALIZABLE"})
            await require_live_role(session, assignment.approved_by, "MANAGER")
            requests = await lock_booking_population(
                session, [r["request_id"] for r in scoped["requests"] if not r.get("hold")]
            )
            donor_roots = list(
                await session.scalars(
                    select(VehicleRoute)
                    .join(OperatingShift)
                    .where(
                        OperatingShift.rider_id.in_([r.original_rider_id for r in rows]),
                        VehicleRoute.status != "CLOSED",
                    )
                )
            )
            vehicles = {shift.vehicle_id} | {r.vehicle_id for r in donor_roots}
            await lock_resources(
                session,
                sorted({shift.rider_id} | {r.original_rider_id for r in rows}, key=str),
                sorted(vehicles, key=str),
                authorize=True,
                authorize_rider_ids=[shift.rider_id],
            )
            await session.execute(
                select(RiderReplacement)
                .where(RiderReplacement.request_id.in_(requests))
                .order_by(RiderReplacement.replacement_id)
                .with_for_update()
            )
            from tirodhan.modules.receiving_points.models import ReceivingPoint

            await session.execute(
                select(ReceivingPoint)
                .where(
                    ReceivingPoint.receiving_point_id.in_(
                        [p["receiving_point_id"] for p in scoped["points"]]
                    )
                )
                .order_by(ReceivingPoint.receiving_point_id)
                .with_for_update(key_share=True)
            )
            current_assignment = await session.get(
                RecoveryAssignment, identity, with_for_update=True
            )
            # Payment, resource and facility locks may have waited. Feasibility,
            # shift eligibility and position freshness must use the post-lock clock.
            live_now = now + timedelta(seconds=time.monotonic() - started)
            if (
                not current_assignment
                or current_assignment.lease_token != token
                or not current_assignment.lease_until
                or current_assignment.lease_until <= live_now
            ):
                raise RouteAuthorityError("RECOVERY_LEASE_LOST")
            claim_expires_at = current_assignment.lease_until
            current_rows = await assignment_rows(session, identity)
            if {r.replacement_id for r in current_rows} != {r.replacement_id for r in rows} or any(
                r.status != "APPROVED" or r.recommended_shift_id != shift.shift_id
                for r in current_rows
            ):
                raise RouteAuthorityError("RECOVERY_ALLOCATION_CHANGED")
            shift = await session.get(OperatingShift, assignment.shift_id)
            assert shift is not None
            from .regional_projection import effective_shift_end

            await require_live_role(session, shift.rider_id, "RIDER")
            if shift.closed_at or not shift.starts_at <= live_now < await effective_shift_end(
                session, shift.shift_id, shift.ends_at
            ):
                raise RouteAuthorityError("ACTIVE_SHIFT_REQUIRED")
            membership = await session.get(RegionVehicle, shift.vehicle_id, with_for_update=True)
            if not membership or membership.region_id != region.region_id:
                raise RouteAuthorityError("REGIONAL_OWNER_CHANGED")
            current = await _read_sources(session, config)
            actual = await local_sources(session, current, shift.vehicle_id, rescued)
            if source_digest(actual) != stamp:
                raise RouteAuthorityError("SOURCE_CHANGED")
            target = actual["vehicles"][0]
            if (
                target["blocked_reason"] is not None
                or not target.get("shift_confirmed")
                or not 0
                <= (live_now - target["observed_at"]).total_seconds()
                <= policy.telemetry.max_age_seconds
            ):
                raise RouteAuthorityError("RESOURCE_AUTHORIZATION_STALE")
            elapsed = max(0, int((live_now - problem.epoch).total_seconds() + 1))
            problem = replace(
                problem,
                vehicles=tuple(
                    replace(v, available_at=max(v.available_at, elapsed)) for v in problem.vehicles
                ),
            )
            validate_proposal(problem, proposal)
            from tirodhan.modules.planning.routing.facilities import validate_sources

            validate_sources(problem, validate_proposal(problem, proposal), actual)
            # Approval authorizes release, but ownership changes only with successor activation.
            from .execution import finish_if_empty
            from .recovery import _release

            for row in current_rows:
                pickup = await session.scalar(
                    select(PickupExecution).where(PickupExecution.request_id == row.request_id)
                )
                if pickup and pickup.status != "COLLECTED":
                    for root in donor_roots:
                        tasks = list(
                            await session.scalars(
                                select(RouteTask).where(
                                    RouteTask.route_id == root.route_id,
                                    RouteTask.pickup_execution_id == pickup.pickup_execution_id,
                                    RouteTask.state.in_(("PENDING", "STARTED")),
                                )
                            )
                        )
                        if any(t.state == "STARTED" or t.committed_at is not None for t in tasks):
                            raise RouteAuthorityError("ACTIVE_SERVICE_REQUIRES_RESOLUTION")
                        if tasks:
                            await _release(session, root, pickup, live_now, "REASSIGNED")
                            await session.flush()
                            await finish_if_empty(session, root, live_now)
            witness = RecoveryWitness(
                witness_id=new_uuid7(),
                assignment_id=identity,
                source_fingerprint=stamp,
                configuration=config.model_dump(mode="json"),
                problem=PROBLEM_CODEC.dump_python(problem, mode="json"),
                proposal=PROPOSAL_CODEC.dump_python(proposal, mode="json"),
                created_at=live_now,
            )
            session.add(witness)
            await session.flush()
            activation = RouteActivation(
                run_id=None,
                engine=None,
                recovery_witness_id=witness.witness_id,
                activated_by=assignment.approved_by,
                created_at=live_now,
            )
            await commit_routes(
                session,
                actor=assignment.approved_by,
                activation=activation,
                config=config,
                problem=problem,
                proposal=proposal,
                hold_ids=set(),
                requests=requests,
                current=actual,
                now=live_now,
                resume_recovery=True,
                delivery_policy=policy.model_dump(mode="json"),
            )
            from .orchestration import wake

            await wake(
                session, "RECOVERY_ACTIVATED", live_now, urgent=True, region_id=region.region_id
            )
            # A lock wait during persistence cannot extend an expired worker token.
            if now + timedelta(seconds=time.monotonic() - started) >= claim_expires_at:
                raise RouteAuthorityError("RECOVERY_LEASE_LOST")
            await session.commit()
            return activation.activation_id
    except DBAPIError as error:
        if getattr(error.orig, "sqlstate", None) not in {"40001", "40P01"}:
            raise
        await finish_attempt(
            factory,
            identity,
            token,
            now + timedelta(seconds=time.monotonic() - started),
            "RETRYABLE_TRANSACTION_CONFLICT",
        )
        return None
    except (
        OperationalInputError,
        RoutingValidationError,
        RoutingAdapterError,
        asyncio.TimeoutError,
        KeyError,
        StopIteration,
    ) as error:
        code = (
            str(error)
            if isinstance(error, OperationalInputError)
            else "UNKNOWN_RECOVERY_INPUT_OR_PROVIDER"
        )
        await finish_attempt(
            factory,
            identity,
            token,
            now + timedelta(seconds=time.monotonic() - started),
            code,
            attention=code
            in {
                "CONTROLLED_RECOVERY_REQUIRED",
                "VOLUNTARY_ACCEPTANCE_REQUIRED",
                "LEGACY_ASSIGNMENT_MUST_DRAIN",
                "ACTIVE_SHIFT_REQUIRED",
                "RESOURCE_AUTHORIZATION_STALE",
                "ACTIVE_SERVICE_REQUIRES_RESOLUTION",
                "PHYSICAL_TRANSFER_ABORTED",
                "REGIONAL_OWNER_CHANGED",
                "RECOVERY_ALLOCATION_CHANGED",
            },
        )
        return None
