"""Disposable PostgreSQL regressions for the commercial preparation protocol."""

import asyncio
import os
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import pytest
from alembic import command as migrations
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import DBAPIError
from test_operational_orchestration import admission, regional, reserve
from test_payment_preparation import assert_readiness_schema
from test_routing_fleet import LineMatrix, engines

from tirodhan.core.config import Settings
from tirodhan.db.values import new_uuid7
from tirodhan.modules.planning.routing.execution import RoutingAdapterError
from tirodhan.modules.routing_operations import admission as admission_module
from tirodhan.modules.routing_operations.admission_jobs import request_admission, run_admissions
from tirodhan.modules.routing_operations.admission_leases import claim, fence, release
from tirodhan.modules.routing_operations.admission_models import (
    AdmissionAllocation,
    AdmissionObligation,
    AdmissionRouteCertificate,
    AdmissionRouteMembership,
    AdmissionSourceHead,
    AdmissionSourceNotice,
)
from tirodhan.modules.routing_operations.admission_queries import in_ids
from tirodhan.modules.routing_operations.admission_state import read_cache
from tirodhan.modules.routing_operations.admission_store import load_certificate, notice_stamp
from tirodhan.modules.routing_operations.commands import OperationalInputError
from tirodhan.modules.routing_operations.models import OperatingVehicle
from tirodhan.modules.routing_operations.orchestration_models import CapacityReservation
from tirodhan.workers.routing_admission import execute
from tirodhan.workers.routing_regional import LocalRoadProfile

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_source_invalidations_coalesce_and_do_not_lock_an_unrelated_region(
    database_session_factory,
):
    from tirodhan.modules.routing_operations.orchestration_models import OperatingRegion

    factory = database_session_factory
    data = await regional(factory, empty=True)
    async with factory() as session, session.begin():
        original = await session.get(OperatingRegion, data[7])
        other = OperatingRegion(
            name="unrelated-synthetic-region",
            policy=original.policy,
            default_warehouse_id=original.default_warehouse_id,
            recorded_by=original.recorded_by,
        )
        session.add(other)
        await session.flush()
        other_id = other.region_id
        before = await session.scalar(
            select(AdmissionSourceHead.version).where(AdmissionSourceHead.region_id == other_id)
        )
        await session.execute(delete(AdmissionSourceNotice))
    for i in range(10):
        async with factory() as session, session.begin():
            await session.execute(
                update(OperatingVehicle)
                .where(OperatingVehicle.vehicle_id == data[1][0])
                .values(blocked_reason="SAFETY" if i % 2 else None)
            )
    async with factory() as session:
        notices = list(await session.scalars(select(AdmissionSourceNotice)))
        assert len(notices) == 1 and notices[0].region_id == data[7]
        assert notices[0].source_key == str(data[1][0])
        assert (
            await session.scalar(
                select(AdmissionSourceHead.version).where(AdmissionSourceHead.region_id == other_id)
            )
            == before
        )


async def test_serializable_head_fences_read_committed_source_writer(database_session_factory):
    factory = database_session_factory
    data = await regional(factory, empty=True)
    region, vehicle = data[7], data[1][0]
    async with factory() as reader:
        await reader.connection(execution_options={"isolation_level": "SERIALIZABLE"})
        before = await reader.scalar(
            select(AdmissionSourceHead.version).where(AdmissionSourceHead.region_id == region)
        )
        async with factory() as writer, writer.begin():
            await writer.execute(
                update(OperatingVehicle)
                .where(OperatingVehicle.vehicle_id == vehicle)
                .values(blocked_reason="SAFETY")
            )
        with pytest.raises(DBAPIError) as failure:
            await reader.scalar(
                select(AdmissionSourceHead)
                .where(AdmissionSourceHead.region_id == region)
                .with_for_update()
            )
        assert getattr(failure.value.orig, "sqlstate", None) == "40001"
        await reader.rollback()
    async with factory() as session:
        assert (
            await session.scalar(
                select(AdmissionSourceHead.version).where(AdmissionSourceHead.region_id == region)
            )
            > before
        )


