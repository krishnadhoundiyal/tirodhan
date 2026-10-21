"""Actual PostGIS administration, immutable history, replay and concurrent updates."""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.business_admin.commands import CategoryEdit, GroupEdit, PriceEdit
from tirodhan.modules.business_admin.history import compare, versions
from tirodhan.modules.business_admin.pricing import FlatPickupPricing
from tirodhan.modules.business_admin.service import execute_business_command
from tirodhan.modules.collection_requests.ports import (
    DeclaredRequestItem,
    PricingNotConfiguredError,
)
from tirodhan.modules.customer_reads.models import CatalogueGroup
from tirodhan.modules.identity.models import AppUser, UserRole
from tirodhan.modules.reliability.primitives import IdempotencyKeyConflictError
from tirodhan.modules.routing_operations.commands import OperationalInputError

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def manager(factory):
    identity = new_uuid7()
    async with factory() as session, session.begin():
        session.add(AppUser(user_id=identity, status="ACTIVE"))
        await session.flush()
        session.add(UserRole(user_id=identity, role_code="MANAGER", granted_at=utc_now()))
    return identity


async def change(factory, actor, command):
    return await execute_business_command(
        factory, actor_id=actor, command=command, expires_at=utc_now() + timedelta(days=1)
    )


def group(**kwargs):
    return GroupEdit(
        client_command_id=new_uuid7(),
        resource_id=new_uuid7(),
        expected_version=0,
        reason="INITIAL_CONFIGURATION",
        group_code="APPROVED_TEST",
        display_name="Configured group",
        display_order=1,
        active=True,
        **kwargs,
    )


async def test_manager_history_replay_conflict_and_immutable_before_after(database_session_factory):
    factory = database_session_factory
    actor = await manager(factory)
    command = group()
    result = await change(factory, actor, command)
    assert await change(factory, actor, command) == result
    with pytest.raises(IdempotencyKeyConflictError):
        await change(factory, actor, command.model_copy(update={"display_name": "Conflicting"}))
    edit = command.model_copy(
        update={
            "client_command_id": new_uuid7(),
            "expected_version": 1,
            "display_name": "Revised group",
            "active": False,
        }
    )
    await change(factory, actor, edit)
    async with factory() as session:
        history = await versions(session, "catalogue_group", result)
        assert len(history["versions"]) == 2
        assert history["versions"][0]["actor_id"] == actor
        difference = await compare(session, "catalogue_group", result, 1, 2)
        assert set(difference["changes"]) == {"display_name", "active"}
        assert difference["after"]["command_id"] == edit.client_command_id
    async with factory() as session, session.begin():
        with pytest.raises(DBAPIError):
            await session.execute(text("UPDATE catalogue_group_revision SET reason='REWRITE'"))


async def test_manager_concurrency_and_live_revocation(database_session_factory):
    factory = database_session_factory
    actor = await manager(factory)
    command = group()
    await change(factory, actor, command)
    edits = [
        command.model_copy(
            update={
                "client_command_id": new_uuid7(),
                "expected_version": 1,
                "display_name": f"Concurrent {i}",
            }
        )
        for i in range(2)
    ]
    results = await asyncio.gather(
        *(change(factory, actor, c) for c in edits), return_exceptions=True
    )
    assert sum(not isinstance(r, Exception) for r in results) == 1
    assert all(
        not isinstance(r, Exception) or isinstance(r, (DBAPIError, OperationalInputError))
        for r in results
    )
    async with factory() as session, session.begin():
        role = await session.scalar(select(UserRole).where(UserRole.user_id == actor))
        role.revoked_at = utc_now()
    with pytest.raises(OperationalInputError, match="LIVE_AUTHORIZATION_REQUIRED"):
        await change(factory, actor, command)  # Replay does not bypass revoked authority.
    async with factory() as session:
        assert await session.scalar(select(CatalogueGroup.display_name)) in {
            "Concurrent 0",
            "Concurrent 1",
        }


