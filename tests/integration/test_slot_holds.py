"""Forecast inventory is the commercial gate; no per-booking routing search."""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError
from test_commercial_admission_benchmark import population, purchase
from test_operational_orchestration import operational

from tirodhan.db.values import new_uuid7
from tirodhan.modules.routing_operations.allowance_estimator import AllowancePolicy
from tirodhan.modules.routing_operations.capacity_models import (
    RegionalCapacityBudget,
    RegionalSlotProjection,
    SlotReservationDebit,
)
from tirodhan.modules.routing_operations.commands import OperationalInputError
from tirodhan.modules.routing_operations.operational_commands import ConfigureSlotAllowance
from tirodhan.modules.routing_operations.orchestration_models import (
    CapacityReservation,
    OperatingRegion,
)
from tirodhan.modules.routing_operations.slot_holds import HoldSlot, hold_slot
from tirodhan.modules.routing_operations.slot_projection import read_slots, refresh_slots

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def policy(version=1):
    # Synthetic assumptions, not Delhi production calibration.
    return AllowancePolicy(
        version=version,
        local_travel_seconds=120,
        clustered_travel_seconds=60,
        facility_travel_seconds=1500,
        stem_seconds=0,
        cluster_threshold=2,
        pickup_start_offset_seconds=900,
        limited_bookings=1,
    )


async def configured(factory, monkeypatch, count=4):
    monkeypatch.setenv("TIRODHAN_ADMISSION_VEHICLES", "1")
    inputs, now = await population(factory, count)
    async with factory() as session:
        region = await session.scalar(select(OperatingRegion))
    await operational(
        factory,
        region.recorded_by,
        ConfigureSlotAllowance(
            client_command_id=new_uuid7(),
            region_id=region.region_id,
            expected_version=region.version,
            policy=policy(),
        ),
        now,
    )
    await refresh_slots(factory, region.region_id, now=now)
    return inputs, now, region.region_id


async def command_for(factory, region, command, **changes):
    command = command.model_copy(update=changes)
    async with factory() as session:
        row = await session.get(RegionalSlotProjection, (region, command.slot_start))
    return HoldSlot(**command.model_dump(), expected_generation=row.generation)


async def test_hold_replay_and_paid_booking_do_not_invoke_routing(
    database_session_factory, monkeypatch
):
    from tirodhan.modules.collection_requests.models import CollectionRequest
    from tirodhan.modules.planning.routing.adapters import ORToolsOptimizer, VroomOptimizer
    from tirodhan.modules.planning.routing.osrm import OSRMMatrixProvider
    from tirodhan.modules.routing_operations import admission

    async def forbidden(*args, **kwargs):
        pytest.fail("commercial checkout invoked road acquisition or feasibility search")

    monkeypatch.setattr(admission, "reserve_capacity", forbidden)
    monkeypatch.setattr(OSRMMatrixProvider, "acquire", forbidden)
    monkeypatch.setattr(VroomOptimizer, "propose", forbidden)
    monkeypatch.setattr(ORToolsOptimizer, "propose", forbidden)
    factory = database_session_factory
    inputs, now, region = await configured(factory, monkeypatch)
    customer, original = inputs[0]
    command = await command_for(factory, region, original)
    identity = await hold_slot(
        factory, customer_id=customer, command=command, expires_at=now + timedelta(hours=1), now=now
    )
    assert (
        await hold_slot(
            factory,
            customer_id=customer,
            command=command,
            expires_at=now + timedelta(hours=2),
            now=now,
        )
        == identity
    )
    async with factory() as session:
        row = await session.get(CapacityReservation, identity)
        assert row.admission_basis == "PROJECTED_ALLOWANCE" and row.certificate is None
        assert (
            await session.scalar(
                select(func.sum(SlotReservationDebit.seconds)).where(
                    SlotReservationDebit.reservation_id == identity,
                    SlotReservationDebit.kind == "DUTY",
                )
            )
            == row.estimated_duty_seconds
        )
    # Binding/payment do not depend on projection freshness after acquisition.
    await purchase(factory, customer, command, now)
    async with factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(CollectionRequest)
                .where(CollectionRequest.status == "ACCEPTED")
            )
            == 1
        )
    await refresh_slots(factory, region, now=now)
    async with factory() as session:
        p = await session.get(RegionalSlotProjection, (region, command.slot_start))
        assert p.bookings == 1 and p.holds == 0 and p.remaining_bookings == 0


