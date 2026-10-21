"""Real PostgreSQL capacity ownership, manager replacement and projection freshness."""

import asyncio
from datetime import timedelta

import h3
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from test_operational_orchestration import admission, operational, regional, reserve
from test_routing_fleet import LineMatrix, engines

from tirodhan.db.values import new_uuid7
from tirodhan.modules.dispatch.models import RiderAssignment, RiderAssignmentItem
from tirodhan.modules.planning.models import PickupExecution
from tirodhan.modules.routing_operations.authority import RouteAuthorityError
from tirodhan.modules.routing_operations.capacity_models import (
    RegionalSlotRefresh,
    RegionCapacityTransfer,
    RiderReplacement,
)
from tirodhan.modules.routing_operations.operational_commands import ConfigureRegion
from tirodhan.modules.routing_operations.orchestration_models import OperatingRegion, RegionVehicle
from tirodhan.modules.routing_operations.regional_commands import TransferRegionalCapacity
from tirodhan.modules.routing_operations.replacement_commands import (
    DecideReplacement,
    RecommendReplacement,
    RequestReplacement,
)
from tirodhan.modules.routing_operations.replacements import recommend_replacement
from tirodhan.modules.routing_operations.slot_projection import read_slots, refresh_slots

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def neighbour(factory, data):
    f, vehicles, point, _, _, _, clock, region_id, *_ = data
    async with factory() as session:
        old = await session.get(OperatingRegion, region_id)
        from tirodhan.modules.routing_operations.rolling import FleetPolicy

        fleet = FleetPolicy.model_validate(old.policy["fleet"])
    cell = h3.latlng_to_cell(28.632, 77.209, 7)
    adjacent = next(c for c in h3.grid_disk(cell, 1) if c != cell)
    result = await operational(
        factory,
        f.manager_id,
        ConfigureRegion(
            client_command_id=new_uuid7(),
            name="adjacent",
            cells=(adjacent,),
            vehicle_ids=(),
            receiving_point_ids=(point.receiving_point_id,),
            default_warehouse_id=point.receiving_point_id,
            fleet=fleet,
            safety_percent=100,
        ),
        clock[0],
    )
    return result


async def transfer_command(factory, data, other):
    from tirodhan.modules.routing_operations.models import OperatingShift

    async with factory() as session:
        shift = await session.scalar(
            select(OperatingShift).where(OperatingShift.vehicle_id == data[1][0])
        )
        membership = await session.get(RegionVehicle, data[1][0])
        source, target = (
            await session.get(OperatingRegion, data[7]),
            await session.get(OperatingRegion, other),
        )
        return TransferRegionalCapacity(
            client_command_id=new_uuid7(),
            shift_id=shift.shift_id,
            from_region_id=source.region_id,
            to_region_id=target.region_id,
            expected_membership_version=membership.version,
            expected_from_version=source.version,
            expected_to_version=target.version,
        )


async def test_adjacent_transfer_one_owner_replay_and_db_fence(database_session_factory):
    factory = database_session_factory
    data = await regional(factory, empty=True)
    other = await neighbour(factory, data)
    command = await transfer_command(factory, data, other)
    result = await operational(factory, data[0].manager_id, command, data[6][0])
    assert await operational(factory, data[0].manager_id, command, data[6][0]) == result
    async with factory() as session:
        member = await session.get(RegionVehicle, data[1][0])
        assert member.region_id == other and member.version == 2
        assert await session.get(RegionCapacityTransfer, result)
    async with factory() as session, session.begin():
        with pytest.raises(DBAPIError):
            await session.execute(
                text(
                    "UPDATE operating_region_vehicle SET region_id=:old WHERE vehicle_id=:vehicle"
                ),
                {"old": data[7], "vehicle": data[1][0]},
            )


async def test_live_hold_prevents_capacity_transfer(database_session_factory):
    factory = database_session_factory
    data = await regional(factory, empty=True)
    other = await neighbour(factory, data)
    await reserve(factory, data[8], admission(data[9], data[6][0]), data[6][0])
    command = await transfer_command(factory, data, other)
    with pytest.raises(RouteAuthorityError, match="COMMITMENTS"):
        await operational(factory, data[0].manager_id, command, data[6][0])