async def test_publication_head_blocks_later_source_change_until_commit(database_session_factory):
    factory = database_session_factory
    data = await regional(factory, empty=True)
    async with factory() as publisher, publisher.begin():
        await publisher.scalar(
            select(AdmissionSourceHead)
            .where(AdmissionSourceHead.region_id == data[7])
            .with_for_update()
        )
        async with factory() as writer:
            await writer.execute(text("SET LOCAL lock_timeout='200ms'"))
            with pytest.raises(DBAPIError) as failure:
                await writer.execute(
                    update(OperatingVehicle)
                    .where(OperatingVehicle.vehicle_id == data[1][0])
                    .values(blocked_reason="SAFETY")
                )
            assert getattr(failure.value.orig, "sqlstate", None) == "55P03"
            await writer.rollback()
    async with factory() as writer, writer.begin():
        await writer.execute(
            update(OperatingVehicle)
            .where(OperatingVehicle.vehicle_id == data[1][0])
            .values(blocked_reason="SAFETY")
        )


async def test_late_commit_with_smaller_notice_identity_cannot_be_acknowledged_away(
    database_session_factory,
):
    factory = database_session_factory
    data = await regional(factory, empty=True)
    region = data[7]
    async with factory() as session, session.begin():
        await session.execute(delete(AdmissionSourceNotice))
    async with factory() as delayed:
        delayed.add(
            AdmissionSourceNotice(
                notice_id=UUID(int=1),
                region_id=region,
                source_table="collection_request",
                source_key=str(new_uuid7()),
            )
        )
        await delayed.flush()
        async with factory() as later, later.begin():
            later.add(
                AdmissionSourceNotice(
                    notice_id=UUID(int=2),
                    region_id=region,
                    source_table="collection_request",
                    source_key=str(new_uuid7()),
                )
            )
        async with factory() as publisher:
            await publisher.connection(execution_options={"isolation_level": "SERIALIZABLE"})
            visible = await notice_stamp(publisher, region)
            await publisher.execute(
                delete(AdmissionSourceNotice).where(AdmissionSourceNotice.region_id == region)
            )
            await publisher.commit()
        await delayed.commit()
    async with factory() as session:
        assert await notice_stamp(session, region) != visible
        assert list(await session.scalars(select(AdmissionSourceNotice.notice_id))) == [UUID(int=1)]


async def test_regional_lease_spans_intents_and_expired_owner_cannot_publish(
    database_session_factory,
):
    factory = database_session_factory
    data = await regional(factory, empty=True)
    now, region = data[6][0], data[7]
    first, second = new_uuid7(), new_uuid7()
    async with factory() as session, session.begin():
        assert await claim(session, region, first, now, now + timedelta(seconds=60))
        assert await claim(session, region, first, now, now + timedelta(seconds=120))
        assert not await claim(session, region, second, now, now + timedelta(seconds=60))
    await release(factory, first)
    async with factory() as session, session.begin():
        assert await claim(session, region, second, now, now + timedelta(seconds=60))
        with pytest.raises(OperationalInputError, match="ADMISSION_REGIONAL_LEASE_LOST"):
            await fence(session, region, first, now)