async def test_concurrent_last_allowance_has_one_winner(database_session_factory, monkeypatch):
    factory = database_session_factory
    inputs, now, region = await configured(factory, monkeypatch, count=8)
    start = inputs[0][1].slot_start
    commands = [
        (
            customer,
            await command_for(
                factory, region, c, slot_start=start, slot_end=start + timedelta(minutes=30)
            ),
        )
        for customer, c in inputs
    ]
    results = await asyncio.gather(
        *(
            hold_slot(
                factory, customer_id=c, command=q, expires_at=now + timedelta(hours=1), now=now
            )
            for c, q in commands
        ),
        return_exceptions=True,
    )
    assert sum(not isinstance(r, Exception) for r in results) == 1, results
    assert all(isinstance(r, OperationalInputError) for r in results if isinstance(r, Exception))
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(CapacityReservation)) == 1
        assert all(
            b.used_seconds <= b.capacity_seconds
            for b in await session.scalars(select(RegionalCapacityBudget))
        )


async def test_adjacent_slots_cannot_spend_same_workload_budget(
    database_session_factory, monkeypatch
):
    factory = database_session_factory
    inputs, now, region = await configured(factory, monkeypatch)
    commands = [(c, await command_for(factory, region, q)) for c, q in inputs[:2]]
    results = await asyncio.gather(
        *(
            hold_slot(
                factory, customer_id=c, command=q, expires_at=now + timedelta(hours=1), now=now
            )
            for c, q in commands
        ),
        return_exceptions=True,
    )
    assert sum(not isinstance(r, Exception) for r in results) == 1, results


async def test_expiry_refresh_releases_allowance_and_keeps_audit(
    database_session_factory, monkeypatch
):
    factory = database_session_factory
    inputs, now, region = await configured(factory, monkeypatch)
    customer, c = inputs[0]
    command = await command_for(factory, region, c)
    identity = await hold_slot(
        factory,
        customer_id=customer,
        command=command,
        expires_at=now + timedelta(seconds=20),
        now=now,
    )
    later = now + timedelta(seconds=21)
    async with factory() as session:
        assert all(s["status"] == "UNKNOWN" for s in await read_slots(session, region, now=later))
    await refresh_slots(factory, region, now=later)
    command = await command_for(factory, region, c, client_command_id=new_uuid7())
    assert await hold_slot(
        factory,
        customer_id=customer,
        command=command,
        expires_at=later + timedelta(hours=1),
        now=later,
    )
    async with factory() as session:
        assert await session.get(CapacityReservation, identity)
        assert await session.scalar(select(func.count()).select_from(CapacityReservation)) == 2


async def test_source_change_blocks_new_claims_but_not_existing_hold_binding(
    database_session_factory, monkeypatch
):
    factory = database_session_factory
    inputs, now, region = await configured(factory, monkeypatch)
    customer, c = inputs[0]
    command = await command_for(factory, region, c)
    await hold_slot(
        factory, customer_id=customer, command=command, expires_at=now + timedelta(hours=1), now=now
    )
    async with factory() as session, session.begin():
        await session.execute(
            update(OperatingRegion)
            .where(OperatingRegion.region_id == region)
            .values(version=OperatingRegion.version + 1)
        )
    c2 = await command_for(factory, region, inputs[1][1])
    with pytest.raises(OperationalInputError, match="PROJECTION"):
        await hold_slot(
            factory,
            customer_id=inputs[1][0],
            command=c2,
            expires_at=now + timedelta(hours=1),
            now=now,
        )
    await purchase(factory, customer, command, now)


