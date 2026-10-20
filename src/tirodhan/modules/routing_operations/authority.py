from __future__ import annotations

import hashlib
import math
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.dispatch.models import (
    RiderAssignment,
    RiderAssignmentItem,
    RiderAvailability,
    RiderProfile,
)
from tirodhan.modules.identity.authorization import lock_active_user_role
from tirodhan.modules.payments.models import Payment
from tirodhan.modules.planning.locking import (
    acquire_work_unit_advisory_lock,
    work_unit_advisory_lock_key,
)
from tirodhan.modules.planning.models import PickupExecution
from tirodhan.modules.planning.routing.adapters import ORTOOLS_VERSION, VROOM_VERSION
from tirodhan.modules.planning.routing.contracts import (
    OptimizerEngine,
    ResourceKind,
    SelectionEngine,
)
from tirodhan.modules.planning.routing.validation import validate_proposal
from tirodhan.modules.reliability.primitives import (
    append_outbox_event,
    claim_idempotency_record,
    complete_idempotency_record,
)

from .commands import OperationalInputError, require_live_role
from .dispatch_models import (
    RouteActivation,
    RouteRevision,
    RouteRevisionStop,
    RouteTask,
    VehicleRoute,
)
from .models import OperatingVehicle, RoutingShadowRun
from .service import PROBLEM_CODEC, PROPOSAL_CODEC
from .snapshot import ShadowConfiguration, _read_sources, source_digest


class RouteAuthorityError(OperationalInputError):
    pass


class ActivationCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    client_command_id: UUID
    run_id: UUID
    engine: OptimizerEngine | SelectionEngine
    resume_recovery: bool = False
    fleet_cycle_id: UUID | None = None


async def lock_booking_population(
    session: AsyncSession, request_ids: list[UUID]
) -> dict[UUID, CollectionRequest]:
    """Payment-first; preserve the cancellation/freeze partial order across all requests."""
    payments = list(
        await session.scalars(
            select(Payment)
            .where(Payment.request_id.in_(request_ids))
            .order_by(Payment.payment_id)
            .with_for_update()
        )
    )
    collected = set(
        await session.scalars(
            select(PickupExecution.request_id).where(
                PickupExecution.request_id.in_(request_ids),
                PickupExecution.status == "COLLECTED",
            )
        )
    )
    if set(request_ids) - collected - {p.request_id for p in payments}:
        raise RouteAuthorityError("PAYMENT_REQUIRED")
    requests = list(
        await session.scalars(
            select(CollectionRequest)
            .where(CollectionRequest.request_id.in_(request_ids))
            .order_by(CollectionRequest.request_id)
        )
    )
    for r in sorted(
        requests, key=lambda r: work_unit_advisory_lock_key(r.cell_id, r.slot_start, r.slot_end)
    ):
        await acquire_work_unit_advisory_lock(
            session, cell_id=r.cell_id, slot_start=r.slot_start, slot_end=r.slot_end
        )
    locked = list(
        await session.scalars(
            select(CollectionRequest)
            .where(CollectionRequest.request_id.in_(request_ids))
            .order_by(CollectionRequest.request_id)
            .with_for_update(key_share=True)
            .execution_options(populate_existing=True)
        )
    )
    for p in payments:
        if p.request_id in collected:
            continue
        r = next(r for r in locked if r.request_id == p.request_id)
        if (
            p.status != "SUCCEEDED"
            or p.amount_minor != r.quoted_amount_minor
            or p.currency != r.currency
        ):
            raise RouteAuthorityError("BOOKING_PAYMENT_NOT_CONFIRMED")
    return {r.request_id: r for r in locked}


