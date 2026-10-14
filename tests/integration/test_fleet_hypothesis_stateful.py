"""Generated real-service histories with a separate ownership/custody event ledger."""

from collections import Counter
from dataclasses import dataclass
from datetime import timedelta
from unittest.mock import patch

import pytest
from fleet_verification.database import DatabaseHarness
from fleet_verification.oracle import Weights, evaluate
from geoalchemy2.elements import WKTElement
from hypothesis import event, given, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule, run_state_machine_as_test
from sqlalchemy import select, update
from test_routing_dispatch import add_booking, execute
from test_routing_fleet import engines, fixture, tick

from tirodhan.db.values import new_uuid7
from tirodhan.modules.collection_requests.cancellation import (
    CancellationConflictError,
    cancel_collection_request_by_customer,
)
from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.dispatch.models import RiderAssignmentItem
from tirodhan.modules.handovers.models import HandoverEvent, HandoverEventItem
from tirodhan.modules.pickups.models import PickupAttempt
from tirodhan.modules.planning.models import PickupExecution
from tirodhan.modules.receiving_points.models import ReceivingPoint
from tirodhan.modules.routing_operations.commands import (
    OperationalInputError,
    PositionReport,
    RestrictFacility,
    ShiftClose,
    ShiftOpen,
    WindowDeclare,
)
from tirodhan.modules.routing_operations.dispatch_models import (
    RouteRevision,
    RouteRevisionStop,
    RouteTask,
    VehicleRoute,
)
from tirodhan.modules.routing_operations.execution import (
    DepartTask,
    DischargeTask,
    PickupTask,
    StartTask,
    current_tasks,
)
from tirodhan.modules.routing_operations.fleet_models import FleetControl
from tirodhan.modules.routing_operations.models import (
    CargoVehicleObservation,
    OperatingShift,
    RoutingShadowRun,
)
from tirodhan.modules.routing_operations.recovery import RecoveryCommand, recover_route
from tirodhan.modules.routing_operations.service import PROBLEM_CODEC, PROPOSAL_CODEC
from tirodhan.modules.routing_operations.telemetry import VehicleIssueCommand, report_vehicle_issue

pytestmark = pytest.mark.integration
OPERATIONS = Counter()


@dataclass
class Household:
    stage: str = "UNALLOCATED"
    status: str = "ACCEPTED"
    owner: object = None
    cargo: object = None
    handed: bool = False
    trusted_load: bool = True