async def test_shift_close_cannot_bypass_certified_hold_owner(database_session_factory):
    from tirodhan.modules.routing_operations.commands import OperationalInputError, ShiftClose

    factory = database_session_factory
    data = await regional(factory, empty=True)
    command = admission(data[9], data[6][0])
    await reserve(factory, data[8], command, data[6][0])
    other = await neighbour(factory, data)
    transfer = await transfer_command(factory, data, other)
    with pytest.raises(OperationalInputError, match="CERTIFIED_COMMITMENTS"):
        await data[5](ShiftClose(client_command_id=new_uuid7(), shift_id=transfer.shift_id))
    with pytest.raises(DBAPIError, match="certified commercial commitments"):
        async with factory() as session, session.begin():
            await session.execute(
                text("UPDATE operating_shift SET closed_at=:at WHERE shift_id=:id"),
                {"at": data[6][0], "id": transfer.shift_id},
            )


async def test_bound_paid_promise_remains_guarded_before_next_certificate_alias_refresh(
    database_session_factory,
):
    from test_commercial_admission_benchmark import population, purchase

    from tirodhan.modules.routing_operations.admission_models import AdmissionAllocation
    from tirodhan.modules.routing_operations.commands import (
        OperationalInputError,
        ShiftClose,
        execute_resource_command,
    )
    from tirodhan.modules.routing_operations.models import OperatingShift

    factory = database_session_factory
    inputs, now = await population(factory, 1)
    customer, command = inputs[0]
    await reserve(factory, customer, command, now)
    await purchase(factory, customer, command, now)
    async with factory() as session:
        allocation = await session.scalar(select(AdmissionAllocation))
        shift = await session.scalar(
            select(OperatingShift).where(OperatingShift.vehicle_id == allocation.vehicle_id)
        )
        region = await session.scalar(select(OperatingRegion))
    with pytest.raises(OperationalInputError, match="CERTIFIED_COMMITMENTS"):
        await execute_resource_command(
            factory,
            manager_id=region.recorded_by,
            command=ShiftClose(client_command_id=new_uuid7(), shift_id=shift.shift_id),
            expires_at=now + timedelta(days=1),
            now=now,
        )


async def test_transfer_racing_new_admission_cannot_both_allocate_source_capacity(
    database_session_factory,
):
    factory = database_session_factory
    data = await regional(factory, empty=True)
    other = await neighbour(factory, data)
    transfer = await transfer_command(factory, data, other)
    result = await asyncio.gather(
        operational(factory, data[0].manager_id, transfer, data[6][0]),
        reserve(factory, data[8], admission(data[9], data[6][0]), data[6][0]),
        return_exceptions=True,
    )
    assert sum(not isinstance(r, Exception) for r in result) == 1


async def test_concurrent_region_transfers_have_one_winner(database_session_factory):
    factory = database_session_factory
    data = await regional(factory, empty=True)
    other = await neighbour(factory, data)
    command = await transfer_command(factory, data, other)
    results = await asyncio.gather(
        *[
            operational(
                factory,
                data[0].manager_id,
                command.model_copy(update={"client_command_id": new_uuid7()}),
                data[6][0],
            )
            for _ in range(2)
        ],
        return_exceptions=True,
    )
    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert all(
        isinstance(result, RouteAuthorityError)
        for result in results
        if isinstance(result, Exception)
    )