async def test_shared_history_is_immutable_and_readable_at_every_revision(database_session_factory):
    factory = database_session_factory
    data = await regional(factory, empty=True)
    customer, context, now = data[8], data[9], data[6][0]
    commands = [admission(context, now) for _ in range(3)]
    for command in commands:
        await reserve(factory, customer, command, now)
    async with factory() as session:
        reservations = list(
            await session.scalars(
                select(CapacityReservation).order_by(CapacityReservation.region_version)
            )
        )
        for count, row in enumerate(reservations, 1):
            proposal = await load_certificate(session, row)
            assert sum(len(r.stop_ids) for r in proposal.routes) == count * 2
            assert "proposal" not in row.certificate
        assert await session.scalar(select(func.count()).select_from(AdmissionObligation)) == 3
        assert await session.scalar(select(func.count()).select_from(AdmissionAllocation)) == 3
        certificate = await session.scalar(select(AdmissionRouteCertificate))
        member = await session.scalar(
            select(AdmissionRouteMembership).where(
                AdmissionRouteMembership.to_revision.is_not(None)
            )
        )
    for table, predicate, values in (
        (
            AdmissionRouteCertificate,
            AdmissionRouteCertificate.certificate_id == certificate.certificate_id,
            {"fingerprint": b"changed"},
        ),
        (
            AdmissionRouteMembership,
            AdmissionRouteMembership.from_revision == member.from_revision,
            {"to_revision": member.to_revision + 1},
        ),
    ):
        async with factory() as session:
            with pytest.raises(DBAPIError, match="immutable|closed once"):
                await session.execute(update(table).where(predicate).values(**values))
            await session.rollback()
    with pytest.raises(DBAPIError, match="requires export/drain"):
        await asyncio.to_thread(
            migrations.downgrade, Config("alembic.ini"), "0029_admission_retry_schedule"
        )
    async with factory() as session:
        await (await session.connection()).run_sync(assert_readiness_schema)
    cache = read_cache(factory)
    assert cache.hits >= 2
    assert cache.bytes <= cache.max_bytes


async def test_identity_array_crosses_asyncpg_bind_limit(database_session_factory):
    factory = database_session_factory
    data = await regional(factory, empty=True)
    identities = [new_uuid7() for _ in range(33000)] + [data[1][0]]
    async with factory() as session:
        assert list(
            await session.scalars(
                select(OperatingVehicle.vehicle_id).where(
                    in_ids(OperatingVehicle.vehicle_id, identities)
                )
            )
        ) == [data[1][0]]


async def test_cursor_survives_quantum_yield_cold_reconstruction_and_elapsed_clock(
    database_session_factory,
    monkeypatch,
):
    factory = database_session_factory
    data = await regional(factory, empty=True)
    clock, customer, context = data[6], data[8], data[9]
    command = admission(context, clock[0])
    await request_admission(
        factory,
        customer_id=customer,
        command=command,
        expires_at=clock[0] + timedelta(hours=1),
        now=clock[0],
    )
    original = admission_module.insert_obligation
    attempts = []

    def quantum(*args, **kwargs):
        progress = kwargs["progress"]
        attempts.append(dict(progress))
        if len(attempts) == 1:
            progress.update(vehicle=str(data[1][0]), pickup=0, discharge=0)
            raise RoutingAdapterError("UNKNOWN_SEARCH_QUANTUM")
        assert progress == {"vehicle": str(data[1][0]), "pickup": 0, "discharge": 0}
        return original(*args, **kwargs)

    monkeypatch.setattr(admission_module, "insert_obligation", quantum)
    matrix = LineMatrix()

    async def work():
        return await run_admissions(
            factory,
            providers={"test-car": matrix},
            optimizers=engines(),
            clock=lambda: clock[0],
            limit=1,
        )

    assert await work() == 1
    cache = read_cache(factory)
    cache.states.clear()
    cache.bytes = 0
    clock[0] += timedelta(seconds=5)
    assert await work() == 1
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(CapacityReservation)) == 1
    assert len(attempts) == 2