class FleetHistory(RuleBasedStateMachine):
    def __init__(self, url):
        super().__init__()
        self.db = DatabaseHarness(url)
        self.f, self.vehicles, self.point, ids, self.config, self.resource, self.clock = (
            self.db.run(fixture(self.db.factory))
        )
        self.households = {r: Household() for r in ids}

        async def second_facility():
            async with self.db.factory() as session, session.begin():
                point = ReceivingPoint(
                    official_name="Generated receiving point B",
                    official_identifier="test-kiosk-43",
                    location=WKTElement("POINT(77.215 28.632)", srid=4326),
                    allowed_radius_m=100,
                    status="ACTIVE",
                )
                session.add(point)
                await session.flush()
                return point

        other = self.db.run(second_facility())
        self.db.run(
            self.resource(
                WindowDeclare(
                    client_command_id=new_uuid7(),
                    receiving_point_id=other.receiving_point_id,
                    starts_at=self.config.epoch,
                    ends_at=self.config.horizon_end,
                ),
                self.clock[0],
            )
        )
        self.facilities = (self.point, other)
        self.destinations = {key: 0 for key in ids}
        self.orders = {}
        self.completed = {}
        self.protected = {}
        self.started_tasks = set()
        self.replays = []
        self.attempts = 0
        self.handover_attempts = 0
        self.revisions = {}
        self.loads = {r: (4000, 4000) for r in ids}

        async def input_windows():
            async with self.db.factory() as session:
                return {
                    b.request_id: (b.slot_start, b.slot_end)
                    for b in await session.scalars(select(CollectionRequest))
                }

        self.windows = self.db.run(input_windows())
        self.optimize(False)
        self.progress(0, True, True)

    async def roots(self):
        async with self.db.factory() as session:
            roots = list(
                await session.scalars(
                    select(VehicleRoute)
                    .where(VehicleRoute.status != "CLOSED")
                    .order_by(VehicleRoute.vehicle_id)
                )
            )
            return [(root, await current_tasks(session, root)) for root in roots]

    def vehicle_rider(self, root):
        return self.f.rider_ids[self.vehicles.index(root.vehicle_id)]

    async def fresh_positions(self):
        self.clock[0] += timedelta(seconds=1)
        for i, vehicle in enumerate(self.vehicles):
            await self.resource(
                PositionReport(
                    client_command_id=new_uuid7(),
                    vehicle_id=vehicle,
                    longitude=77.207 + i * 0.013,
                    latitude=28.632,
                    observed_at=self.clock[0],
                ),
                self.clock[0],
            )

    async def execute(self, root, task, command):
        async with self.db.factory() as session:
            revision = await session.scalar(
                select(RouteRevision).where(
                    RouteRevision.route_id == root.route_id, RouteRevision.version == root.version
                )
            )
        command = command.model_copy(
            update={"task_id": task.task_id, "revision_id": revision.revision_id}
        )
        rider = self.vehicle_rider(root)
        try:
            result = await execute(self.db.factory, rider, command, self.clock[0])
        except OperationalInputError as error:
            event(f"execution rejected: {error}")
            return None
        self.replays.append((rider, command, self.clock[0], result))
        OPERATIONS[command.operation] += 1
        return result

    @rule(immediate=st.booleans(), facility=st.integers(0, 1))
    def booking(self, immediate, facility=0):
        async def create():
            key = await add_booking(
                self.db.factory,
                self.config,
                self.facilities[facility],
                next(iter(self.households)),
                self.f.manager_id,
            )
            # Synthetic paid-booking admission input, before any planning/execution.
            async with self.db.factory() as session, session.begin():
                start = self.clock[0] + timedelta(minutes=0 if immediate else 30)
                await session.execute(
                    update(CollectionRequest)
                    .where(CollectionRequest.request_id == key)
                    .values(slot_start=start, slot_end=start + timedelta(minutes=30))
                )
            self.windows[key] = (start, start + timedelta(minutes=30))
            return key

        if len(self.households) < 6:
            key = self.db.run(create())
            self.households[key] = Household()
            self.destinations[key] = facility
            self.loads[key] = (2000, 2000)
            OPERATIONS["BOOKING"] += 1

    @rule(failure=st.booleans())
    def optimize(self, failure):
        async def plan():
            await self.fresh_positions()
            solvers = engines()
            if failure:

                async def unavailable(*args, **kwargs):
                    raise TimeoutError

                for solver in solvers.values():
                    solver.propose = unavailable
            result = await tick(self.db.factory, self.clock, optimizers=solvers)
            OPERATIONS["CYCLE_" + (result.status if result else "NOT_DUE")] += 1
            if result is None or result.status != "ACTIVATED":
                return
            async with self.db.factory() as session:
                run = await session.get(RoutingShadowRun, result.run_id)
            problem = PROBLEM_CODEC.validate_python(run.problem)
            candidate = PROPOSAL_CODEC.validate_python(
                run.results[result.selected_engine]["proposal"]
            )
            economics = result.measurements["policy"]["economics"]
            weights = Weights(
                economics["additional_vehicle_seconds"],
                economics["waiting_weight"],
                economics["disruption_seconds"],
                economics["window_risk_buffer_seconds"],
                economics["window_risk_weight"],
            )
            orders = {r.vehicle_id: r.stop_ids for r in candidate.routes}
            evaluate(problem, orders, weights, self.orders)
            by_stop = {s.stop_id: s for s in problem.stops}
            for route in candidate.routes:
                for stop_id in route.stop_ids:
                    stop = by_stop[stop_id]
                    household = self.households[stop.lot_id]
                    if stop.kind == "PICKUP":
                        assert household.status != "CANCELLED" and household.cargo is None
                        assert household.trusted_load
                        household.owner = route.vehicle_id
                        if household.stage != "STARTED":
                            household.stage = "ASSIGNED"
                    else:
                        if household.cargo is not None:
                            assert household.cargo == route.vehicle_id
            self.orders = orders

        self.db.run(plan())

    @rule(vehicle=st.integers(0, 1), seconds=st.integers(1, 180), displacement=st.integers(-20, 20))
    def move(self, vehicle, seconds, displacement):
        self.clock[0] += timedelta(seconds=seconds)
        self.db.run(
            self.resource(
                PositionReport(
                    client_command_id=new_uuid7(),
                    vehicle_id=self.vehicles[vehicle],
                    longitude=77.207 + vehicle * 0.013 + displacement * 0.00001,
                    latitude=28.632,
                    observed_at=self.clock[0],
                ),
                self.clock[0],
            )
        )

    @rule(vehicle=st.integers(0, 1), collect=st.booleans(), valid_receiving=st.booleans())
    def progress(self, vehicle, collect, valid_receiving):
        async def progress():
            roots = [
                (r, t)
                for r, t in await self.roots()
                if r.vehicle_id == self.vehicles[vehicle] and t
            ]
            if not roots:
                return
            root, tasks = roots[0]
            task = tasks[0]
            async with self.db.factory() as session:
                pickup = await session.get(PickupExecution, task.pickup_execution_id)
                booking = await session.get(CollectionRequest, pickup.request_id)
            household = self.households[pickup.request_id]
            if task.kind == "PICKUP" and self.clock[0] < booking.slot_start:
                self.clock[0] = booking.slot_start
            await self.fresh_positions()
            if task.state == "PENDING":
                cmd = StartTask(
                    client_command_id=new_uuid7(), task_id=task.task_id, revision_id=new_uuid7()
                )
                if await self.execute(root, task, cmd) is None:
                    return
                household.stage = "STARTED" if task.kind == "PICKUP" else "COLLECTED"
                if task.kind == "PICKUP":
                    assert booking.slot_start <= self.clock[0] <= booking.slot_end
                    household.status = "PLANNED"
                self.protected[task.task_id] = root.vehicle_id
                self.started_tasks.add(task.task_id)
            if task.kind == "PICKUP":
                cmd = PickupTask(
                    client_command_id=new_uuid7(),
                    task_id=task.task_id,
                    revision_id=new_uuid7(),
                    outcome="COLLECTED" if collect else "NOT_COLLECTED",
                )
                if await self.execute(root, task, cmd) is not None:
                    self.attempts += 1
                    if collect:
                        assert household.cargo is None
                        household.cargo, household.stage = root.vehicle_id, "COLLECTED"
                        self.completed[task.task_id] = self.clock[0]
                        self.protected.pop(task.task_id, None)
                        self.started_tasks.discard(task.task_id)
            else:
                destination = self.destinations[pickup.request_id]
                cmd = DischargeTask(
                    client_command_id=new_uuid7(),
                    task_id=task.task_id,
                    revision_id=new_uuid7(),
                    scanned_pickup_id=pickup.pickup_execution_id,
                    scanned_receiving_identifier=self.facilities[destination].official_identifier,
                    client_capture_id=new_uuid7(),
                    captured_at=self.clock[0],
                    longitude=(77.211 if destination == 0 else 77.215) if valid_receiving else 78.0,
                    latitude=28.630 if destination == 0 else 28.632,
                )
                if await self.execute(root, task, cmd) is not None:
                    self.handover_attempts += 1
                    if valid_receiving:
                        assert household.cargo == root.vehicle_id
                        household.handed = True
                        self.completed[task.task_id] = self.clock[0]
                        self.protected.pop(task.task_id, None)
                        self.started_tasks.discard(task.task_id)
            self.orders[root.vehicle_id] = tuple(
                t.stop_id for t in tasks if t.task_id not in self.completed
            )

        self.db.run(progress())

    @rule(vehicle=st.integers(0, 1))
    def depart(self, vehicle):
        async def depart():
            for root, tasks in await self.roots():
                if root.vehicle_id == self.vehicles[vehicle] and tasks:
                    task = tasks[0]
                    if (
                        await self.execute(
                            root,
                            task,
                            DepartTask(
                                client_command_id=new_uuid7(),
                                task_id=task.task_id,
                                revision_id=new_uuid7(),
                            ),
                        )
                        is not None
                    ):
                        self.protected[task.task_id] = root.vehicle_id

        self.db.run(depart())

    @rule(index=st.integers(0, 5))
    def cancel(self, index):
        key = list(self.households)[index % len(self.households)]
        household = self.households[key]

        async def cancel():
            try:
                async with self.db.factory() as session, session.begin():
                    booking = await session.get(CollectionRequest, key)
                    with patch(
                        "tirodhan.modules.collection_requests.cancellation.utc_now",
                        lambda: self.clock[0],
                    ):
                        await cancel_collection_request_by_customer(
                            session,
                            request_id=key,
                            customer_id=booking.customer_id,
                            idempotency_key=f"generated-cancel-{key}",
                            planning_lead_time_minutes=1,
                            idempotency_expires_at=self.clock[0] + timedelta(days=1),
                        )
                return True
            except CancellationConflictError as error:
                event(f"cancellation rejected: {type(error).__name__}")
                return False

        if self.db.run(cancel()):
            assert household.cargo is None and household.stage != "STARTED"
            household.status, household.owner, household.stage = "CANCELLED", None, "CANCELLED"
            self.db.run(self.remove_cancelled_tasks(key))

    async def remove_cancelled_tasks(self, key):
        async with self.db.factory() as session:
            pickup = await session.scalar(
                select(PickupExecution).where(PickupExecution.request_id == key)
            )
            if pickup:
                for task in await session.scalars(
                    select(RouteTask).where(
                        RouteTask.pickup_execution_id == pickup.pickup_execution_id
                    )
                ):
                    self.protected.pop(task.task_id, None)
                    self.started_tasks.discard(task.task_id)
                    root = await session.get(VehicleRoute, task.route_id)
                    self.orders[root.vehicle_id] = tuple(
                        t.stop_id for t in await current_tasks(session, root)
                    )

    @rule(
        vehicle=st.integers(0, 1),
        breakdown=st.booleans(),
        reason=st.sampled_from(("BREAKDOWN", "SAFETY", "DISCHARGE_DELAY", "LOAD_UNCERTAIN")),
    )
    def recover(self, vehicle, breakdown, reason):
        async def recover():
            for root, _ in await self.roots():
                if root.vehicle_id != self.vehicles[vehicle]:
                    continue
                if breakdown:
                    await report_vehicle_issue(
                        self.db.factory,
                        rider_id=self.vehicle_rider(root),
                        command=VehicleIssueCommand(
                            client_command_id=new_uuid7(),
                            route_id=root.route_id,
                            reason=reason,
                        ),
                        expires_at=self.clock[0] + timedelta(days=1),
                        now=self.clock[0],
                    )
                    OPERATIONS["ISSUE_" + reason] += 1
                    if reason == "LOAD_UNCERTAIN":
                        for household in self.households.values():
                            if household.owner == root.vehicle_id:
                                household.trusted_load = False
                async with self.db.factory() as session:
                    revision = await session.scalar(
                        select(RouteRevision).where(
                            RouteRevision.route_id == root.route_id,
                            RouteRevision.version == root.version,
                        )
                    )
                await recover_route(
                    self.db.factory,
                    manager_id=self.f.manager_id,
                    command=RecoveryCommand(
                        client_command_id=new_uuid7(),
                        route_id=root.route_id,
                        revision_id=revision.revision_id,
                        reason_code="EXECUTION_EXCEPTION",
                    ),
                    expires_at=self.clock[0] + timedelta(days=1),
                    now=self.clock[0],
                )
                for key, household in self.households.items():
                    if household.owner == root.vehicle_id and household.cargo is None:
                        household.owner, household.stage = None, "PENDING_ASSIGNMENT"
                        await self.remove_cancelled_tasks(key)
                self.orders[root.vehicle_id] = tuple(
                    t.stop_id
                    for r, tasks in await self.roots()
                    if r.route_id == root.route_id
                    for t in tasks
                )

        self.db.run(recover())

    @rule(seconds=st.integers(1, 120), facility=st.integers(0, 1))
    def restrict(self, seconds, facility=0):
        self.db.run(
            self.resource(
                RestrictFacility(
                    client_command_id=new_uuid7(),
                    receiving_point_id=self.facilities[facility].receiving_point_id,
                    starts_at=self.clock[0],
                    ends_at=self.clock[0] + timedelta(seconds=seconds),
                    reason="FULL",
                ),
                self.clock[0],
            )
        )

    @rule()
    def worker_restart(self):
        import asyncio

        async def restart():
            self.clock[0] += timedelta(seconds=61)
            await self.fresh_positions()
            solvers = engines()

            async def killed(*args, **kwargs):
                raise asyncio.CancelledError

            for solver in solvers.values():
                solver.propose = killed
            try:
                await tick(self.db.factory, self.clock, optimizers=solvers)
            except asyncio.CancelledError:
                OPERATIONS["WORKER_CRASH"] += 1
            self.clock[0] += timedelta(seconds=121)

        self.db.run(restart())
        self.optimize(False)

    @rule(index=st.integers(0, 20))
    def replay(self, index):
        if self.replays:
            rider, command, at, result = self.replays[index % len(self.replays)]
            assert self.db.run(execute(self.db.factory, rider, command, at)) == result
            OPERATIONS["REPLAY"] += 1

    @rule(vehicle=st.integers(0, 1))
    def end_or_restart_shift(self, vehicle):
        async def shift():
            async with self.db.factory() as session:
                current = await session.scalar(
                    select(OperatingShift)
                    .where(OperatingShift.vehicle_id == self.vehicles[vehicle])
                    .order_by(
                        OperatingShift.closed_at.is_(None).desc(),
                        OperatingShift.created_at.desc(),
                        OperatingShift.shift_id.desc(),
                    )
                )
            try:
                if current.closed_at is None:
                    await self.resource(
                        ShiftClose(client_command_id=new_uuid7(), shift_id=current.shift_id),
                        self.clock[0],
                    )
                else:
                    await self.resource(
                        ShiftOpen(
                            client_command_id=new_uuid7(),
                            vehicle_id=current.vehicle_id,
                            rider_id=current.rider_id,
                            commitment_id=new_uuid7(),
                            resource_kind="COMMITTED",
                            starts_at=self.clock[0],
                            ends_at=max(
                                self.config.horizon_end, self.clock[0] + timedelta(hours=2)
                            ),
                        ),
                        self.clock[0],
                    )
            except OperationalInputError as error:
                event(f"shift rejected: {error}")

        self.db.run(shift())

    @invariant()
    def persisted_ledger_agrees(self):
        async def check():
            async with self.db.factory() as session:
                bookings = {
                    b.request_id: b for b in await session.scalars(select(CollectionRequest))
                }
                pickups = {p.request_id: p for p in await session.scalars(select(PickupExecution))}
                assignment_vehicle = {
                    r.assignment_id: r.vehicle_id
                    for r in await session.scalars(select(VehicleRoute))
                }
                owners = {}
                for item in await session.scalars(
                    select(RiderAssignmentItem).where(RiderAssignmentItem.released_at.is_(None))
                ):
                    request = next(
                        k
                        for k, p in pickups.items()
                        if p.pickup_execution_id == item.pickup_execution_id
                    )
                    assert request not in owners
                    owners[request] = assignment_vehicle[item.assignment_id]
                cargo = {
                    o.request_id: o.vehicle_id
                    for o in await session.scalars(select(CargoVehicleObservation))
                }
                handed = set(
                    await session.scalars(
                        select(PickupExecution.request_id)
                        .join(
                            HandoverEventItem,
                            HandoverEventItem.pickup_execution_id
                            == PickupExecution.pickup_execution_id,
                        )
                        .join(
                            HandoverEvent,
                            HandoverEvent.handover_event_id == HandoverEventItem.handover_event_id,
                        )
                        .where(
                            HandoverEvent.status == "VALIDATED",
                            HandoverEventItem.status == "VALIDATED",
                        )
                    )
                )
                for key, household in self.households.items():
                    assert bookings[key].status == household.status
                    assert (bookings[key].slot_start, bookings[key].slot_end) == self.windows[key]
                    assert owners.get(key) == household.owner
                    assert cargo.get(key) == household.cargo
                    assert (key in handed) == household.handed
                    if household.cargo is not None:
                        assert pickups[key].status == "COLLECTED"
                    elif household.owner is not None:
                        assert pickups[key].status == "ASSIGNED"
                    elif household.status != "CANCELLED":
                        assert (
                            bookings[key].status == "ACCEPTED"
                            or pickups[key].status == "PENDING_ASSIGNMENT"
                        )
                for vehicle in self.vehicles:
                    for dimension in (0, 1):
                        assert (
                            sum(
                                self.loads[key][dimension]
                                for key, household in self.households.items()
                                if household.cargo == vehicle and not household.handed
                            )
                            <= 10000
                        )
                tasks = {t.task_id: t for t in await session.scalars(select(RouteTask))}
                for task_id, completed_at in self.completed.items():
                    assert (
                        tasks[task_id].state == "COMPLETED"
                        and tasks[task_id].completed_at == completed_at
                    )
                for task_id, vehicle in self.protected.items():
                    assert tasks[task_id].committed_at is not None and tasks[task_id].state in (
                        "PENDING",
                        "STARTED",
                    )
                    root = await session.get(VehicleRoute, tasks[task_id].route_id)
                    assert root.vehicle_id == vehicle
                    destination = await session.scalar(
                        select(RouteTask.task_id)
                        .join(RouteRevisionStop, RouteRevisionStop.task_id == RouteTask.task_id)
                        .join(
                            RouteRevision,
                            RouteRevision.revision_id == RouteRevisionStop.revision_id,
                        )
                        .where(
                            RouteRevision.route_id == root.route_id,
                            RouteRevision.version == root.version,
                            RouteTask.state.in_(("PENDING", "STARTED")),
                        )
                        .order_by(RouteRevisionStop.ordinal)
                        .limit(1)
                    )
                    assert destination == task_id
                for task_id in self.started_tasks:
                    assert tasks[task_id].state == "STARTED"
                assert len(list(await session.scalars(select(PickupAttempt)))) == self.attempts
                assert (
                    len(list(await session.scalars(select(HandoverEvent))))
                    == self.handover_attempts
                )
                for revision in await session.scalars(select(RouteRevision)):
                    fact = (
                        revision.route_id,
                        revision.version,
                        revision.activation_id,
                        revision.supersedes_revision_id,
                    )
                    assert self.revisions.setdefault(revision.revision_id, fact) == fact

        self.db.run(check())

    def teardown(self):
        self.db.close()