async def test_slot_projection_invalidates_transactionally_and_reconciles(database_session_factory):
    factory = database_session_factory
    data = await regional(factory, empty=True)
    now = data[6][0]
    from test_slot_holds import policy

    from tirodhan.modules.routing_operations.operational_commands import ConfigureSlotAllowance

    async with factory() as session:
        region = await session.get(OperatingRegion, data[7])
    await operational(
        factory,
        data[0].manager_id,
        ConfigureSlotAllowance(
            client_command_id=new_uuid7(),
            region_id=data[7],
            expected_version=region.version,
            policy=policy(),
        ),
        now,
    )
    assert await refresh_slots(factory, data[7], now=now) > 0
    async with factory() as session:
        before = await read_slots(session, data[7], now=now)
        assert before and all(s["status"] != "UNKNOWN" for s in before)
    await reserve(factory, data[8], admission(data[9], now), now)
    async with factory() as session:
        stale = await read_slots(session, data[7], now=now)
        assert all(s["status"] == "UNKNOWN" and not s["capacity_certified"] for s in stale)
        assert (await session.get(RegionalSlotRefresh, data[7])).due_at <= now + timedelta(
            seconds=5
        )
    await refresh_slots(factory, data[7], now=now)
    async with factory() as session:
        assert all(s["status"] != "UNKNOWN" for s in await read_slots(session, data[7], now=now))
        assert all(
            s["status"] == "UNKNOWN"
            for s in await read_slots(session, data[7], now=now + timedelta(seconds=61))
        )


async def test_slot_http_requires_owned_context_and_capacity_http_requires_manager(
    database_session_factory, migrated_database_url
):
    import httpx
    from test_operational_api import _headers, _token_codec, _token_for

    from tirodhan.core.config import Settings
    from tirodhan.main import create_app

    factory = database_session_factory
    data = await regional(factory, empty=True, second=True)
    await refresh_slots(factory, data[7], now=data[6][0])
    app = create_app(
        Settings(
            _env_file=None,
            environment="test",
            database_url=migrated_database_url,
            routing_orchestration_enabled=True,
            routing_fleet_enabled=True,
            routing_dispatch_enabled=True,
        ),
        access_token_codec=_token_codec(),
    )
    app.state.database_session_factory = factory
    customer, _ = await _token_for(factory, data[8], roles=("CUSTOMER",))
    manager, _ = await _token_for(factory, data[0].manager_id, roles=("MANAGER", "CUSTOMER"))
    path = f"/v2/customer/slot-availability?serviceability_context_id={data[9]}"
    capacity = f"/v2/manager/regions/{data[7]}/capacity?limit=1"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://api.test"
    ) as client:
        assert (await client.get(path)).status_code == 401
        assert (await client.get(path, headers=_headers(manager))).status_code == 404
        result = await client.get(path, headers=_headers(customer))
        assert result.status_code == 200, result.text
        assert result.json()["capacity_certified"] is False and result.json()["slots"]
        assert (await client.get(capacity, headers=_headers(customer))).status_code == 403
        response = await client.get(capacity, headers=_headers(manager))
        assert response.status_code == 200, response.text
        assert len(response.json()["resources"]) == 1 and response.json()["next_cursor"]
        next_page = await client.get(
            capacity + f"&after_vehicle_id={response.json()['next_cursor']}",
            headers=_headers(manager),
        )
        assert next_page.status_code == 200 and next_page.json()["next_cursor"] is None
        assert len(next_page.json()["resources"]) == 1
        assert (
            response.json()["resources"][0]["vehicle_id"]
            != next_page.json()["resources"][0]["vehicle_id"]
        )