async def test_refresh_racing_hold_cannot_overwrite_consumption(
    database_session_factory, monkeypatch
):
    from tirodhan.modules.routing_operations import allowance_projection as module

    factory = database_session_factory
    inputs, now, region = await configured(factory, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    original = module.forecast

    async def paused(*args, **kwargs):
        result = await original(*args, **kwargs)
        entered.set()
        await release.wait()
        return result

    monkeypatch.setattr(module, "forecast", paused)
    task = asyncio.create_task(refresh_slots(factory, region, now=now))
    await asyncio.wait_for(entered.wait(), 10)
    customer, c = inputs[0]
    command = await command_for(factory, region, c)
    await hold_slot(
        factory, customer_id=customer, command=command, expires_at=now + timedelta(hours=1), now=now
    )
    release.set()
    result = await asyncio.gather(task, return_exceptions=True)
    assert isinstance(result[0], DBAPIError), result
    monkeypatch.setattr(module, "forecast", original)
    await refresh_slots(factory, region, now=now)
    async with factory() as session:
        assert (
            await session.get(RegionalSlotProjection, (region, c.slot_start))
        ).remaining_bookings == 0


async def test_unconfigured_projection_cannot_authorize_hold(database_session_factory, monkeypatch):
    monkeypatch.setenv("TIRODHAN_ADMISSION_VEHICLES", "1")
    factory = database_session_factory
    inputs, now = await population(factory, 1)
    async with factory() as session:
        region = await session.scalar(select(OperatingRegion.region_id))
    await refresh_slots(factory, region, now=now)
    customer, c = inputs[0]
    command = await command_for(factory, region, c)
    with pytest.raises(OperationalInputError):
        await hold_slot(
            factory,
            customer_id=customer,
            command=command,
            expires_at=now + timedelta(hours=1),
            now=now,
        )
    async with factory() as session:
        assert all(s["status"] == "UNKNOWN" for s in await read_slots(session, region, now=now))


async def test_http_flow_and_retired_worker_never_prepare_commercial_routes(
    database_session_factory, migrated_database_url, monkeypatch
):
    import httpx
    from test_operational_api import _headers, _token_codec, _token_for

    from tirodhan.core.config import Settings
    from tirodhan.main import create_app
    from tirodhan.modules.routing_operations.admission_jobs import request_admission
    from tirodhan.modules.routing_operations.models import OperatingShift
    from tirodhan.workers.routing_admission import execute

    factory = database_session_factory
    inputs, now, region = await configured(factory, monkeypatch)
    customer, c = inputs[0]
    command = await command_for(factory, region, c)
    settings = Settings(
        _env_file=None,
        environment="test",
        database_url=migrated_database_url,
        routing_orchestration_enabled=True,
        routing_fleet_enabled=True,
        routing_dispatch_enabled=True,
        pending_payment_lifetime_seconds=1800,
    )
    app = create_app(settings, access_token_codec=_token_codec())
    app.state.database_session_factory = factory
    token, _ = await _token_for(factory, customer, roles=("CUSTOMER",))
    async with factory() as session:
        rider = await session.scalar(select(OperatingShift.rider_id).limit(1))
    rider_token, _ = await _token_for(factory, rider, roles=("RIDER",))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://api.test"
    ) as client:
        retired = await client.post(
            "/v2/customer/capacity-admissions",
            json=c.model_dump(mode="json"),
            headers=_headers(token),
        )
        assert retired.status_code == 410
        assert (
            await client.get("/v2/rider/flexible-admission-offers", headers=_headers(rider_token))
        ).status_code == 410
        assert (
            await client.post(
                "/v2/rider/operations",
                json={
                    "operation": "ADMISSION_FLEXIBLE_ACCEPT",
                    "client_command_id": str(new_uuid7()),
                    "offer_id": str(new_uuid7()),
                },
                headers=_headers(rider_token),
            )
        ).status_code == 410
        held = await client.post(
            "/v2/customer/slot-holds", json=command.model_dump(mode="json"), headers=_headers(token)
        )
        assert held.status_code == 201, held.text
        assert held.json()["bookable"] and held.json()["admission_basis"] == "PROJECTED_ALLOWANCE"
        assert (
            await client.post(
                "/v2/customer/slot-holds",
                json=command.model_dump(mode="json"),
                headers=_headers(token),
            )
        ).json() == held.json()
        old = c.model_copy(update={"client_command_id": new_uuid7()})
        await request_admission(
            factory,
            customer_id=customer,
            command=old,
            expires_at=now + timedelta(hours=1),
            now=now,
        )
        historical = await client.get(
            f"/v2/customer/capacity-admissions/{old.client_command_id}", headers=_headers(token)
        )
        assert historical.json()["status"] == historical.json()["capacity_class"] == "RETIRED"
        assert not historical.json()["bookable"]
    with pytest.raises(OperationalInputError, match="COMMERCIAL_ACCEPTANCE_RETIRED"):
        await execute(region, [], settings)