async def test_flat_pickup_price_is_versioned_and_independent_of_item_count(
    database_session_factory,
):
    factory = database_session_factory
    actor = await manager(factory)
    g = group()
    await change(factory, actor, g)
    await change(
        factory,
        actor,
        CategoryEdit(
            client_command_id=new_uuid7(),
            resource_id=new_uuid7(),
            expected_version=0,
            reason="CATALOGUE_CONFIGURATION",
            category_code="APPROVED_ITEM",
            group_id=g.resource_id,
            display_name="Approved item",
            description="Synthetic",
            display_order=1,
            active=True,
            quantity_input="OPTIONAL",
            weight_input="OPTIONAL",
        ),
    )
    pricing = FlatPickupPricing(factory)
    items = [DeclaredRequestItem("APPROVED_ITEM", 1, 100)]
    with pytest.raises(PricingNotConfiguredError):
        await pricing.quote(items)
    command = PriceEdit(
        client_command_id=new_uuid7(),
        resource_id=new_uuid7(),
        expected_version=0,
        reason="MVP_FLAT_FEE",
        amount_minor=12345,
        active=True,
    )
    await change(factory, actor, command)
    first = await pricing.quote(items)
    many = await pricing.quote(items * 3)
    assert first.total_amount_minor == many.total_amount_minor == 12345
    assert many.currency == "INR" and sum(i.quoted_line_amount_minor for i in many.items) == 12345
    await change(
        factory,
        actor,
        command.model_copy(
            update={"client_command_id": new_uuid7(), "expected_version": 1, "amount_minor": 23456}
        ),
    )
    current = await pricing.quote(items)
    assert first.total_amount_minor == 12345 and current.total_amount_minor == 23456
    assert first.items[0].pricing_rule_version != current.items[0].pricing_rule_version


async def test_product_revision_keeps_immutable_previous_asset(database_session_factory):
    from tirodhan.modules.business_admin.commands import MediaRegister

    factory = database_session_factory
    actor = await manager(factory)
    g = group()
    await change(factory, actor, g)
    images = []
    for _ in range(2):
        identity = new_uuid7()
        command = MediaRegister(
            client_command_id=identity,
            resource_id=identity,
            expected_version=0,
            reason="PRODUCT_IMAGE",
            width=640,
            height=480,
            alt_text="Synthetic artwork",
        )
        with pytest.raises(OperationalInputError, match="VERIFIED"):
            await change(factory, actor, command)
        await execute_business_command(
            factory,
            actor_id=actor,
            command=command,
            media_verified=True,
            expires_at=utc_now() + timedelta(days=1),
        )
        assert await change(factory, actor, command) == identity
        images.append(identity)
    category = CategoryEdit(
        client_command_id=new_uuid7(),
        resource_id=new_uuid7(),
        expected_version=0,
        reason="CATALOGUE_CONFIGURATION",
        category_code="IMAGE_ITEM",
        group_id=g.resource_id,
        display_name="Item",
        description="Synthetic",
        display_order=1,
        active=True,
        quantity_input="OPTIONAL",
        weight_input="OPTIONAL",
        image_id=images[0],
    )
    await change(factory, actor, category)
    await change(
        factory,
        actor,
        category.model_copy(
            update={
                "client_command_id": new_uuid7(),
                "expected_version": 1,
                "image_id": images[1],
            }
        ),
    )
    async with factory() as session:
        history = await compare(session, "catalogue_category", category.resource_id, 1, 2)
        assert history["changes"]["image_id"] == {
            "before": str(images[0]),
            "after": str(images[1]),
        }
        for asset in images:
            state = await versions(session, "catalogue_media", asset)
            assert state["versions"][0]["resulting_state"]["width"] == 640
    async with factory() as session, session.begin():
        with pytest.raises(DBAPIError, match="immutable"):
            await session.execute(
                text("UPDATE catalogue_media SET object_key='product-art/overwrite'")
            )


async def test_rider_provisioning_role_revocation_and_profile_history(database_session_factory):
    from tirodhan.modules.business_admin.commands import RiderEdit, RiderRoleEdit
    from tirodhan.modules.routing_operations.commands import require_live_role

    factory = database_session_factory
    actor = await manager(factory)
    rider = new_uuid7()
    async with factory() as session, session.begin():
        session.add(AppUser(user_id=rider, status="ACTIVE"))
    profile = RiderEdit(
        client_command_id=new_uuid7(),
        resource_id=rider,
        expected_version=0,
        reason="RIDER_ONBOARDING",
        status="ACTIVE",
    )
    await change(factory, actor, profile)
    role = RiderRoleEdit(
        client_command_id=new_uuid7(),
        resource_id=new_uuid7(),
        expected_version=0,
        reason="RIDER_ONBOARDING",
        rider_id=rider,
        active=True,
    )
    await change(factory, actor, role)
    async with factory() as session:
        await require_live_role(session, rider, "RIDER")
    await change(
        factory,
        actor,
        role.model_copy(
            update={
                "client_command_id": new_uuid7(),
                "expected_version": 1,
                "active": False,
            }
        ),
    )
    await change(
        factory,
        actor,
        profile.model_copy(
            update={
                "client_command_id": new_uuid7(),
                "expected_version": 1,
                "status": "SUSPENDED",
            }
        ),
    )
    async with factory() as session:
        with pytest.raises(OperationalInputError, match="LIVE_AUTHORIZATION"):
            await require_live_role(session, rider, "RIDER")
        history = await compare(session, "rider_profile", rider, 1, 2)
        assert history["changes"]["status"]["after"] == "SUSPENDED"
        assert history["after"]["actor_id"] == actor
        assert len((await versions(session, "user_role", role.resource_id))["versions"]) == 2