async def test_rider_request_recommend_manager_approve_and_normal_route_activation(
    database_session_factory,
):
    from test_regional_parallel import prepared, run

    factory = database_session_factory
    data, _ = await prepared(factory)
    assert (await run(factory, data)).status == "ACTIVATED"
    async with factory() as session:
        item = await session.scalar(
            select(RiderAssignmentItem).where(RiderAssignmentItem.released_at.is_(None))
        )
        assignment = await session.get(RiderAssignment, item.assignment_id)
        pickup = await session.get(PickupExecution, item.pickup_execution_id)
    command = RequestReplacement(client_command_id=new_uuid7(), request_id=pickup.request_id)
    replacement = await operational(factory, assignment.rider_id, command, data[6][0])
    assert await operational(factory, assignment.rider_id, command, data[6][0]) == replacement
    assert await recommend_replacement(
        factory,
        replacement,
        providers={"test-car": LineMatrix()},
        optimizers=engines(),
        now=data[6][0],
    )
    async with factory() as session:
        row = await session.get(RiderReplacement, replacement)
    await operational(
        factory,
        data[0].manager_id,
        DecideReplacement(
            client_command_id=new_uuid7(),
            replacement_id=replacement,
            expected_version=row.version,
            decision="APPROVE",
        ),
        data[6][0],
    )
    async with factory() as session:
        approved = await session.get(RiderReplacement, replacement)
    await operational(
        factory,
        data[0].manager_id,
        RecommendReplacement(
            client_command_id=new_uuid7(),
            replacement_id=replacement,
            expected_version=approved.version,
        ),
        data[6][0],
    )
    async with factory() as session:
        retained = await session.get(RiderReplacement, replacement)
        assert retained.status == "APPROVED" and retained.recommended_shift_id
    assert await recommend_replacement(
        factory,
        replacement,
        providers={"test-car": LineMatrix()},
        optimizers=engines(),
        now=data[6][0],
    )
    data[6][0] += timedelta(seconds=1)
    cycle = await run(factory, data)
    assert cycle.status == "ACTIVATED"
    async with factory() as session:
        row = await session.get(RiderReplacement, replacement)
        assert row.status == "COMPLETED"
        owner = await session.scalar(
            select(RiderAssignment.rider_id)
            .join(RiderAssignmentItem)
            .where(
                RiderAssignmentItem.pickup_execution_id == pickup.pickup_execution_id,
                RiderAssignmentItem.released_at.is_(None),
            )
        )
        assert owner != assignment.rider_id


async def test_collected_replacement_approval_keeps_custody_until_evidenced_transfer(
    database_session_factory,
):
    pytest.importorskip(
        "ortools", reason="regional replacement requires the optional routing extra"
    )
    from test_orchestration_custody_races import loaded_route

    from tirodhan.modules.routing_operations.operational_commands import (
        CompleteTransfer,
        ConfirmShift,
        TransferReceipt,
        custodian,
    )
    from tirodhan.modules.routing_operations.physical import cargo_load

    factory = database_session_factory
    data, root, revision, booking, target, at = await loaded_route(factory)
    await operational(
        factory,
        target.rider_id,
        ConfirmShift(client_command_id=new_uuid7(), shift_id=target.shift_id),
        target.starts_at - timedelta(seconds=1),
    )
    # First household is collected; the original rider may still own other pending work.
    replacement = await operational(
        factory,
        data[0].rider_ids[0],
        RequestReplacement(client_command_id=new_uuid7(), request_id=booking.request_id),
        at,
    )
    assert await recommend_replacement(
        factory, replacement, providers={"test-car": LineMatrix()}, optimizers=engines(), now=at
    )
    async with factory() as session:
        row = await session.get(RiderReplacement, replacement)
    await operational(
        factory,
        data[0].manager_id,
        DecideReplacement(
            client_command_id=new_uuid7(),
            replacement_id=replacement,
            expected_version=row.version,
            decision="APPROVE",
        ),
        at,
    )
    async with factory() as session:
        row = await session.get(RiderReplacement, replacement)
        assert (await custodian(session, booking.request_id))[0] == data[1][0]
        intent = row.transfer_intent_id
    complete = CompleteTransfer(client_command_id=new_uuid7(), intent_id=intent)
    with pytest.raises(RouteAuthorityError, match="RECEIPTS"):
        await operational(factory, data[0].manager_id, complete, at)
    for rider in (data[0].rider_ids[0], target.rider_id):
        await operational(
            factory,
            rider,
            TransferReceipt(
                client_command_id=new_uuid7(),
                intent_id=intent,
                scanned_request_id=booking.request_id,
                captured_at=at,
            ),
            at,
        )
    result = await operational(factory, data[0].manager_id, complete, at)
    assert await operational(factory, data[0].manager_id, complete, at) == result
    async with factory() as session:
        assert (await custodian(session, booking.request_id)) == (
            target.vehicle_id,
            target.rider_id,
        )
        assert (await cargo_load(session, target.vehicle_id))[0] > 0
        pending = await session.get(RiderReplacement, replacement)
        assert pending.status == "APPROVED" and pending.activation_id is None
        assert pending.failure_code == "AWAITING_ROUTE_ACTIVATION"


