"""Real PostgreSQL admission safety, retry and populated migration regressions."""

import asyncio
from datetime import timedelta

import pytest
from alembic import command as migrations
from alembic.config import Config
from legacy_financial_fixture import pre_administration_writer
from sqlalchemy import func, select, text
from test_operational_orchestration import admission, regional, reserve
from test_payment_preparation import assert_readiness_schema
from test_routing_fleet import LineMatrix, engines

from tirodhan.db.values import new_uuid7
from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.planning.routing.execution import RoutingAdapterError
from tirodhan.modules.routing_operations import admission_jobs
from tirodhan.modules.routing_operations.admission_jobs import request_admission, run_admissions
from tirodhan.modules.routing_operations.commands import OperationalInputError
from tirodhan.modules.routing_operations.orchestration_models import (
    CapacityAdmissionRequest,
    CapacityReservation,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.mark.parametrize("failure", ["INVALID_ROAD_MATRIX", "MATRIX_TIMEOUT", "MATRIX_UNAVAILABLE"])
async def test_provider_outage_is_durable_backoff_then_one_certificate(
    database_session_factory, failure
):
    factory = database_session_factory
    data = await regional(factory, empty=True)
    clock, customer, context = data[6], data[8], data[9]
    command = admission(context, clock[0] + timedelta(minutes=10))
    kwargs = dict(
        factory=factory,
        customer_id=customer,
        command=command,
        expires_at=clock[0] + timedelta(hours=1),
        now=clock[0],
    )
    await request_admission(**kwargs)

    class Fails:
        calls = 0

        async def acquire(self, nodes):
            self.calls += 1
            raise RoutingAdapterError(failure)

    matrix = Fails()

    async def work(source):
        return await run_admissions(
            factory, providers={"test-car": source}, optimizers=engines(), clock=lambda: clock[0]
        )

    assert await work(matrix) == 1
    assert await work(matrix) == 0  # No hot loop or premature terminal rejection.
    assert matrix.calls == 1
    async with factory() as session:
        row = await session.get(CapacityAdmissionRequest, command.client_command_id)
        assert row.status == "PENDING" and row.failure_code == failure
        assert row.next_attempt_at == clock[0] + timedelta(seconds=5)
        assert await session.scalar(select(func.count()).select_from(CapacityReservation)) == 0
    clock[0] += timedelta(seconds=5)
    assert await work(LineMatrix()) == 1
    assert await work(LineMatrix()) == 0
    assert (await request_admission(**kwargs)).admission_id == command.client_command_id
    async with factory() as session:
        row = await session.get(CapacityAdmissionRequest, command.client_command_id)
        assert row.status == "SUCCEEDED" and row.attempts == 2
        assert row.next_attempt_at is None
        assert await session.scalar(select(func.count()).select_from(CapacityReservation)) == 1


@pytest.mark.parametrize("after_expiry", [False, True])
async def test_lost_response_after_certificate_commit_recovers_same_identity(
    database_session_factory, monkeypatch, after_expiry
):
    factory = database_session_factory
    data = await regional(factory, empty=True)
    clock, customer, context = data[6], data[8], data[9]
    command = admission(context, clock[0] + timedelta(minutes=10))
    await request_admission(
        factory,
        customer_id=customer,
        command=command,
        expires_at=clock[0] + timedelta(hours=1),
        now=clock[0],
    )
    original = admission_jobs.reserve_capacity

    async def crash(*args, **kwargs):
        await original(*args, **kwargs)
        raise asyncio.CancelledError()

    monkeypatch.setattr(admission_jobs, "reserve_capacity", crash)

    async def work():
        return await run_admissions(
            factory,
            providers={"test-car": LineMatrix()},
            optimizers=engines(),
            clock=lambda: clock[0],
        )

    with pytest.raises(asyncio.CancelledError):
        await work()
    async with factory() as session:
        row = await session.get(CapacityAdmissionRequest, command.client_command_id)
        assert row.status == "RUNNING"
        assert await session.scalar(select(func.count()).select_from(CapacityReservation)) == 1
    assert await work() == 0  # Live owner cannot be stolen.
    clock[0] += timedelta(seconds=3601 if after_expiry else 301)
    monkeypatch.setattr(admission_jobs, "reserve_capacity", original)
    assert await work() == (0 if after_expiry else 1)
    async with factory() as session:
        row = await session.get(CapacityAdmissionRequest, command.client_command_id)
        assert row.status == "SUCCEEDED"
        assert await session.scalar(select(func.count()).select_from(CapacityReservation)) == 1


async def test_cancellation_during_roads_invalidates_certificate_without_losing_work(
    database_session_factory,
):
    from tirodhan.modules.collection_requests.cancellation import (
        cancel_collection_request_by_customer,
    )

    factory = database_session_factory
    data = await regional(factory)
    clock, customer, context = data[6], data[8], data[9]
    command = admission(context, clock[0])
    entered, release = asyncio.Event(), asyncio.Event()

    class Paused(LineMatrix):
        async def acquire(self, nodes):
            entered.set()
            await release.wait()
            return await super().acquire(nodes)

    task = asyncio.create_task(reserve(factory, customer, command, clock[0], matrix=Paused()))
    await asyncio.wait_for(entered.wait(), 10)
    try:
        async with factory() as session, session.begin():
            await cancel_collection_request_by_customer(
                session,
                request_id=data[3][0],
                customer_id=customer,
                idempotency_key=str(new_uuid7()),
                planning_lead_time_minutes=15,
                idempotency_expires_at=clock[0] + timedelta(days=1),
            )
    finally:
        release.set()
    with pytest.raises(OperationalInputError, match="CAPACITY_PROJECTION_STALE_RETRY"):
        await task
    assert await reserve(factory, customer, command, clock[0]) == command.client_command_id
    async with factory() as session:
        assert (await session.get(CollectionRequest, data[3][0])).status == "CANCELLED"
        assert (await session.get(CollectionRequest, data[3][1])).status == "ACCEPTED"
        assert await session.scalar(select(func.count()).select_from(CapacityReservation)) == 1


async def test_0029_roundtrip_preserves_intents_certificates_and_financial_history(
    database_session_factory,
    monkeypatch,
):
    factory = database_session_factory
    async with pre_administration_writer(factory):
        data = await regional(factory)
    command = admission(data[9], data[6][0])
    await request_admission(
        factory,
        customer_id=data[8],
        command=command,
        expires_at=data[6][0] + timedelta(hours=1),
        now=data[6][0],
    )
    # Simulate the pre-0030 writer. New shared proof histories intentionally
    # refuse destructive downgrade; this test continues covering the legacy format.
    from tirodhan.modules.routing_operations import admission as admission_module
    from tirodhan.modules.routing_operations.service import PROPOSAL_CODEC

    def legacy(proposal, revision, incremental, safety_percent):
        return {
            "region_version": revision,
            "proposal": PROPOSAL_CODEC.dump_python(proposal, mode="json"),
            "incremental": incremental,
            "safety_percent": safety_percent,
        }

    async def legacy_persist(*args, **kwargs):
        return None

    monkeypatch.setattr(admission_module, "certificate_document", legacy)
    monkeypatch.setattr(admission_module, "persist_certificate", legacy_persist)
    async with pre_administration_writer(factory):
        await reserve(factory, data[8], command, data[6][0])

    async def facts():
        async with factory() as session:
            return (
                await session.execute(
                    text(
                        "SELECT (SELECT jsonb_agg(to_jsonb(p) ORDER BY payment_id) FROM payment p),"
                        "(SELECT jsonb_agg(to_jsonb(a) ORDER BY admission_id) "
                        "FROM capacity_admission_request a),"
                        "(SELECT jsonb_agg(to_jsonb(r) ORDER BY reservation_id) "
                        "FROM regional_capacity_reservation r)"
                    )
                )
            ).one()

    before = await facts()
    try:
        await asyncio.to_thread(
            migrations.downgrade, Config("alembic.ini"), "0028_regional_parallel_routing"
        )
    finally:
        await asyncio.to_thread(migrations.upgrade, Config("alembic.ini"), "head")
    assert await facts() == before
    async with factory() as session:
        await (await session.connection()).run_sync(assert_readiness_schema)


async def test_two_admission_workers_contend_without_overselling(database_session_factory):
    factory = database_session_factory
    data = await regional(factory, empty=True)
    clock, customer, context = data[6], data[8], data[9]
    commands = [
        admission(
            context,
            clock[0],
            mass_g=10000,
            volume_ml=10000,
            pickup_seconds=1801,
            discharge_seconds=10,
        )
        for _ in range(2)
    ]
    await asyncio.gather(
        *(
            request_admission(
                factory,
                customer_id=customer,
                command=c,
                expires_at=clock[0] + timedelta(hours=1),
                now=clock[0],
            )
            for c in commands
        )
    )
    # Exact concurrent customer replay must not create a third intent.
    await request_admission(
        factory,
        customer_id=customer,
        command=commands[0],
        expires_at=clock[0] + timedelta(hours=1),
        now=clock[0],
    )
    arrived, release = asyncio.Event(), asyncio.Event()

    class Barrier(LineMatrix):
        calls = 0

        async def acquire(self, nodes):
            self.calls += 1
            arrived.set()
            await release.wait()
            return await super().acquire(nodes)

    matrix = Barrier()

    async def work(source):
        return await run_admissions(
            factory,
            providers={"test-car": source},
            optimizers=engines(),
            limit=1,
            clock=lambda: clock[0],
        )

    first = asyncio.create_task(work(matrix))
    try:
        await asyncio.wait_for(arrived.wait(), 10)
        assert await work(matrix) == 0  # Same region is owned without holding a transaction.
    finally:
        release.set()
    assert await first == 1
    async with factory() as session:
        rows = list(await session.scalars(select(CapacityAdmissionRequest)))
        assert sorted(r.status for r in rows) == ["PENDING", "SUCCEEDED"]
        assert await session.scalar(select(func.count()).select_from(CapacityReservation)) == 1
    clock[0] += timedelta(seconds=5)
    assert await work(LineMatrix()) == 1
    async with factory() as session:
        rows = list(await session.scalars(select(CapacityAdmissionRequest)))
        assert sorted(r.status for r in rows) == ["PENDING", "SUCCEEDED"]
        pending = next(r for r in rows if r.status == "PENDING")
        assert pending.failure_code == "UNKNOWN_SEARCH_EXHAUSTED"
        assert await session.scalar(select(func.count()).select_from(CapacityReservation)) == 1


async def test_repeated_technical_conflicts_remain_retryable_until_intent_expiry(
    database_session_factory, monkeypatch
):
    factory = database_session_factory
    data = await regional(factory, empty=True)
    clock, customer, context = data[6], data[8], data[9]
    command = admission(context, clock[0])
    expires = clock[0] + timedelta(minutes=10)
    await request_admission(
        factory, customer_id=customer, command=command, expires_at=expires, now=clock[0]
    )

    async def stale(*args, **kwargs):
        raise OperationalInputError("CAPACITY_PROJECTION_STALE_RETRY")

    monkeypatch.setattr(admission_jobs, "reserve_capacity", stale)
    for _ in range(5):
        assert (
            await run_admissions(factory, providers={}, optimizers={}, clock=lambda: clock[0]) == 1
        )
        async with factory() as session:
            row = await session.get(CapacityAdmissionRequest, command.client_command_id)
            assert row.status == "PENDING"
            clock[0] = row.next_attempt_at
    clock[0] = expires
    assert await run_admissions(factory, providers={}, optimizers={}, clock=lambda: clock[0]) == 0
    async with factory() as session:
        row = await session.get(CapacityAdmissionRequest, command.client_command_id)
        assert row.status == "FAILED" and row.failure_code == "ADMISSION_EXPIRED"


async def test_bound_paid_booking_reuses_certificate_without_reoptimizing_incumbents(
    database_session_factory,
):
    from test_commercial_admission_benchmark import population, purchase

    factory = database_session_factory
    inputs, now = await population(factory, 2)
    solvers = engines()
    for customer, command in inputs:
        result = await reserve(factory, customer, command, now, solvers=solvers)
        assert result == command.client_command_id
        assert sum(s.calls for s in solvers.values()) == 0
        booked = await purchase(factory, customer, command, now)
        async with factory() as session:
            assert (
                await session.get(CollectionRequest, booked.request.request_id)
            ).status == "ACCEPTED"
            certificate = await session.get(CapacityReservation, result)
            assert certificate.certificate["incremental"] is True
    async with factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(CollectionRequest)
                .where(CollectionRequest.status == "ACCEPTED")
            )
            == 2
        )