async def test_receiving_postgis_guards_and_reused_duty_history(database_session_factory):
    from test_operational_orchestration import regional

    from tirodhan.modules.business_admin.commands import ReceivingEdit
    from tirodhan.modules.business_admin.events import events
    from tirodhan.modules.business_admin.history import current_version
    from tirodhan.modules.routing_operations.commands import WindowDeclare

    factory = database_session_factory
    data = await regional(factory, empty=True)
    point, actor = data[2], data[0].manager_id
    async with factory() as session:
        revision = await current_version(session, "receiving_point", point.receiving_point_id)
        timeline = await versions(session, "fleet_control", f"REGION:{data[7]}")
        # The old regional fixture does not configure a parallel coordinator.
        assert timeline["current_revision"] == 0
    edit = ReceivingEdit(
        client_command_id=new_uuid7(),
        resource_id=point.receiving_point_id,
        expected_version=revision,
        reason="RECEIVING_CONFIGURATION",
        official_name="Revised point",
        official_identifier=point.official_identifier,
        longitude=77.211,
        latitude=28.630,
        allowed_radius_m=point.allowed_radius_m,
        status="ACTIVE",
        unloading_capacity=2,
    )
    await change(factory, actor, edit)
    async with factory() as session:
        snapshot = (await versions(session, "receiving_point", point.receiving_point_id))[
            "versions"
        ][-1]
        assert snapshot["resulting_state"]["longitude"] == 77.211
        assert "location" not in snapshot["resulting_state"]
    await data[5](
        WindowDeclare(
            client_command_id=new_uuid7(),
            receiving_point_id=point.receiving_point_id,
            starts_at=data[6][0] + timedelta(days=1),
            ends_at=data[6][0] + timedelta(days=2),
        )
    )
    async with factory() as session:
        windows = await events(session, "receiving_window", point.receiving_point_id, limit=1)
        assert windows["history_semantics"] == "IMMUTABLE_FACTS"
        assert windows["events"][0]["actor_id"] == actor
        assert windows["next_cursor"]


async def test_configured_price_snapshot_and_completed_booking_replay(database_session_factory):
    from scheduling_helpers import TestSlotAvailability
    from test_collection_payment import SLOT_END, SLOT_START, create_context, create_user

    from tirodhan.modules.collection_requests.service import (
        CreateCollectionRequestCommand,
        create_collection_request,
    )
    from tirodhan.modules.payments.models import Payment

    factory = database_session_factory
    actor = await manager(factory)
    g = group()
    await change(factory, actor, g)
    await change(
        factory,
        actor,
        CategoryEdit(
            client_command_id=new_uuid7(),
            resource_id=new_uuid7(),
            expected_version=0,
            reason="CATALOGUE_CONFIGURATION",
            category_code="PRICE_ITEM",
            group_id=g.resource_id,
            display_name="Configured",
            description="Synthetic",
            display_order=1,
            active=True,
            quantity_input="OPTIONAL",
            weight_input="OPTIONAL",
        ),
    )
    price = PriceEdit(
        client_command_id=new_uuid7(),
        resource_id=new_uuid7(),
        expected_version=0,
        reason="MVP_FLAT_FEE",
        amount_minor=12345,
        active=True,
    )
    await change(factory, actor, price)
    customer = await create_user(factory)
    context = await create_context(factory, customer.user_id)
    command = CreateCollectionRequestCommand(
        customer_id=customer.user_id,
        client_request_id=new_uuid7(),
        serviceability_context_id=context.serviceability_context_id,
        slot_start=SLOT_START,
        slot_end=SLOT_END,
        items=(DeclaredRequestItem("PRICE_ITEM", 17, 10000),),
        payment_expires_at=utc_now() + timedelta(minutes=30),
    )
    original = await create_collection_request(
        factory,
        command,
        FlatPickupPricing(factory),
        idempotency_expires_at=utc_now() + timedelta(days=1),
        slot_availability=TestSlotAvailability(),
        planning_lead_time_minutes=4,
    )
    await change(
        factory,
        actor,
        price.model_copy(
            update={
                "client_command_id": new_uuid7(),
                "expected_version": 1,
                "amount_minor": 23456,
            }
        ),
    )

    class NoRequote:
        async def quote(self, items):
            raise AssertionError("completed booking replay must not reprice")

    replay = await create_collection_request(
        factory, command, NoRequote(), idempotency_expires_at=utc_now() + timedelta(days=1)
    )
    assert replay.request.request_id == original.request.request_id
    assert replay.request.quoted_amount_minor == 12345
    async with factory() as session:
        payment = await session.scalar(
            select(Payment).where(Payment.request_id == original.request.request_id)
        )
        assert payment.amount_minor == 12345 and payment.currency == "INR"


