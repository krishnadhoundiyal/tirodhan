"""Generated schedules with independently observed simultaneous PostgreSQL lock waits."""

import asyncio
import os
from dataclasses import replace
from datetime import timedelta

import pytest
from fleet_verification.contracts import proposal as reference_proposal
from fleet_verification.database import DatabaseHarness
from fleet_verification.oracle import Weights, enumerate_feasible
from hypothesis import event, example, given, settings
from hypothesis import strategies as st
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from test_routing_dispatch import add_booking, collect_first, execute, root_revision
from test_routing_fleet import LineMatrix, engines, fixture, tick
from test_routing_operations import process, requested

from tirodhan.db.values import new_uuid7
from tirodhan.modules.collection_requests.cancellation import (
    CancellationConflictError,
    cancel_collection_request_by_customer,
)
from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.dispatch.models import RiderAssignmentItem
from tirodhan.modules.handovers.models import HandoverEvent, HandoverEventItem
from tirodhan.modules.payments.models import Payment
from tirodhan.modules.pickups.models import PickupAttempt
from tirodhan.modules.planning.models import PickupExecution
from tirodhan.modules.planning.routing.adapters import ORTOOLS_VERSION, VROOM_VERSION
from tirodhan.modules.planning.routing.contracts import OptimizerEngine
from tirodhan.modules.routing_operations.authority import ActivationCommand, activate_proposal
from tirodhan.modules.routing_operations.commands import OperationalInputError, PositionReport
from tirodhan.modules.routing_operations.dispatch_models import (
    RouteActivation,
    RouteRevision,
    RouteTask,
    VehicleRoute,
)
from tirodhan.modules.routing_operations.execution import (
    DepartTask,
    DischargeTask,
    PickupTask,
    StartTask,
)
from tirodhan.modules.routing_operations.fleet_models import FleetControl, FleetCycle
from tirodhan.modules.routing_operations.models import (
    CargoVehicleObservation,
    OperatingVehicle,
)
from tirodhan.modules.routing_operations.recovery import RecoveryCommand, recover_route
from tirodhan.modules.routing_operations.telemetry import VehicleIssueCommand, report_vehicle_issue

pytestmark = pytest.mark.integration
SCENARIOS = (
    "workers",
    "same-booking",
    "revision-departure",
    "revision-collection",
    "cancel-dispatch",
    "cancel-collection",
    "position-publication",
    "cargo-assignment",
    "breakdown-reallocation",
    "shift-expiry",
    "handover-routing",
    "crash-takeover",
    "command-replay",
    "multi-operation",
)