def test_generated_fleet_histories(migrated_database_url):
    run_state_machine_as_test(
        lambda: FleetHistory(migrated_database_url),
        settings=settings(
            max_examples=5 if settings.get_current_profile_name() == "fleet-ci" else 40
        ),
    )
    print("GENERATED_STATE_OPERATIONS", dict(OPERATIONS))


@settings(max_examples=4)
@given(late=st.booleans(), rejected=st.booleans())
def test_generated_complete_cargo_histories(migrated_database_url, late, rejected):
    machine = FleetHistory(migrated_database_url)
    try:
        if late:
            machine.booking(True, 1)
            machine.optimize(False)
            machine.persisted_ledger_agrees()
        if rejected:
            # Wrong receiving geography is a durable rejected fact, never custody release.
            machine.progress(0, True, False)
            machine.persisted_ledger_agrees()
            machine.progress(0, True, False)
            machine.persisted_ledger_agrees()
        for _ in range(12):
            for vehicle in (0, 1):
                machine.progress(vehicle, True, True)
                machine.persisted_ledger_agrees()
            if all(h.handed for h in machine.households.values()):
                break
        assert all(h.handed for h in machine.households.values())
        machine.replay(0)
        machine.persisted_ledger_agrees()
    finally:
        machine.teardown()


def test_equal_timestamp_shift_history_selects_the_open_identity(migrated_database_url):
    machine = FleetHistory(migrated_database_url)
    try:
        for _ in range(6):
            machine.end_or_restart_shift(1)
            machine.persisted_ledger_agrees()
    finally:
        machine.teardown()


@pytest.mark.parametrize("reason", ["BREAKDOWN", "SAFETY", "DISCHARGE_DELAY", "LOAD_UNCERTAIN"])
def test_reported_issue_history_preserves_cargo_and_held_work(migrated_database_url, reason):
    machine = FleetHistory(migrated_database_url)
    try:
        machine.recover(0, True, reason)
        machine.persisted_ledger_agrees()
        machine.booking(True)
        machine.optimize(False)
        machine.persisted_ledger_agrees()
    finally:
        machine.teardown()


def test_generated_history_teardown_clears_adopted_fleet_facts(migrated_database_url):
    machine = FleetHistory(migrated_database_url)
    factory = machine.db.factory
    machine.teardown()
    import asyncio

    async def empty():
        async with factory() as session:
            assert await session.scalar(select(FleetControl.scope)) is None
            assert await session.scalar(select(CollectionRequest.request_id)) is None

    asyncio.run(empty())