async def lock_resources(
    session: AsyncSession,
    rider_ids: list[UUID],
    vehicle_ids: list[UUID],
    *,
    authorize: bool = True,
) -> None:
    if authorize:
        profiles = list(
            await session.scalars(
                select(RiderProfile)
                .where(RiderProfile.rider_id.in_(rider_ids))
                .order_by(RiderProfile.rider_id)
                .with_for_update(key_share=True)
                .execution_options(populate_existing=True)
            )
        )
        if len(profiles) != len(set(rider_ids)) or any(p.status != "ACTIVE" for p in profiles):
            raise RouteAuthorityError("RESOURCE_AUTHORIZATION_STALE")
    availability = list(
        await session.scalars(
            select(RiderAvailability)
            .where(RiderAvailability.rider_id.in_(rider_ids))
            .order_by(RiderAvailability.rider_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    if len(availability) != len(set(rider_ids)):
        raise RouteAuthorityError("RIDER_AVAILABILITY_REQUIRED")
    # V1 profile -> availability -> authority order; FK-compatible locks avoid recorder cycles.
    for rider_id in sorted(set(rider_ids), key=str):
        if authorize and not await lock_active_user_role(
            session, user_id=rider_id, role_code="RIDER", no_key_update=True
        ):
            raise RouteAuthorityError("RESOURCE_AUTHORIZATION_STALE")
    vehicles = list(
        await session.scalars(
            select(OperatingVehicle)
            .where(OperatingVehicle.vehicle_id.in_(vehicle_ids))
            .order_by(OperatingVehicle.vehicle_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    if len(vehicles) != len(set(vehicle_ids)):
        raise RouteAuthorityError("VEHICLE_NOT_FOUND")


async def activate_proposal(
    factory: async_sessionmaker[AsyncSession],
    *,
    manager_id: UUID,
    command: ActivationCommand,
    expires_at: datetime,
    now: datetime | None = None,
    clock: Callable[[], datetime] | None = None,
) -> UUID:
    """No external I/O. SERIALIZABLE covers source/predicate drift; locks arbitrate ownership."""
    try:
        async with factory() as session:
            await session.connection(execution_options={"isolation_level": "SERIALIZABLE"})
            result = await _activate(
                session,
                manager_id,
                command,
                expires_at,
                now or utc_now(),
                clock or (utc_now if now is None else None),
            )
            await session.commit()
            return result
    except DBAPIError as error:
        if getattr(error.orig, "sqlstate", None) in {"40001", "40P01"}:
            raise RouteAuthorityError("RETRYABLE_TRANSACTION_CONFLICT") from None
        raise


async def _activate(
    session: AsyncSession,
    actor: UUID,
    command: ActivationCommand,
    expires_at: datetime,
    now: datetime,
    clock: Callable[[], datetime] | None = None,
) -> UUID:
    await require_live_role(session, actor, "MANAGER")
    from tirodhan.modules.business_admin.history import attribute_change

    await attribute_change(session, actor, command.client_command_id, "ROUTE_ACTIVATION")
    claim = await claim_idempotency_record(
        session,
        scope=f"routing.activate:{actor}",
        idempotency_key=str(command.client_command_id),
        request_fingerprint=hashlib.sha256(command.model_dump_json().encode()).digest(),
        expires_at=expires_at,
        now=now,
    )
    if claim.record.status == "COMPLETED":
        assert claim.record.result_resource_id is not None
        return claim.record.result_resource_id
    if not claim.created:
        raise RouteAuthorityError("COMMAND_IN_PROGRESS")
    run = await session.get(RoutingShadowRun, command.run_id)
    if run is None or run.problem is None or run.results is None:
        raise RouteAuthorityError("VALIDATED_RUN_REQUIRED")
    if run.status != "SUCCEEDED" and not (
        run.status == "FAILED" and run.failure_code == "ENGINE_EVALUATION_FAILED"
    ):
        raise RouteAuthorityError("VALIDATED_RUN_REQUIRED")
    config = ShadowConfiguration.model_validate(run.configuration)
    if command.engine != SelectionEngine.COMPOSITE:
        from .fleet_models import FleetControl
        from .orchestration_models import RegionVehicle

        if await session.scalar(
            select(FleetControl.scope).where(
                FleetControl.region_id.in_(select(RegionVehicle.region_id))
            )
        ):
            raise RouteAuthorityError("REGIONAL_OWNER_REQUIRED")
    problem = PROBLEM_CODEC.validate_python(run.problem)
    result = run.results.get(command.engine.value)
    if result is None or "proposal" not in result or "failure_code" in result:
        raise RouteAuthorityError("VALIDATED_ENGINE_RESULT_REQUIRED")
    proposal = PROPOSAL_CODEC.validate_python(result["proposal"])
    from .parallel import VERSION, Selection, selected_routes

    versions = {
        OptimizerEngine.VROOM: VROOM_VERSION,
        OptimizerEngine.OR_TOOLS: ORTOOLS_VERSION,
        SelectionEngine.COMPOSITE: VERSION,
    }
    if proposal.engine != command.engine or proposal.engine_version != versions[command.engine]:
        raise RouteAuthorityError("ENGINE_IDENTITY_MISMATCH")
    if command.engine == SelectionEngine.COMPOSITE:
        selection = Selection.model_validate(result.get("selection"))
        if (
            selection.policy_version != problem.policy_version
            or selected_routes(selection) != proposal.routes
            or not config.confirmed_shifts
            or config.region_id is None
            or command.fleet_cycle_id is None
        ):
            raise RouteAuthorityError("COMPOSITE_SELECTION_MISMATCH")
    from .orchestration_models import CapacityReservation

    hold_ids = set(
        await session.scalars(
            select(CapacityReservation.reservation_id).where(
                CapacityReservation.reservation_id.in_([lot.lot_id for lot in problem.lots])
            )
        )
    )
    requests = await lock_booking_population(
        session, [lot.collection_request_id for lot in problem.lots if lot.lot_id not in hold_ids]
    )
    await lock_resources(
        session, [v.rider_id for v in problem.vehicles], [v.vehicle_id for v in problem.vehicles]
    )
    from .admission_queries import in_ids
    from .capacity_models import RiderReplacement

    await session.execute(
        select(RiderReplacement)
        .where(in_ids(RiderReplacement.request_id, list(requests)))
        .order_by(RiderReplacement.replacement_id)
        .with_for_update()
    )
    from tirodhan.modules.receiving_points.models import ReceivingPoint

    await session.execute(
        select(ReceivingPoint)
        .where(
            ReceivingPoint.receiving_point_id.in_(
                [p.receiving_point_id for p in problem.receiving_points]
            )
        )
        .order_by(ReceivingPoint.receiving_point_id)
        .with_for_update(key_share=True)
    )
    current = await _read_sources(session, config)
    if clock is not None:
        now = clock()  # Lock waits never authorize departure using an old captured instant.
    import json

    digest = hashlib.sha256(
        json.dumps(current, sort_keys=True, default=str, separators=(",", ":")).encode()
    ).digest()
    if config.fleet_mode:
        digest = source_digest(
            current, position_vehicle_ids={v.vehicle_id for v in problem.vehicles}
        )
    if digest != run.source_fingerprint:
        raise RouteAuthorityError("SOURCE_CHANGED")
    if config.fleet_mode and command.fleet_cycle_id is None:
        raise RouteAuthorityError("FLEET_OWNER_REQUIRED")
    if now >= config.horizon_end:
        raise RouteAuthorityError("HORIZON_EXPIRED")
    held_stops = {s.stop_id for s in problem.stops if s.lot_id in hold_ids}
    used_vehicles = {
        route.vehicle_id
        for route in proposal.routes
        if any(s not in held_stops for s in route.stop_ids)
    }
    for v in current["vehicles"]:
        if config.fleet_mode and v["vehicle_id"] not in {
            item.vehicle_id for item in problem.vehicles
        }:
            continue
        if v["vehicle_id"] in used_vehicles and v["blocked_reason"] is not None:
            raise RouteAuthorityError("VEHICLE_OPERATION_BLOCKED")
        if v["vehicle_id"] in used_vehicles and (
            not v["starts_at"] <= now < v["ends_at"]
            or (
                v["resource_kind"] != "COMMITTED"
                and not (config.regional and v.get("accepted_requests"))
            )
        ):
            raise RouteAuthorityError("ACTIVE_COMMITTED_SHIFT_REQUIRED")
        if (
            config.confirmed_shifts
            and v["vehicle_id"] in used_vehicles
            and v["resource_kind"] == "COMMITTED"
            and not v.get("shift_confirmed")
        ):
            raise RouteAuthorityError("CONFIRMED_ACTIVE_SHIFT_REQUIRED")
        if not 0 <= (now - v["observed_at"]).total_seconds() <= config.max_position_age_seconds:
            raise RouteAuthorityError("FRESH_POSITION_REQUIRED")
    # Revalidate from activation time, never use an old predicted departure as live authority.
    elapsed = math.ceil((now - problem.epoch).total_seconds())
    live_problem = replace(
        problem,
        vehicles=tuple(
            replace(v, available_at=max(v.available_at, elapsed)) for v in problem.vehicles
        ),
    )
    assessment = validate_proposal(live_problem, proposal)
    if command.engine == SelectionEngine.COMPOSITE:
        from tirodhan.modules.planning.routing.facilities import validate_sources

        validate_sources(live_problem, assessment, current)
    if any(
        v.resource_kind != ResourceKind.COMMITTED
        and not any(
            r["vehicle_id"] == v.vehicle_id and r.get("accepted_requests")
            for r in current["vehicles"]
        )
        for v in problem.vehicles
    ):
        raise RouteAuthorityError("COMMITTED_ONLY")
    legacy = await session.scalar(
        select(RiderAssignment.assignment_id)
        .where(
            RiderAssignment.rider_id.in_([v.rider_id for v in problem.vehicles]),
            RiderAssignment.status == "ACTIVE",
            RiderAssignment.source != "COMMITTED_DISPATCH",
        )
        .limit(1)
    )
    if legacy is not None:
        raise RouteAuthorityError("LEGACY_ASSIGNMENT_MUST_DRAIN")
    if any(r.status == "PRE_PLANNING" for r in requests.values()):
        raise RouteAuthorityError("LEGACY_BATCH_MUST_DRAIN")
    if command.fleet_cycle_id is not None:
        from .fleet_models import FleetControl, FleetCycle

        cycle = await session.get(FleetCycle, command.fleet_cycle_id)
        control = await session.scalar(
            select(FleetControl)
            .where(FleetControl.scope == (cycle.scope if cycle else "COMMITTED_FLEET"))
            .with_for_update()
        )
        if (
            cycle is None
            or control is None
            or cycle.run_id != run.run_id
            or cycle.status != "RUNNING"
            or cycle.requested_by != actor
            or cycle.selected_engine != command.engine.value
            or control.manager_id != actor
            or control.version != cycle.control_version
            or control.lease_token != cycle.lease_token
            or control.lease_until is None
            or control.lease_until <= now
        ):
            raise RouteAuthorityError("FLEET_LEASE_LOST")
        from tirodhan.modules.planning.routing.validation import RoutingValidationError

        from .economics import EconomicPolicy, measure
        from .rolling import incumbent_orders
        from .snapshot import OperationalSnapshot

        assert cycle.measurements is not None
        economics = EconomicPolicy.model_validate(cycle.measurements["policy"]["economics"])
        orders = incumbent_orders(OperationalSnapshot(current, digest))
        if command.engine == SelectionEngine.COMPOSITE:
            from .parallel import verify_selection

            verify_selection(
                problem,
                selection,
                economics,
                orders,
                tuple(
                    (
                        PROBLEM_CODEC.validate_python(item["problem"]),
                        Selection.model_validate(item["selection"]),
                    )
                    for item in result.get("rebase_history", [])
                ),
            )
        ranked = []
        for engine, candidate_result in (
            run.results.items() if command.engine != SelectionEngine.COMPOSITE else ()
        ):
            if "proposal" not in candidate_result or "failure_code" in candidate_result:
                continue
            candidate = PROPOSAL_CODEC.validate_python(candidate_result["proposal"])
            if (
                candidate.engine.value != engine
                or candidate.engine_version != versions[candidate.engine]
            ):
                raise RouteAuthorityError("ENGINE_IDENTITY_MISMATCH")
            try:
                cost = measure(live_problem, candidate, economics, orders)
                ranked.append((cost.rank, engine))
            except RoutingValidationError:
                continue
        if command.engine != SelectionEngine.COMPOSITE and (
            not ranked or min(ranked)[1] != command.engine.value
        ):
            raise RouteAuthorityError("FLEET_SELECTION_CHANGED_REBASE_REQUIRED")
    activation = RouteActivation(
        run_id=run.run_id, engine=command.engine.value, activated_by=actor, created_at=now
    )
    session.add(activation)
    await session.flush([activation])
    from .hold_projection import executable_projection

    actual_problem, proposal = executable_projection(live_problem, proposal, hold_ids)
    # Removing a passive visit changes road arcs. Check the route we actually authorize.
    assessment = validate_proposal(actual_problem, proposal)
    if command.engine == SelectionEngine.COMPOSITE:
        validate_sources(actual_problem, assessment, current)
    by_stop = {s.stop_id: s for s in actual_problem.stops}
    by_vehicle = {v.vehicle_id: v for v in actual_problem.vehicles}
    schedules = {r.vehicle_id: r for r in assessment.routes}
    if config.fleet_mode:
        # Transfer future work before inserting successor ownership, regardless of UUID order.
        # Started/committed legs and collected custody were bound by the validator above.
        destination = {
            by_stop[s].lot_id: route.vehicle_id
            for route in proposal.routes
            for s in route.stop_ids
            if by_stop[s].kind.value == "PICKUP"
        }
        for request_id, vehicle_id in destination.items():
            pickup = await session.scalar(
                select(PickupExecution).where(PickupExecution.request_id == request_id)
            )
            if pickup is None:
                continue
            old = await session.scalar(
                select(RiderAssignmentItem).where(
                    RiderAssignmentItem.pickup_execution_id == pickup.pickup_execution_id,
                    RiderAssignmentItem.released_at.is_(None),
                )
            )
            if old is None:
                continue
            old_root = await session.scalar(
                select(VehicleRoute).where(
                    VehicleRoute.assignment_id == old.assignment_id, VehicleRoute.status != "CLOSED"
                )
            )
            if old_root is None or old_root.vehicle_id == vehicle_id:
                continue
            tasks = list(
                await session.scalars(
                    select(RouteTask).where(
                        RouteTask.route_id == old_root.route_id,
                        RouteTask.pickup_execution_id == pickup.pickup_execution_id,
                        RouteTask.state.in_(("PENDING", "STARTED")),
                    )
                )
            )
            if pickup.status == "COLLECTED" or any(
                t.state != "PENDING" or t.committed_at for t in tasks
            ):
                raise RouteAuthorityError("PROTECTED_OWNERSHIP_TRANSFER")
            old.released_at, old.release_reason_code = now, "REASSIGNED"
            for old_task in tasks:
                old_task.state = "CANCELLED"
        await session.flush()
        # Empty donor routes close conventionally; custody routes can never disappear here.
        from .execution import finish_if_empty

        for route_data in current["routes"]:
            if route_data["vehicle_id"] not in by_vehicle:
                continue
            if not any(
                r.vehicle_id == route_data["vehicle_id"] and r.stop_ids for r in proposal.routes
            ):
                old_root = await session.get(VehicleRoute, route_data["route_id"])
                assert old_root is not None
                await finish_if_empty(session, old_root, now)
    for proposed in sorted(proposal.routes, key=lambda r: str(r.vehicle_id)):
        if not proposed.stop_ids:
            continue
        v = by_vehicle[proposed.vehicle_id]
        root = await session.scalar(
            select(VehicleRoute)
            .where(
                VehicleRoute.vehicle_id == v.vehicle_id,
                VehicleRoute.status != "CLOSED",
            )
            .with_for_update()
        )
        if root is not None:
            if root.shift_id != v.operating_shift_id:
                raise RouteAuthorityError("ROUTE_SHIFT_MISMATCH")
            if root.status == "RECOVERY_REQUIRED" and not command.resume_recovery:
                raise RouteAuthorityError("EXPLICIT_RECOVERY_RESUME_REQUIRED")
            assignment = await session.get(RiderAssignment, root.assignment_id)
            assert assignment is not None
            if config.fleet_mode and root.status == "ACTIVE":
                from .execution import current_tasks

                if (
                    tuple(t.stop_id for t in await current_tasks(session, root))
                    == proposed.stop_ids
                ):
                    continue  # Unchanged routes retain their revision and in-flight command fence.
            previous = await session.scalar(
                select(RouteRevision.revision_id).where(
                    RouteRevision.route_id == root.route_id,
                    RouteRevision.version == root.version,
                )
            )
            root.version += 1
            root.status = "ACTIVE"
        else:
            assignment = RiderAssignment(
                assignment_id=new_uuid7(),
                collection_group_id=None,
                rider_id=v.rider_id,
                source="COMMITTED_DISPATCH",
                status="ACTIVE",
                assigned_by_user_id=actor,
                created_at=now,
                assigned_at=now,
                started_at=now,
            )
            session.add(assignment)
            await session.flush([assignment])
            root = VehicleRoute(
                vehicle_id=v.vehicle_id,
                shift_id=v.operating_shift_id,
                assignment_id=assignment.assignment_id,
                version=1,
                status="ACTIVE",
                created_at=now,
            )
            session.add(root)
            await session.flush([root])
            previous = None
        revision = RouteRevision(
            route_id=root.route_id,
            activation_id=activation.activation_id,
            version=root.version,
            supersedes_revision_id=previous,
            created_at=now,
        )
        session.add(revision)
        await session.flush([revision])
        for ordinal, stop_id in enumerate(proposed.stop_ids):
            stop = by_stop[stop_id]
            request = requests[stop.lot_id]
            pickup = await session.scalar(
                select(PickupExecution)
                .where(PickupExecution.request_id == request.request_id)
                .with_for_update()
            )
            if request.status not in ("ACCEPTED", "PLANNED") and (
                pickup is None or pickup.status != "COLLECTED"
            ):
                raise RouteAuthorityError("BOOKING_NOT_EXECUTABLE")
            if pickup is None:
                pickup = PickupExecution(
                    pickup_execution_id=new_uuid7(),
                    request_id=request.request_id,
                    collection_group_id=None,
                    status="PENDING_ASSIGNMENT",
                    created_at=now,
                    updated_at=now,
                )
                session.add(pickup)
                await session.flush([pickup])
            if stop.kind.value == "PICKUP":
                owner = await session.scalar(
                    select(RiderAssignmentItem).where(
                        RiderAssignmentItem.pickup_execution_id == pickup.pickup_execution_id,
                        RiderAssignmentItem.released_at.is_(None),
                    )
                )
                if owner is not None and owner.assignment_id != root.assignment_id:
                    raise RouteAuthorityError("OWNERSHIP_CHANGED")
                if owner is None:
                    historical = await session.get(
                        RiderAssignmentItem, (root.assignment_id, pickup.pickup_execution_id)
                    )
                    if historical is not None:
                        raise RouteAuthorityError("RELEASED_ITEM_REQUIRES_NEW_ASSIGNMENT")
                    session.add(
                        RiderAssignmentItem(
                            assignment_id=root.assignment_id,
                            pickup_execution_id=pickup.pickup_execution_id,
                            assigned_at=now,
                        )
                    )
                pickup.status = "ASSIGNED"
                pickup.updated_at = now
            task = await session.scalar(
                select(RouteTask).where(
                    RouteTask.route_id == root.route_id,
                    RouteTask.stop_id == stop_id,
                    RouteTask.state.in_(("PENDING", "STARTED")),
                )
            )
            if task is None:
                task = RouteTask(
                    route_id=root.route_id,
                    stop_id=stop_id,
                    pickup_execution_id=pickup.pickup_execution_id,
                    kind=stop.kind.value,
                    receiving_point_id=stop.receiving_point_id,
                    state="PENDING",
                )
                session.add(task)
                await session.flush([task])
            scheduled = next(s for s in schedules[v.vehicle_id].stops if s.stop_id == stop_id)
            session.add(
                RouteRevisionStop(
                    revision_id=revision.revision_id,
                    ordinal=ordinal,
                    task_id=task.task_id,
                    planned_start=problem.epoch + timedelta(seconds=scheduled.service_start_at),
                    planned_end=problem.epoch + timedelta(seconds=scheduled.service_end_at),
                )
            )
        availability = await session.get(RiderAvailability, v.rider_id)
        assert availability is not None
        availability.work_state = "BUSY"
        availability.version += 1
        availability.updated_at = now
        vehicle = await session.get(OperatingVehicle, v.vehicle_id)
        assert vehicle is not None
        vehicle.version += 1
        if config.regional:
            from .orchestration_models import RouteDelivery

            # Receipt deadline derives from dispatch readiness and predicted first stop.
            acknowledgment = 60
            readiness = 0
            if command.fleet_cycle_id:
                assert cycle is not None and cycle.measurements is not None
                configured = cycle.measurements["policy"]
                acknowledgment = configured.get("delivery_ack_seconds", 60)
                readiness = max(0, configured.get("dispatch_readiness_seconds", 0) - acknowledgment)
            first = schedules[v.vehicle_id].stops[0].service_start_at
            due = min(
                now + timedelta(seconds=acknowledgment),
                max(now, problem.epoch + timedelta(seconds=first - readiness)),
            )
            session.add(
                RouteDelivery(
                    revision_id=revision.revision_id,
                    rider_id=v.rider_id,
                    due_at=now,
                    acknowledgment_due_at=due,
                    attempts=0,
                )
            )
        await append_outbox_event(
            session,
            event_key=f"vehicle-route-revised:{revision.revision_id}",
            aggregate_type="VehicleRoute",
            aggregate_id=root.route_id,
            event_type="VehicleRouteRevised",
            payload={
                "route_id": str(root.route_id),
                "revision_id": str(revision.revision_id),
                "rider_id": str(v.rider_id),
            },
        )
    from .capacity_models import RiderReplacement

    for replacement in await session.scalars(
        select(RiderReplacement).where(
            in_ids(RiderReplacement.request_id, [s.lot_id for s in actual_problem.stops]),
            RiderReplacement.status == "APPROVED",
        )
    ):
        if replacement.transfer_intent_id:
            from .orchestration_models import CargoTransferIntent

            intent = await session.get(CargoTransferIntent, replacement.transfer_intent_id)
            if intent is None or intent.resolution != "TRANSFERRED":
                continue
        pickup_stop = next(
            (
                s
                for s in actual_problem.stops
                if s.lot_id == replacement.request_id
                and s.kind.value == ("DISCHARGE" if replacement.transfer_intent_id else "PICKUP")
            ),
            None,
        )
        replacement_owner = next(
            (
                r.vehicle_id
                for r in proposal.routes
                if pickup_stop and pickup_stop.stop_id in r.stop_ids
            ),
            None,
        )
        if (
            replacement_owner is not None
            and by_vehicle[replacement_owner].operating_shift_id == replacement.recommended_shift_id
        ):
            replacement.status = "COMPLETED"
            replacement.version += 1
            replacement.failure_code = None
            replacement.activated_at = now
            replacement.activation_id = activation.activation_id
            await append_outbox_event(
                session,
                event_key=f"replacement.activated:{replacement.replacement_id}",
                event_type="routing.replacement.activated",
                aggregate_type="rider_replacement",
                aggregate_id=replacement.replacement_id,
                payload={
                    "replacement_id": str(replacement.replacement_id),
                    "activation_id": str(activation.activation_id),
                },
                available_at=now,
            )
    await complete_idempotency_record(
        session,
        claim.record,
        result_resource_id=activation.activation_id,
        result_status_code=200,
        completed_at=now,
    )
    if command.fleet_cycle_id is not None:
        assert cycle is not None
        cycle.activation_id = activation.activation_id
        cycle.selected_engine = command.engine.value
        cycle.status, cycle.completed_at = "ACTIVATED", now
        if command.engine == SelectionEngine.COMPOSITE:
            from .regional_planner import complete_cycle

            await complete_cycle(session, cycle, now)
    return activation.activation_id