async def test_recommendation_stale_on_source_change_and_manager_can_reject(
    database_session_factory,
):
    from test_regional_parallel import prepared, run

    from tirodhan.modules.routing_operations.commands import PositionReport

    factory = database_session_factory
    data, _ = await prepared(factory)
    await run(factory, data)
    async with factory() as session:
        item = await session.scalar(
            select(RiderAssignmentItem).where(RiderAssignmentItem.released_at.is_(None))
        )
        rider = (await session.get(RiderAssignment, item.assignment_id)).rider_id
        pickup = await session.get(PickupExecution, item.pickup_execution_id)
    replacement = await operational(
        factory,
        rider,
        RequestReplacement(client_command_id=new_uuid7(), request_id=pickup.request_id),
        data[6][0],
    )
    assert await recommend_replacement(
        factory,
        replacement,
        providers={"test-car": LineMatrix()},
        optimizers=engines(),
        now=data[6][0],
    )
    async with factory() as session:
        row = await session.get(RiderReplacement, replacement)
    await data[5](
        PositionReport(
            client_command_id=new_uuid7(),
            vehicle_id=data[1][0],
            longitude=77.208,
            latitude=28.632,
            observed_at=data[6][0] + timedelta(seconds=1),
        ),
        at=data[6][0] + timedelta(seconds=1),
    )
    with pytest.raises(RouteAuthorityError, match="STALE"):
        await operational(
            factory,
            data[0].manager_id,
            DecideReplacement(
                client_command_id=new_uuid7(),
                replacement_id=replacement,
                expected_version=row.version,
                decision="APPROVE",
            ),
            data[6][0],
        )
    await operational(
        factory,
        data[0].manager_id,
        DecideReplacement(
            client_command_id=new_uuid7(),
            replacement_id=replacement,
            expected_version=row.version,
            decision="REJECT",
        ),
        data[6][0],
    )


async def test_past_facility_opening_does_not_invalidate_future_booking(database_session_factory):
    from tirodhan.modules.routing_operations.commands import WindowDeclare

    factory = database_session_factory
    data = await regional(factory, empty=True)
    now = data[6][0]
    await data[5](
        WindowDeclare(
            client_command_id=new_uuid7(),
            receiving_point_id=data[2].receiving_point_id,
            starts_at=now - timedelta(hours=2),
            ends_at=now - timedelta(hours=1),
        )
    )
    assert await reserve(factory, data[8], admission(data[9], now), now)


async def test_expired_loaded_rider_keeps_custody_without_blocking_healthy_admission(
    database_session_factory,
):
    from test_orchestration_custody_races import loaded_route

    from tirodhan.modules.routing_operations.admission_state import read_cache
    from tirodhan.modules.routing_operations.commands import PositionReport, WindowDeclare
    from tirodhan.modules.routing_operations.operational_commands import (
        AuthorizeDischarge,
        AuthorizeExtension,
        ConsentExtension,
    )
    from tirodhan.modules.routing_operations.physical import cargo_load
    from tirodhan.modules.routing_operations.rolling import FleetPolicy

    factory = database_session_factory
    data, root, revision, booking, target, at = await loaded_route(factory)
    late = target.ends_at + timedelta(seconds=1)
    consent = await operational(
        factory,
        target.rider_id,
        ConsentExtension(
            client_command_id=new_uuid7(),
            shift_id=target.shift_id,
            ends_at=late + timedelta(hours=4),
        ),
        at,
    )
    await operational(
        factory,
        data[0].manager_id,
        AuthorizeExtension(
            client_command_id=new_uuid7(), consent_id=consent, safe_conditions_confirmed=True
        ),
        at,
    )
    await data[5](
        WindowDeclare(
            client_command_id=new_uuid7(),
            receiving_point_id=data[2].receiving_point_id,
            starts_at=at,
            ends_at=late + timedelta(hours=5),
        )
    )
    await operational(
        factory,
        data[0].manager_id,
        AuthorizeDischarge(
            client_command_id=new_uuid7(),
            request_id=booking.request_id,
            receiving_point_id=data[2].receiving_point_id,
            discharge_deadline=late + timedelta(hours=3),
        ),
        at,
    )
    async with factory() as session:
        region = await session.get(OperatingRegion, data[7])
        fleet = FleetPolicy.model_validate(region.policy["fleet"])
        before = await cargo_load(session, data[1][0])
    await read_cache(factory).capture(
        factory,
        fleet.snapshot_configuration(at, region_id=data[7], admission_mode=True),
        region.version,
    )
    await data[5](
        PositionReport(
            client_command_id=new_uuid7(),
            vehicle_id=target.vehicle_id,
            longitude=77.220,
            latitude=28.632,
            observed_at=late,
        ),
        at=late,
    )
    result = await reserve(factory, data[8], admission(data[9], late), late)
    assert result
    async with factory() as session:
        assert before[0] > 0 and await cargo_load(session, data[1][0]) == before
        pickup = await session.scalar(
            select(PickupExecution).where(PickupExecution.request_id == booking.request_id)
        )
        assert pickup.status == "COLLECTED"