async def test_two_yielded_intents_resume_their_own_checkpoint(
    database_session_factory, monkeypatch
):
    factory = database_session_factory
    data = await regional(factory, empty=True)
    clock, customer, context = data[6], data[8], data[9]
    commands = [admission(context, clock[0]) for _ in range(2)]
    for command in commands:
        await request_admission(
            factory,
            customer_id=customer,
            command=command,
            expires_at=clock[0] + timedelta(hours=1),
            now=clock[0],
        )
    original = admission_module.insert_obligation
    seen = {}

    def quantum(problem, seed, lot, **kwargs):
        progress = kwargs["progress"]
        if seen.get(lot, 0):
            assert progress == {"owner": str(lot)}, "another intent discarded this checkpoint"
        else:
            assert not progress
        seen[lot] = seen.get(lot, 0) + 1
        progress["owner"] = str(lot)
        raise RoutingAdapterError("UNKNOWN_SEARCH_QUANTUM")

    monkeypatch.setattr(admission_module, "insert_obligation", quantum)
    matrix = LineMatrix()

    async def work():
        return await run_admissions(
            factory,
            providers={"test-car": matrix},
            optimizers=engines(),
            clock=lambda: clock[0],
            limit=2,
        )

    assert await work() == 2
    clock[0] += timedelta(seconds=5)
    assert await work() == 2
    assert set(seen.values()) == {2}
    monkeypatch.setattr(admission_module, "insert_obligation", original)
    clock[0] += timedelta(seconds=10)
    assert await work() == 2
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(CapacityReservation)) == 2


async def test_finite_worker_without_work_never_loads_a_graph(
    database_session_factory,
    migrated_database_url,
):
    data = await regional(database_session_factory, empty=True)
    profile = LocalRoadProfile(
        profile_version="test-car",
        graph_version="absent",
        graph_path=Path("/deliberately-missing-graph.osrm"),
        port=5002,
        threads=1,
        max_snap_distance_m=300,
        block_nodes=32,
        max_pairs=1024,
    )
    settings = Settings(
        environment="test",
        database_url=SecretStr(migrated_database_url),
        routing_orchestration_enabled=True,
    )
    assert not settings.routing_dispatch_enabled
    with pytest.raises(OperationalInputError, match="COMMERCIAL_ACCEPTANCE_RETIRED"):
        await execute(data[7], [profile], settings, max_intents=10000, max_wall_seconds=1)


@pytest.mark.routing_engine
@pytest.mark.skipif(
    os.getenv("TIRODHAN_ROUTING_ENGINE_TESTS") != "1", reason="explicit real native runtime opt-in"
)
async def test_retired_worker_does_not_process_queued_commercial_intents(
    database_session_factory,
    migrated_database_url,
):
    from test_commercial_admission_benchmark import population

    from tirodhan.modules.routing_operations.commands import OperationalInputError
    from tirodhan.modules.routing_operations.dispatch_models import VehicleRoute
    from tirodhan.modules.routing_operations.orchestration_models import CapacityAdmissionRequest

    factory = database_session_factory
    inputs, now = await population(factory, 3, adopted=True)
    for customer, command in inputs:
        await request_admission(
            factory,
            customer_id=customer,
            command=command,
            expires_at=now + timedelta(hours=1),
            now=now,
        )
    async with factory() as session:
        region = await session.scalar(select(CapacityAdmissionRequest.region_id).limit(1))
    settings = Settings(
        environment="test",
        database_url=SecretStr(migrated_database_url),
        routing_orchestration_enabled=True,
    )
    profile = LocalRoadProfile(
        profile_version="test-car",
        graph_version="fixture-car",
        graph_path=Path("/graphs/car/map.osrm"),
        port=5002,
        threads=1,
        max_snap_distance_m=300,
        block_nodes=32,
        max_pairs=1024,
    )
    with pytest.raises(OperationalInputError, match="COMMERCIAL_ACCEPTANCE_RETIRED"):
        await execute(region, [profile], settings)
    async with factory() as session:
        rows = list(await session.scalars(select(CapacityAdmissionRequest)))
        assert rows and all(row.status == "PENDING" for row in rows)
        assert await session.scalar(select(func.count()).select_from(CapacityReservation)) == 0
        assert await session.scalar(select(func.count()).select_from(VehicleRoute)) == 0