async def test_failure_after_reservation_insert_rolls_back_all_capacity(
    database_session_factory, monkeypatch
):
    from sqlalchemy import event

    factory = database_session_factory
    inputs, now, region = await configured(factory, monkeypatch)
    customer, c = inputs[0]
    command = await command_for(factory, region, c)
    engine = factory.kw["bind"].sync_engine

    def interrupt(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO slot_reservation_debit"):
            raise RuntimeError("synthetic crash before debit commit")

    event.listen(engine, "before_cursor_execute", interrupt)
    try:
        with pytest.raises(RuntimeError, match="synthetic crash"):
            await hold_slot(
                factory,
                customer_id=customer,
                command=command,
                expires_at=now + timedelta(hours=1),
                now=now,
            )
    finally:
        event.remove(engine, "before_cursor_execute", interrupt)
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(CapacityReservation)) == 0
        assert await session.scalar(select(func.sum(RegionalCapacityBudget.used_seconds))) == 0
    assert await hold_slot(
        factory, customer_id=customer, command=command, expires_at=now + timedelta(hours=1), now=now
    )


async def test_postgres_rejects_hold_without_workload_debits(database_session_factory, monkeypatch):
    from sqlalchemy import insert

    factory = database_session_factory
    inputs, now, region = await configured(factory, monkeypatch)
    customer, c = inputs[0]
    command = await command_for(factory, region, c)
    identity = await hold_slot(
        factory, customer_id=customer, command=command, expires_at=now + timedelta(hours=1), now=now
    )
    async with factory() as session:
        row = await session.get(CapacityReservation, identity)
        values = {
            column.name: getattr(row, column.name)
            for column in CapacityReservation.__table__.columns
        }
    values.update(reservation_id=new_uuid7(), region_version=row.region_version + 1)
    with pytest.raises(DBAPIError, match="complete capacity debits"):
        async with factory() as session, session.begin():
            await session.execute(
                update(OperatingRegion)
                .where(OperatingRegion.region_id == region)
                .values(version=row.region_version + 1)
            )
            await session.execute(insert(CapacityReservation).values(**values))
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(CapacityReservation)) == 1


async def test_shift_close_protects_projection_backed_commitments(
    database_session_factory, monkeypatch
):
    from tirodhan.modules.routing_operations.commands import ShiftClose, execute_resource_command
    from tirodhan.modules.routing_operations.models import OperatingShift

    factory = database_session_factory
    inputs, now, region = await configured(factory, monkeypatch)
    customer, c = inputs[0]
    command = await command_for(factory, region, c)
    await hold_slot(
        factory, customer_id=customer, command=command, expires_at=now + timedelta(hours=1), now=now
    )
    async with factory() as session:
        shift = await session.scalar(select(OperatingShift).where(OperatingShift.starts_at > now))
        manager = (await session.get(OperatingRegion, region)).recorded_by
    with pytest.raises(OperationalInputError, match="COMMITMENTS"):
        await execute_resource_command(
            factory,
            manager_id=manager,
            command=ShiftClose(client_command_id=new_uuid7(), shift_id=shift.shift_id),
            expires_at=now + timedelta(hours=1),
            now=now,
        )


async def test_migration_roundtrip_preserves_financial_history_and_schema(database_session_factory):
    import json

    import httpx
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import text
    from test_payment_preparation import (
        Gateway,
        assert_readiness_schema,
        booking,
        cancel,
        initiate,
        payment_body,
        process,
        settings,
    )

    from tirodhan.modules.payments.razorpay import RazorpayProvider

    factory = database_session_factory
    user, result = await booking(factory)
    gateway = Gateway(factory.kw["bind"])
    async with httpx.AsyncClient(transport=httpx.MockTransport(gateway)) as remote:
        provider = RazorpayProvider(settings(), client=remote)
        attempt = await initiate(factory, user, result, provider)
        await process(factory, provider, payment_body(attempt), event_id="slot-migration-capture")
        await cancel(factory, result.request)

    async def history():
        from legacy_financial_fixture import pre_weight_item_snapshot

        async with factory() as session:
            snapshots = {}
            for table in (
                "collection_request",
                "collection_request_item",
                "payment",
                "payment_attempt",
                "captured_charge",
                "payment_provider_event",
                "refund",
                "refund_obligation",
                "outbox_event",
            ):
                rows = list(await session.scalars(text(f"SELECT to_jsonb(t) FROM {table} t")))
                assert rows, table
                if table == "collection_request_item":
                    rows = [pre_weight_item_snapshot(row) for row in rows]
                snapshots[table] = sorted(json.dumps(row, sort_keys=True) for row in rows)
            return snapshots

    before = await history()
    try:
        await asyncio.to_thread(
            command.downgrade, Config("alembic.ini"), "0031_regional_capacity_recovery"
        )
        assert await history() == before
    finally:
        await asyncio.to_thread(command.upgrade, Config("alembic.ini"), "head")
    assert await history() == before
    async with factory() as session:
        await (await session.connection()).run_sync(assert_readiness_schema)
        assert (
            await session.scalar(
                text(
                    "SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal AND tgname IN "
                    "('initiated_item_terms','initiated_quote_terms','initiated_payment_terms')"
                )
            )
            == 3
        )