async def test_replacement_matrix_outage_retries_without_releasing_original_work(
    database_session_factory,
):
    from test_regional_parallel import prepared, run

    from tirodhan.modules.planning.routing.execution import RoutingAdapterError

    factory = database_session_factory
    data, _ = await prepared(factory)
    assert (await run(factory, data)).status == "ACTIVATED"
    async with factory() as session:
        item = await session.scalar(
            select(RiderAssignmentItem).where(RiderAssignmentItem.released_at.is_(None))
        )
        assignment = await session.get(RiderAssignment, item.assignment_id)
        pickup = await session.get(PickupExecution, item.pickup_execution_id)
    identity = await operational(
        factory,
        assignment.rider_id,
        RequestReplacement(client_command_id=new_uuid7(), request_id=pickup.request_id),
        data[6][0],
    )

    class Unavailable:
        async def acquire(self, nodes):
            raise RoutingAdapterError("MATRIX_TIMEOUT")

    assert not await recommend_replacement(
        factory,
        identity,
        providers={"test-car": Unavailable()},
        optimizers=engines(),
        now=data[6][0],
    )
    async with factory() as session:
        failed = await session.get(RiderReplacement, identity)
        retry_at = failed.next_attempt_at
        assert failed.status == "REQUESTED" and failed.attempt == 1 and retry_at > data[6][0]
        assert (
            await session.get(RiderAssignmentItem, (item.assignment_id, item.pickup_execution_id))
        ).released_at is None
    assert await recommend_replacement(
        factory, identity, providers={"test-car": LineMatrix()}, optimizers=engines(), now=retry_at
    )
    async with factory() as session:
        assert (await session.get(RiderReplacement, identity)).status == "RECOMMENDED"
        assert (
            await session.get(RiderAssignmentItem, (item.assignment_id, item.pickup_execution_id))
        ).released_at is None


async def test_migration_round_trip_preserves_financial_schema(database_session_factory):
    from alembic import command
    from alembic.config import Config
    from test_collection_payment import create_context, create_request, create_user
    from test_payment_preparation import assert_readiness_schema

    factory = database_session_factory
    # Exercise this migration's own populated legacy boundary. Later immutable
    # administration history deliberately prevents lossy downgrade after adoption.
    await asyncio.to_thread(
        command.downgrade, Config("alembic.ini"), "0032_projected_slot_allowances"
    )
    try:
        user = await create_user(factory)
        context = await create_context(factory, user.user_id)
        await create_request(factory, user.user_id, context.serviceability_context_id)
        await asyncio.to_thread(
            command.downgrade, Config("alembic.ini"), "0030_admission_certificates"
        )
    finally:
        await asyncio.to_thread(command.upgrade, Config("alembic.ini"), "head")
    async with factory() as session:
        await (await session.connection()).run_sync(assert_readiness_schema)