async def test_slot_elapsed_during_retry_is_terminal_without_a_capacity_promise(
    database_session_factory,
):
    factory = database_session_factory
    data = await regional(factory, empty=True)
    clock, customer, context = data[6], data[8], data[9]
    command = admission(context, clock[0])
    await request_admission(
        factory,
        customer_id=customer,
        command=command,
        expires_at=clock[0] + timedelta(hours=2),
        now=clock[0],
    )
    clock[0] = command.slot_start
    assert await run_admissions(factory, providers={}, optimizers={}, clock=lambda: clock[0]) == 1
    async with factory() as session:
        row = await session.get(CapacityAdmissionRequest, command.client_command_id)
        assert row.status == "FAILED" and row.failure_code == "SLOT_UNAVAILABLE"
        assert await session.scalar(select(func.count()).select_from(CapacityReservation)) == 0


async def test_legacy_neighbourhood_count_does_not_cap_commercial_admission(
    database_session_factory,
):
    from tirodhan.modules.routing_operations.fleet_models import FleetControl

    factory = database_session_factory
    data = await regional(factory, empty=True)
    now, customer, context = data[6][0], data[8], data[9]
    async with factory() as session, session.begin():
        control = await session.get(FleetControl, "COMMITTED_FLEET")
        control.configuration = dict(control.configuration, max_requests=1)
    source = LineMatrix()
    first = admission(context, now)
    await reserve(factory, customer, first, now, matrix=source)
    calls = source.calls
    second = admission(context, now)
    assert await reserve(factory, customer, second, now, matrix=source) == second.client_command_id
    assert source.calls > calls
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(CapacityReservation)) == 2
        assert await session.get(CapacityReservation, first.client_command_id) is not None