async def test_cancellation_racing_payment_releases_once_without_reaccepting(
    database_session_factory, monkeypatch
):
    import test_commercial_admission_benchmark as commercial

    from tirodhan.modules.collection_requests.cancellation import (
        cancel_collection_request_by_customer,
    )
    from tirodhan.modules.collection_requests.models import CollectionRequest

    factory = database_session_factory
    inputs, now, region = await configured(factory, monkeypatch)
    customer, c = inputs[0]
    command = await command_for(factory, region, c)
    await hold_slot(
        factory, customer_id=customer, command=command, expires_at=now + timedelta(hours=1), now=now
    )
    result = await commercial.create_collection_request(
        factory,
        commercial.CreateCollectionRequestCommand(
            customer_id=customer,
            client_request_id=new_uuid7(),
            serviceability_context_id=c.context_id,
            slot_start=c.slot_start,
            slot_end=c.slot_end,
            items=commercial.ITEMS,
            payment_expires_at=now + timedelta(minutes=30),
            capacity_reservation_id=command.client_command_id,
        ),
        commercial.FixedPricing(),
        idempotency_expires_at=now + timedelta(hours=2),
        require_capacity_reservation=True,
        planning_lead_time_minutes=15,
    )
    attempt, _ = await commercial.create_ready_attempt(
        factory,
        customer,
        result.request.request_id,
        key=str(new_uuid7()),
        order_id=str(new_uuid7()),
    )
    event = commercial.success_event(attempt, str(new_uuid7()))
    cancel_key = str(new_uuid7())

    async def cancel():
        async with factory() as session, session.begin():
            return await cancel_collection_request_by_customer(
                session,
                request_id=result.request.request_id,
                customer_id=customer,
                idempotency_key=cancel_key,
                planning_lead_time_minutes=15,
                idempotency_expires_at=now + timedelta(hours=2),
            )

    async def capture():
        return await commercial.process_authenticated_payment_event(
            factory,
            event,
            payload_hash=b"synthetic-projection-race",
            planning_lead_time_minutes=15,
            idempotency_expires_at=now + timedelta(hours=2),
        )

    await asyncio.gather(cancel(), capture())
    await asyncio.gather(cancel(), capture())
    async with factory() as session:
        assert (
            await session.get(CollectionRequest, result.request.request_id)
        ).status == "CANCELLED"
        assert (
            await session.get(CapacityReservation, command.client_command_id)
        ).request_id == result.request.request_id
    await refresh_slots(factory, region, now=now)
    async with factory() as session:
        row = await session.get(RegionalSlotProjection, (region, c.slot_start))
        assert row.bookings == 0 and row.holds == 0 and row.remaining_bookings == 1


async def test_policy_history_and_service_class_are_explicit(database_session_factory, monkeypatch):
    from tirodhan.modules.routing_operations.capacity_models import SlotAllowanceConfiguration

    factory = database_session_factory
    inputs, now, region = await configured(factory, monkeypatch)
    customer, c = inputs[0]
    oversized = await command_for(factory, region, c, pickup_seconds=600)
    with pytest.raises(OperationalInputError, match="SERVICE_CLASS"):
        await hold_slot(
            factory,
            customer_id=customer,
            command=oversized,
            expires_at=now + timedelta(hours=1),
            now=now,
        )
    command = await command_for(factory, region, c)
    await hold_slot(
        factory, customer_id=customer, command=command, expires_at=now + timedelta(hours=1), now=now
    )
    async with factory() as session:
        operating = await session.get(OperatingRegion, region)
    await operational(
        factory,
        operating.recorded_by,
        ConfigureSlotAllowance(
            client_command_id=new_uuid7(),
            region_id=region,
            expected_version=operating.version,
            policy=policy(2),
        ),
        now,
    )
    async with factory() as session:
        assert len(list(await session.scalars(select(SlotAllowanceConfiguration)))) == 2
        assert (
            await session.get(CapacityReservation, command.client_command_id)
        ).estimator_policy_version == 1
    await purchase(factory, customer, command, now)