async def test_manager_http_history_images_and_live_authorization(database_session_factory):
    from httpx import ASGITransport, AsyncClient
    from test_operational_api import _application, _headers, _token_for

    from tirodhan.modules.evidence.media_policy import ConfiguredMediaPolicy
    from tirodhan.modules.evidence.media_ports import StoredObjectProperties, UploadAuthorization

    factory = database_session_factory
    actor = await manager(factory)
    token, _ = await _token_for(factory, actor)

    class ProductStorage:
        async def create_product_upload_authorization(self, **kwargs):
            return UploadAuthorization("synthetic-create-only", utc_now() + timedelta(minutes=5))

        async def inspect_object(self, **kwargs):
            return StoredObjectProperties(content_type="image/jpeg", size_bytes=1024)

    app = _application(
        "postgresql+asyncpg://unused",
        media_storage=ProductStorage(),
        media_policy=ConfiguredMediaPolicy(
            allowed_content_types={"PHOTO": ["image/jpeg"]},
            maximum_size_bytes={"PHOTO": 2048},
        ),
    )
    app.state.database_session_factory = factory
    prefix = "/v1/manager/business"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        command = group()
        assert (
            await client.post(f"{prefix}/commands", json=command.model_dump(mode="json"))
        ).status_code == 401
        response = await client.post(
            f"{prefix}/commands", json=command.model_dump(mode="json"), headers=_headers(token)
        )
        assert response.status_code == 200, response.text
        path = f"{prefix}/catalogue_group/{command.resource_id}"
        assert (await client.get(f"{path}/versions/1", headers=_headers(token))).json()[
            "actor_id"
        ] == str(actor)
        assert (
            await client.get(f"{path}/compare?before=1&after=1", headers=_headers(token))
        ).json()["changes"] == {}
        image = new_uuid7()
        upload = await client.post(
            f"{prefix}/product-images/upload",
            headers=_headers(token),
            json={"client_command_id": str(image), "content_type": "image/jpeg"},
        )
        assert upload.status_code == 200 and upload.json()["overwrite_allowed"] is False
        payload = {
            "operation": "CATALOGUE_MEDIA",
            "client_command_id": str(image),
            "resource_id": str(image),
            "expected_version": 0,
            "reason": "PRODUCT_IMAGE",
            "width": 640,
            "height": 480,
            "alt_text": "Synthetic image",
        }
        assert (
            await client.post(f"{prefix}/commands", headers=_headers(token), json=payload)
        ).status_code == 200
        assert (
            await client.post(f"{prefix}/commands", headers=_headers(token), json=payload)
        ).status_code == 200
        assert (
            await client.post(
                f"{prefix}/product-images/upload",
                headers=_headers(token),
                json={"client_command_id": str(image), "content_type": "image/jpeg"},
            )
        ).status_code == 409
        async with factory() as session, session.begin():
            role = await session.scalar(select(UserRole).where(UserRole.user_id == actor))
            role.revoked_at = utc_now()
        assert (await client.get(f"{path}/versions", headers=_headers(token))).status_code == 403