async def test_independent_regions_prepare_concurrently_and_unique_owner_is_enforced(
    database_session_factory, monkeypatch
):
    from sqlalchemy.exc import IntegrityError

    from tirodhan.modules.routing_operations.orchestration_models import OperatingRegion, RegionCell
    from tirodhan.modules.serviceability.models import ServiceabilityContext

    factory = database_session_factory
    data = await regional(factory, empty=True)
    now, customer, context = data[6][0], data[8], data[9]
    async with factory() as session, session.begin():
        old = await session.get(OperatingRegion, data[7])
        second = OperatingRegion(
            name="independent-admission-region",
            default_warehouse_id=old.default_warehouse_id,
            policy=old.policy,
            recorded_by=old.recorded_by,
            created_at=now,
        )
        session.add(second)
        await session.flush()
        session.add(RegionCell(cell_id="synthetic-separate-cell", region_id=second.region_id))
        original = await session.get(ServiceabilityContext, context)
        other = ServiceabilityContext(
            user_id=customer,
            address_snapshot_encrypted=original.address_snapshot_encrypted,
            location=original.location,
            cell_id="synthetic-separate-cell",
            status="SERVICEABLE",
            created_at=now,
            expires_at=now + timedelta(hours=1),
        )
        session.add(other)
        await session.flush()
    commands = [
        admission(c, now + timedelta(minutes=10))
        for c in (context, other.serviceability_context_id)
    ]
    for c in commands:
        await request_admission(
            factory, customer_id=customer, command=c, expires_at=now + timedelta(hours=1), now=now
        )
    calls, arrived, release = [], asyncio.Event(), asyncio.Event()

    async def pause(*args, **kwargs):
        calls.append(kwargs["command"].client_command_id)
        if len(calls) == 2:
            arrived.set()
        await release.wait()
        raise RoutingAdapterError("MATRIX_TIMEOUT")

    monkeypatch.setattr(admission_jobs, "reserve_capacity", pause)

    async def worker():
        return await run_admissions(
            factory, providers={}, optimizers={}, limit=1, clock=lambda: now
        )

    first = asyncio.create_task(worker())
    # Let the first claim commit; the second must skip its region and reach the other.
    for _ in range(100):
        if calls:
            break
        await asyncio.sleep(0.01)
    second_task = asyncio.create_task(worker())
    try:
        await asyncio.wait_for(arrived.wait(), 10)
        extra = admission(context, now + timedelta(minutes=10))
        await request_admission(
            factory,
            customer_id=customer,
            command=extra,
            expires_at=now + timedelta(hours=1),
            now=now,
        )
        with pytest.raises(IntegrityError, match="uq_capacity_admission_region_running"):
            async with factory() as session, session.begin():
                row = await session.get(CapacityAdmissionRequest, extra.client_command_id)
                row.status = "RUNNING"
                row.lease_token = new_uuid7()
                row.lease_until = now + timedelta(seconds=300)
    finally:
        release.set()
    assert await asyncio.gather(first, second_task) == [1, 1]