async def test_operational_fleet_tick_does_not_resume_commercial_acceptance(
    database_session_factory, migrated_database_url, monkeypatch
):
    from unittest.mock import AsyncMock

    from pydantic import SecretStr

    from tirodhan.core.config import Settings
    from tirodhan.modules.routing_operations import admission_jobs, destinations
    from tirodhan.workers import routing_fleet

    await configured(database_session_factory, monkeypatch)
    recovery = AsyncMock()
    cycle = AsyncMock(return_value=None)
    retired = AsyncMock(side_effect=AssertionError("retired commercial worker was invoked"))
    monkeypatch.setattr(destinations, "recover_destinations", recovery)
    monkeypatch.setattr(routing_fleet, "run_fleet_cycle", cycle)
    monkeypatch.setattr(admission_jobs, "run_admissions", retired)
    settings = Settings(
        environment="test",
        database_url=SecretStr(migrated_database_url),
        routing_orchestration_enabled=True,
        routing_fleet_enabled=True,
        routing_dispatch_enabled=True,
    )
    assert await routing_fleet.execute([], settings) == "NOT_DUE_OR_OWNED"
    recovery.assert_awaited_once()
    cycle.assert_awaited_once()
    retired.assert_not_awaited()


async def test_loaded_vehicle_without_discharge_task_is_not_forecast_as_free(
    database_session_factory,
):
    from test_orchestration_custody_races import loaded_route

    from tirodhan.modules.routing_operations.dispatch_models import RouteTask
    from tirodhan.modules.routing_operations.models import OperatingShift
    from tirodhan.modules.routing_operations.operational_commands import ConfirmShift
    from tirodhan.modules.routing_operations.physical import cargo_load

    factory = database_session_factory
    data, root, revision, booking, target, at = await loaded_route(factory)
    async with factory() as session:
        region = await session.get(OperatingRegion, data[7])
        shift = await session.scalar(
            select(OperatingShift).where(OperatingShift.vehicle_id == data[1][0])
        )
    await operational(
        factory,
        region.recorded_by,
        ConfigureSlotAllowance(
            client_command_id=new_uuid7(),
            region_id=region.region_id,
            expected_version=region.version,
            policy=policy(),
        ),
        at,
    )
    await operational(
        factory,
        shift.rider_id,
        ConfirmShift(client_command_id=new_uuid7(), shift_id=shift.shift_id),
        shift.starts_at - timedelta(seconds=1),
    )
    # A disrupted discharge can leave a live pickup task and physically collected cargo.
    async with factory() as session, session.begin():
        assert (await cargo_load(session, shift.vehicle_id))[0] > 0
        pending_pickup = await session.scalar(
            select(RouteTask).where(
                RouteTask.route_id == root.route_id,
                RouteTask.kind == "PICKUP",
                RouteTask.state == "PENDING",
            )
        )
        assert pending_pickup is not None
        await session.execute(
            update(RouteTask)
            .where(RouteTask.route_id == root.route_id, RouteTask.kind == "DISCHARGE")
            .values(state="CANCELLED")
        )
    await refresh_slots(factory, region.region_id, now=at)
    async with factory() as session:
        # Only the confirmed vehicle could contribute; cargo excludes its remaining shift.
        assert (
            await session.scalar(
                select(func.sum(RegionalCapacityBudget.capacity_seconds)).where(
                    RegionalCapacityBudget.kind == "DUTY"
                )
            )
            == 0
        )
        assert (await cargo_load(session, shift.vehicle_id))[0] > 0