async def test_reused_fleet_membership_and_duty_changes_have_attributable_history(
    database_session_factory,
):
    from test_operational_orchestration import operational, regional

    from tirodhan.modules.business_admin.events import events
    from tirodhan.modules.dispatch.fleet_service import FleetCommand, execute_fleet_command
    from tirodhan.modules.routing_operations.commands import ShiftClose
    from tirodhan.modules.routing_operations.models import OperatingShift
    from tirodhan.modules.routing_operations.operational_commands import ConfirmShift

    factory = database_session_factory
    data = await regional(factory, empty=True)
    actor, rider = data[0].manager_id, data[0].rider_ids[0]

    async def fleet_command(command):
        return await execute_fleet_command(
            factory,
            manager_id=actor,
            command=command,
            idempotency_expires_at=utc_now() + timedelta(days=1),
        )

    fleet = await fleet_command(
        FleetCommand(operation="CREATE", client_command_id=new_uuid7(), name="Synthetic fleet")
    )
    member = await fleet_command(
        FleetCommand(
            operation="JOIN", client_command_id=new_uuid7(), fleet_id=fleet, rider_id=rider
        )
    )
    leave = FleetCommand(
        operation="LEAVE",
        client_command_id=new_uuid7(),
        fleet_id=fleet,
        rider_id=rider,
        membership_id=member,
    )
    await fleet_command(leave)
    assert await fleet_command(leave) == member
    async with factory() as session:
        history = await compare(session, "fleet_membership", member, 1, 2)
        assert history["changes"]["left_at"]["before"] is None
        assert history["after"]["actor_id"] == actor
        assert history["after"]["command_id"] == leave.client_command_id
        shift = await session.scalar(select(OperatingShift).where(OperatingShift.rider_id == rider))
    confirmation = ConfirmShift(client_command_id=new_uuid7(), shift_id=shift.shift_id)
    await operational(factory, rider, confirmation, shift.starts_at - timedelta(seconds=1))
    close = ShiftClose(client_command_id=new_uuid7(), shift_id=shift.shift_id)
    await data[5](close)
    async with factory() as session:
        history = await versions(session, "operating_shift", shift.shift_id)
        assert len(history["versions"]) == 2 and history["versions"][-1]["actor_id"] == actor
        assert history["versions"][-1]["command_id"] == close.client_command_id
        facts = await events(session, "shift_confirmation", shift.shift_id)
        assert facts["events"][0]["command_id"] == confirmation.client_command_id
        assert facts["events"][0]["recorded_at"] is not None


async def test_live_commitments_guard_receiving_vehicle_and_region_edits(database_session_factory):
    from test_regional_parallel import prepared

    from tirodhan.modules.business_admin.commands import ReceivingEdit, RegionEdit, VehicleEdit
    from tirodhan.modules.business_admin.history import current_version
    from tirodhan.modules.routing_operations.orchestration_models import OperatingRegion, RegionCell

    factory = database_session_factory
    data, _ = await prepared(factory)
    actor, point = data[0].manager_id, data[2]
    async with factory() as session:
        point_version = await current_version(session, "receiving_point", point.receiving_point_id)
        vehicle_version = await current_version(session, "operating_vehicle", data[1][0])
        region_version = await current_version(session, "operating_region", data[7])
        region = await session.get(OperatingRegion, data[7])
        cells = tuple(
            await session.scalars(select(RegionCell.cell_id).where(RegionCell.region_id == data[7]))
        )
    commands = [
        ReceivingEdit(
            client_command_id=new_uuid7(),
            resource_id=point.receiving_point_id,
            expected_version=point_version,
            reason="FACILITY_CONFIGURATION",
            official_name=point.official_name,
            official_identifier=point.official_identifier,
            longitude=77.211,
            latitude=28.630,
            allowed_radius_m=point.allowed_radius_m,
            status="INACTIVE",
        ),
        VehicleEdit(
            client_command_id=new_uuid7(),
            resource_id=data[1][0],
            expected_version=vehicle_version,
            reason="VEHICLE_CONFIGURATION",
            mass_capacity_g=1,
            volume_capacity_ml=1,
            profile_version="test-car",
            end_receiving_point_id=point.receiving_point_id,
        ),
        RegionEdit(
            client_command_id=new_uuid7(),
            resource_id=data[7],
            expected_version=region_version,
            reason="REGION_CONFIGURATION",
            name=region.name,
            cells=cells,
            receiving_point_ids=(point.receiving_point_id,),
            default_warehouse_id=point.receiving_point_id,
        ),
    ]
    for command in commands:
        with pytest.raises(OperationalInputError, match="COMMITMENTS_REQUIRE_RECOVERY"):
            await change(factory, actor, command)