async def test_migration_refuses_to_revoke_old_overlapping_owners(database_session_factory):
    from sqlalchemy.exc import DBAPIError

    factory = database_session_factory
    async with pre_administration_writer(factory):
        data = await regional(factory, empty=True)
    now, customer, context = data[6][0], data[8], data[9]
    for _ in range(2):
        await request_admission(
            factory,
            customer_id=customer,
            command=admission(context, now),
            expires_at=now + timedelta(hours=1),
            now=now,
        )
    await asyncio.to_thread(
        migrations.downgrade, Config("alembic.ini"), "0028_regional_parallel_routing"
    )
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE capacity_admission_request SET status='RUNNING',"
                    "lease_token=admission_id,lease_until=expires_at"
                )
            )
        with pytest.raises(DBAPIError, match="drain regional admission workers"):
            await asyncio.to_thread(migrations.upgrade, Config("alembic.ini"), "head")
        async with factory() as session:
            assert (
                await session.scalar(text("SELECT version_num FROM alembic_version"))
                == "0028_regional_parallel_routing"
            )
            assert (
                await session.scalar(
                    text("SELECT count(*) FROM capacity_admission_request WHERE status='RUNNING'")
                )
                == 2
            )
    finally:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE capacity_admission_request SET status='PENDING',"
                    "lease_token=NULL,lease_until=NULL"
                )
            )
        await asyncio.to_thread(migrations.upgrade, Config("alembic.ini"), "head")


async def test_legacy_nullable_region_is_resolved_before_claim(database_session_factory):
    factory = database_session_factory
    data = await regional(factory, empty=True)
    now, customer, context = data[6][0], data[8], data[9]
    command = admission(context, now + timedelta(minutes=10))
    await request_admission(
        factory, customer_id=customer, command=command, expires_at=now + timedelta(hours=1), now=now
    )
    async with factory() as session, session.begin():
        row = await session.get(CapacityAdmissionRequest, command.client_command_id)
        row.region_id = None
    seen = []

    class CheckOwner(LineMatrix):
        async def acquire(self, nodes):
            async with factory() as session:
                row = await session.get(CapacityAdmissionRequest, command.client_command_id)
                seen.append((row.region_id, row.status))
            return await super().acquire(nodes)

    assert (
        await run_admissions(
            factory, providers={"test-car": CheckOwner()}, optimizers=engines(), clock=lambda: now
        )
        == 1
    )
    assert seen == [(data[7], "RUNNING")]