@pytest.mark.parametrize("withdrawal", ["close", "transfer"])
@pytest.mark.parametrize("reverse", [False, True])
async def test_projected_hold_racing_capacity_withdrawal_has_one_winner(
    database_session_factory, monkeypatch, withdrawal, reverse
):
    import h3
    from test_fleet_hypothesis_concurrency import overlap

    from tirodhan.modules.routing_operations.admission_models import AdmissionSourceHead
    from tirodhan.modules.routing_operations.commands import ShiftClose, execute_resource_command
    from tirodhan.modules.routing_operations.models import OperatingShift
    from tirodhan.modules.routing_operations.operational_commands import (
        ConfigureRegion,
        ConfirmShift,
    )
    from tirodhan.modules.routing_operations.orchestration_models import RegionCell, RegionVehicle
    from tirodhan.modules.routing_operations.regional_commands import TransferRegionalCapacity
    from tirodhan.modules.routing_operations.rolling import FleetPolicy
    from tirodhan.modules.serviceability.models import ServiceabilityContext

    factory = database_session_factory
    inputs, now, region_id = await configured(factory, monkeypatch)
    async with factory() as session:
        region = await session.get(OperatingRegion, region_id)
        shift = await session.scalar(select(OperatingShift).where(OperatingShift.starts_at > now))
        cells = list(
            await session.scalars(
                select(RegionCell.cell_id).where(RegionCell.region_id == region_id)
            )
        )
    await operational(
        factory,
        shift.rider_id,
        ConfirmShift(client_command_id=new_uuid7(), shift_id=shift.shift_id),
        shift.starts_at - timedelta(seconds=1),
    )
    # Advance this synthetic scenario to an active shift. Resolution is fixture input,
    # so keep its contexts valid at that clock; no customer command bypasses ownership.
    now = shift.starts_at
    async with factory() as session, session.begin():
        await session.execute(
            update(ServiceabilityContext).values(expires_at=now + timedelta(hours=2))
        )
    await refresh_slots(factory, region_id, now=now)
    if withdrawal == "transfer":
        adjacent = next(c for cell in cells for c in h3.grid_disk(cell, 1) if c not in cells)
        other = await operational(
            factory,
            region.recorded_by,
            ConfigureRegion(
                client_command_id=new_uuid7(),
                name="synthetic-allowance-neighbour",
                cells=(adjacent,),
                vehicle_ids=(),
                receiving_point_ids=(region.default_warehouse_id,),
                default_warehouse_id=region.default_warehouse_id,
                fleet=FleetPolicy.model_validate(region.policy["fleet"]),
                safety_percent=100,
            ),
            now,
        )
        await refresh_slots(factory, region_id, now=now)
        async with factory() as session:
            current = await session.get(OperatingRegion, region_id)
            target = await session.get(OperatingRegion, other)
            membership = await session.get(RegionVehicle, shift.vehicle_id)
        transfer = TransferRegionalCapacity(
            client_command_id=new_uuid7(),
            shift_id=shift.shift_id,
            from_region_id=region_id,
            to_region_id=other,
            expected_membership_version=membership.version,
            expected_from_version=current.version,
            expected_to_version=target.version,
        )

        async def withdraw():
            return await operational(factory, region.recorded_by, transfer, now)

    else:

        async def withdraw():
            return await execute_resource_command(
                factory,
                manager_id=region.recorded_by,
                command=ShiftClose(client_command_id=new_uuid7(), shift_id=shift.shift_id),
                expires_at=now + timedelta(hours=1),
                now=now,
            )

    customer, original = inputs[0]
    command = await command_for(factory, region_id, original)

    async def claim():
        return await hold_slot(
            factory,
            customer_id=customer,
            command=command,
            expires_at=now + timedelta(hours=1),
            now=now,
        )

    commands = [claim, withdraw]
    if reverse:
        commands.reverse()
    # The existing harness observes BOTH live PostgreSQL lock waits before releasing.
    results = await overlap(factory, AdmissionSourceHead, region_id, commands)
    assert sum(not isinstance(r, Exception) for r in results) == 1, results
    async with factory() as session:
        held = await session.get(CapacityReservation, command.client_command_id)
        current_shift = await session.get(OperatingShift, shift.shift_id)
        member = await session.get(RegionVehicle, shift.vehicle_id)
        if held:
            assert current_shift.closed_at is None and member.region_id == region_id
        else:
            assert current_shift.closed_at is not None or member.region_id != region_id