async def overlap(factory, model, key, commands, *, after_wait=None):
    """Block genuine domain transactions at a DB row, confirm each is waiting, release.

    Sequential tasks or callbacks alone cannot satisfy this test. PostgreSQL proves
    simultaneously live transactions, including chained Payment/resource/command waits.
    """
    async with factory() as blocker:
        await blocker.begin()
        column = list(model.__table__.primary_key.columns)[0]
        await blocker.execute(select(model).where(column == key).with_for_update())
        tasks = []
        try:
            for index, command in enumerate(commands):
                tasks.append(asyncio.create_task(command()))
                for _ in range(400):
                    async with factory() as observer:
                        waiting = await observer.scalar(
                            text(
                                "SELECT count(*) FROM pg_stat_activity "
                                "WHERE datname=current_database() AND pid<>pg_backend_pid() "
                                "AND wait_event_type='Lock' AND xact_start IS NOT NULL"
                            )
                        )
                    if waiting >= index + 1:
                        break
                    if tasks[-1].done():
                        raise AssertionError(
                            "command finished before genuine overlap was established"
                        )
                    await asyncio.sleep(0.01)
                else:
                    raise AssertionError("PostgreSQL did not observe all competing transactions")
            if after_wait:
                after_wait()
            await blocker.commit()
            return await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 30)
        finally:
            await blocker.rollback()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def scenario(factory, kind, loaded, delta, reverse):
    f, vehicles, point, ids, config, resource, clock = await fixture(factory)
    if loaded or kind not in ("workers", "same-booking", "cancel-dispatch", "crash-takeover"):
        assert (await tick(factory, clock)).status == "ACTIVATED"
    else:
        loaded = False
    if kind in ("revision-collection", "cancel-collection", "cargo-assignment", "handover-routing"):
        loaded = False
        root, revision, tasks = await root_revision(factory)
        async with factory() as session:
            pickup = await session.get(PickupExecution, tasks[0].pickup_execution_id)
            booking = await session.get(CollectionRequest, pickup.request_id)
        clock[0] = booking.slot_start
        await resource(
            PositionReport(
                client_command_id=new_uuid7(),
                vehicle_id=vehicles[0],
                longitude=77.208,
                latitude=28.631,
                observed_at=clock[0],
            ),
            clock[0],
        )
        await execute(
            factory,
            f.rider_ids[0],
            StartTask(
                client_command_id=new_uuid7(),
                task_id=tasks[0].task_id,
                revision_id=revision.revision_id,
            ),
            clock[0],
        )
    elif loaded:
        root, revision, task, _, _, at = await collect_first(factory, f, config)
        clock[0] = at
    before_cargo = {}
    async with factory() as session:
        before_cargo = {
            c.request_id: c.vehicle_id
            for c in await session.scalars(select(CargoVehicleObservation))
        }
    clock[0] += timedelta(seconds=delta)
    if loaded:
        await resource(
            PositionReport(
                client_command_id=new_uuid7(),
                vehicle_id=vehicles[0],
                longitude=77.208,
                latitude=28.631,
                observed_at=clock[0],
            ),
            clock[0],
        )
    if kind == "cargo-assignment" or (
        loaded and kind in ("workers", "same-booking", "crash-takeover")
    ):
        await add_booking(factory, config, point, ids[0], f.manager_id)
    cancellation_target = ids[1] if loaded else ids[0]

    async def plan():
        return await tick(factory, clock)

    async def crashed():
        solvers = engines()

        async def fail(*args, **kwargs):
            raise asyncio.CancelledError

        for solver in solvers.values():
            solver.propose = fail
        return await tick(factory, clock, optimizers=solvers)

    if kind in ("workers", "crash-takeover"):
        results = await overlap(
            factory,
            FleetControl,
            "COMMITTED_FLEET",
            [crashed, crashed] if kind == "crash-takeover" else [plan, plan],
        )
        if kind == "workers":
            assert (
                sum(
                    r is not None and not isinstance(r, BaseException) and r.status == "ACTIVATED"
                    for r in results
                )
                == 1
            )
        else:
            # Lock acquisition order is not a worker identity guarantee. Whichever
            # contender owns the lease crashes; the other must observe that owner.
            assert sum(isinstance(r, asyncio.CancelledError) for r in results) == 1
            assert sum(r is None for r in results) == 1
            clock[0] += timedelta(seconds=121)
            assert (await tick(factory, clock)).status == "ACTIVATED"
            async with factory() as session:
                assert {"STALE", "ACTIVATED"} <= set(
                    await session.scalars(select(FleetCycle.status))
                )
        async with factory() as session:
            after = {
                c.request_id: c.vehicle_id
                for c in await session.scalars(select(CargoVehicleObservation))
            }
            assert before_cargo.items() <= after.items()
        return

    async def evaluated_command():
        class EnumeratedEngine:
            def __init__(self, engine):
                self.engine = engine

            async def propose(self, problem, *, search_seconds):
                choices = list(enumerate_feasible(problem, Weights(0, 0, 0, 0, 0), {}))
                assert choices, "The race setup must have independently feasible proposals"
                orders, _ = min(choices, key=lambda choice: choice[1].rank)
                return replace(
                    reference_proposal(problem, orders),
                    engine=self.engine,
                    engine_version=VROOM_VERSION
                    if self.engine == OptimizerEngine.VROOM
                    else ORTOOLS_VERSION,
                )

        conf = config.model_copy(update={"max_position_age_seconds": 20000})
        run = await requested(factory, f, conf)
        result = await process(
            factory,
            run,
            conf,
            provider=LineMatrix(),
            clock=lambda: clock[0],
            optimizers={e: EnumeratedEngine(e) for e in OptimizerEngine},
        )
        assert result.status == "SUCCEEDED", result.failure_code
        return ActivationCommand(
            client_command_id=new_uuid7(),
            run_id=run,
            engine="VROOM",
            resume_recovery=kind == "handover-routing",
        )

    async def activate(command):
        return await activate_proposal(
            factory,
            manager_id=f.manager_id,
            command=command,
            expires_at=clock[0] + timedelta(days=1),
            now=clock[0],
            clock=lambda: clock[0],
        )

    if kind == "handover-routing":
        command = PickupTask(
            client_command_id=new_uuid7(),
            task_id=tasks[0].task_id,
            revision_id=revision.revision_id,
            outcome="COLLECTED",
        )
        await execute(factory, f.rider_ids[0], command, clock[0])
        await recover_route(
            factory,
            manager_id=f.manager_id,
            command=RecoveryCommand(
                client_command_id=new_uuid7(),
                route_id=root.route_id,
                revision_id=revision.revision_id,
                reason_code="EXECUTION_EXCEPTION",
            ),
            expires_at=clock[0] + timedelta(days=1),
            now=clock[0],
        )
        root, revision, tasks = await root_revision(factory)
        await execute(
            factory,
            f.rider_ids[0],
            StartTask(
                client_command_id=new_uuid7(),
                task_id=tasks[0].task_id,
                revision_id=revision.revision_id,
            ),
            clock[0],
        )
        async with factory() as session:
            before_cargo = {
                c.request_id: c.vehicle_id
                for c in await session.scalars(select(CargoVehicleObservation))
            }

    proposal = await evaluated_command()

    async def publication():
        return await activate(proposal)

    async def cancel():
        async with factory() as session, session.begin():
            booking = await session.get(CollectionRequest, cancellation_target)
            return await cancel_collection_request_by_customer(
                session,
                request_id=cancellation_target,
                customer_id=booking.customer_id,
                idempotency_key="generated-race",
                planning_lead_time_minutes=1,
                idempotency_expires_at=clock[0] + timedelta(days=1),
            )

    async def position():
        return await resource(
            PositionReport(
                client_command_id=new_uuid7(),
                vehicle_id=vehicles[0],
                longitude=77.207 + delta * 0.00001,
                latitude=28.632,
                observed_at=clock[0],
            ),
            clock[0],
        )

    if kind in ("same-booking", "cancel-dispatch"):
        second = await evaluated_command() if kind == "same-booking" else None

        async def other():
            return await activate(second)

        commands = [publication, other if second else cancel]
    else:
        root, revision, tasks = await root_revision(factory)
        next_task = tasks[0]
        if kind in ("revision-collection", "cancel-collection", "cargo-assignment"):
            mutation = PickupTask(
                client_command_id=new_uuid7(),
                task_id=next_task.task_id,
                revision_id=revision.revision_id,
                outcome="COLLECTED",
            )
        elif kind == "handover-routing":
            mutation = DischargeTask(
                client_command_id=new_uuid7(),
                task_id=next_task.task_id,
                revision_id=revision.revision_id,
                scanned_pickup_id=next_task.pickup_execution_id,
                scanned_receiving_identifier="test-kiosk-42",
                client_capture_id=new_uuid7(),
                captured_at=clock[0],
                longitude=77.211,
                latitude=28.630,
            )
        else:
            mutation = DepartTask(
                client_command_id=new_uuid7(),
                task_id=next_task.task_id,
                revision_id=revision.revision_id,
            )

        async def execute_mutation():
            return await execute(factory, f.rider_ids[0], mutation, clock[0])

        async def breakdown():
            return await report_vehicle_issue(
                factory,
                rider_id=f.rider_ids[0],
                command=VehicleIssueCommand(
                    client_command_id=new_uuid7(), route_id=root.route_id, reason="BREAKDOWN"
                ),
                expires_at=clock[0] + timedelta(days=1),
                now=clock[0],
            )

        choices = {
            "revision-departure": [publication, execute_mutation],
            "revision-collection": [publication, execute_mutation],
            "cancel-collection": [execute_mutation, cancel],
            "position-publication": [publication, position],
            "cargo-assignment": [publication, execute_mutation],
            "breakdown-reallocation": [publication, breakdown],
            "shift-expiry": [publication, position],
            "handover-routing": [publication, execute_mutation],
            "command-replay": [execute_mutation, execute_mutation],
            "multi-operation": [publication, execute_mutation, position],
        }
        commands = choices[kind]
    if reverse and kind != "cancel-collection":
        commands = commands[::-1]

    def expire():
        clock[0] = config.horizon_end + timedelta(seconds=1)

    lock_model, lock_key = OperatingVehicle, vehicles[0]
    if kind in ("cancel-dispatch", "cancel-collection"):
        lock_model = Payment
        async with factory() as session:
            lock_key = await session.scalar(
                select(Payment.payment_id).where(Payment.request_id == cancellation_target)
            )
    results = await overlap(
        factory,
        lock_model,
        lock_key,
        commands,
        after_wait=expire if kind == "shift-expiry" else None,
    )
    for result in results:
        if isinstance(result, BaseException):
            assert isinstance(
                result, (OperationalInputError, CancellationConflictError, DBAPIError)
            ), repr(result)
            if isinstance(result, DBAPIError):
                assert getattr(result.orig, "sqlstate", None) in {"40001", "40P01"}
            event(f"recoverable conflict: {type(result).__name__}")
    if kind == "command-replay":
        assert results[0] == results[1]
    if kind == "shift-expiry":
        assert any(isinstance(r, OperationalInputError) for r in results)
    async with factory() as session:
        rows = list(
            (
                await session.execute(
                    select(RiderAssignmentItem.pickup_execution_id, func.count())
                    .where(RiderAssignmentItem.released_at.is_(None))
                    .group_by(RiderAssignmentItem.pickup_execution_id)
                )
            ).all()
        )
        assert all(count == 1 for _, count in rows)
        after_cargo = {
            c.request_id: c.vehicle_id
            for c in await session.scalars(select(CargoVehicleObservation))
        }
        assert before_cargo.items() <= after_cargo.items()
        for key, _vehicle in after_cargo.items():
            pickup = await session.scalar(
                select(PickupExecution).where(PickupExecution.request_id == key)
            )
            assert pickup.status == "COLLECTED"
            assert (await session.get(CollectionRequest, key)).status != "CANCELLED"
        # Every committed route has one revision at its current version and an activation.
        for route in await session.scalars(select(VehicleRoute)):
            assert (
                await session.scalar(
                    select(RouteRevision.revision_id).where(
                        RouteRevision.route_id == route.route_id,
                        RouteRevision.version == route.version,
                    )
                )
                is not None
            )
            if route.status == "ACTIVE":
                assert (
                    await session.scalar(
                        select(RouteTask.task_id).where(
                            RouteTask.route_id == route.route_id,
                            RouteTask.state.in_(("PENDING", "STARTED")),
                        )
                    )
                    is not None
                )
        assert (
            await session.scalar(select(func.count()).select_from(RouteActivation)) >= 1
            or kind == "cancel-dispatch"
        )
        if kind == "cancel-collection":
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(PickupAttempt)
                    .where(PickupAttempt.outcome == "COLLECTED")
                )
                == 1
            )
        if kind == "handover-routing":
            # Handover may need a retry after an optimistic conflict, but custody cannot vanish.
            validated = await session.scalar(
                select(func.count())
                .select_from(HandoverEventItem)
                .join(HandoverEvent)
                .where(HandoverEvent.status == "VALIDATED", HandoverEventItem.status == "VALIDATED")
            )
            assert validated in (0, 1)


@pytest.mark.parametrize("kind", SCENARIOS)
@settings(max_examples=3 if os.getenv("TIRODHAN_HYPOTHESIS_PROFILE") != "fleet-extended" else 12)
@given(loaded=st.booleans(), delta=st.integers(1, 4), reverse=st.booleans())
@example(loaded=True, delta=1, reverse=False)
def test_generated_real_transaction_races(migrated_database_url, kind, loaded, delta, reverse):
    db = DatabaseHarness(migrated_database_url)
    try:
        db.run(scenario(db.factory, kind, loaded, delta, reverse))
    finally:
        db.close()
