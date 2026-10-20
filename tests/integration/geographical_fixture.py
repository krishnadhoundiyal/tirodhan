"""Synthetic inputs through real resource, slot-hold, booking and payment domain commands."""

import json
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import h3
from geoalchemy2.elements import WKTElement
from sqlalchemy import insert
from test_collection_payment import ITEMS, FixedPricing, create_ready_attempt, success_event

from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.collection_requests.service import (
    CreateCollectionRequestCommand,
    create_collection_request,
)
from tirodhan.modules.dispatch.models import RiderAvailability, RiderProfile
from tirodhan.modules.identity.models import AppUser, UserRole
from tirodhan.modules.payments.service import process_authenticated_payment_event
from tirodhan.modules.receiving_points.models import ReceivingPoint
from tirodhan.modules.routing_operations.allowance_estimator import AllowancePolicy
from tirodhan.modules.routing_operations.capacity_models import RegionalSlotProjection
from tirodhan.modules.routing_operations.commands import (
    PositionReport,
    ShiftOpen,
    VehicleCreate,
    WindowDeclare,
    execute_resource_command,
)
from tirodhan.modules.routing_operations.economics import EconomicPolicy
from tirodhan.modules.routing_operations.operational_commands import (
    ConfigureRegion,
    ConfirmShift,
    execute_operational,
)
from tirodhan.modules.routing_operations.regional_planner import RegionalPolicy, configure_region
from tirodhan.modules.routing_operations.rolling import FleetPolicy
from tirodhan.modules.routing_operations.slot_holds import HoldSlot, hold_slot
from tirodhan.modules.routing_operations.slot_projection import refresh_slots
from tirodhan.modules.routing_operations.telemetry import TelemetryPolicy
from tirodhan.modules.serviceability.models import ServiceabilityContext

DATA = json.loads(
    (Path(__file__).parents[1] / "routing/data/geographical-delhi-eligible-nodes.json").read_text()
)


async def operational(factory, actor, command, now):
    return await execute_operational(
        factory, actor_id=actor, command=command, expires_at=now + timedelta(days=3), now=now
    )


async def resource(factory, manager, command, now):
    return await execute_resource_command(
        factory, manager_id=manager, command=command, expires_at=now + timedelta(days=3), now=now
    )


async def setup_region(
    factory,
    name,
    fleet_size,
    horizon,
    *,
    index=0,
    public_nodes=None,
    extra_receiving_points=(),
    confirmed=True,
    mass_capacity_g=100000,
    volume_capacity_ml=100000,
    facility_node=None,
):
    now = utc_now().replace(microsecond=0)
    local = now.astimezone(ZoneInfo("Asia/Kolkata"))
    epoch = (local + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    points = public_nodes or DATA["regions"][name]["nodes"]
    manager, point = new_uuid7(), new_uuid7()
    riders = [new_uuid7() for _ in range(fleet_size)]
    origin = facility_node or points[-fleet_size]
    async with factory() as session, session.begin():
        await session.execute(
            insert(AppUser), [dict(user_id=u, status="ACTIVE") for u in [manager, *riders]]
        )
        await session.execute(
            insert(UserRole),
            [dict(user_id=manager, role_code="MANAGER", granted_at=now)]
            + [dict(user_id=r, role_code="RIDER", granted_at=now) for r in riders],
        )
        await session.execute(
            insert(RiderProfile), [dict(rider_id=r, status="ACTIVE") for r in riders]
        )
        await session.execute(
            insert(RiderAvailability),
            [
                dict(
                    rider_id=r,
                    availability_intent="AVAILABLE",
                    work_state="IDLE",
                    version=1,
                    updated_at=now,
                )
                for r in riders
            ],
        )
        session.add(
            ReceivingPoint(
                receiving_point_id=point,
                official_name=f"Synthetic QA {name}",
                official_identifier=f"QA-{point}",
                location=location(origin),
                allowed_radius_m=250,
                status="ACTIVE",
                version=1,
                created_at=now,
                updated_at=now,
            )
        )
    await resource(
        factory,
        manager,
        WindowDeclare(
            client_command_id=new_uuid7(),
            receiving_point_id=point,
            starts_at=epoch - timedelta(hours=1),
            ends_at=epoch + timedelta(hours=9),
        ),
        now,
    )
    vehicles, shifts = [], []
    for i, rider in enumerate(riders):
        vehicle = await resource(
            factory,
            manager,
            VehicleCreate(
                client_command_id=new_uuid7(),
                mass_capacity_g=mass_capacity_g,
                volume_capacity_ml=volume_capacity_ml,
                profile_version="qa-car",
                end_receiving_point_id=point,
            ),
            now,
        )
        shift = await resource(
            factory,
            manager,
            ShiftOpen(
                client_command_id=new_uuid7(),
                vehicle_id=vehicle,
                rider_id=rider,
                resource_kind="COMMITTED",
                commitment_id=new_uuid7(),
                starts_at=epoch - timedelta(minutes=5),
                ends_at=epoch + timedelta(hours=8),
            ),
            now,
        )
        if confirmed:
            await operational(
                factory, rider, ConfirmShift(client_command_id=new_uuid7(), shift_id=shift), now
            )
        p = points[-fleet_size + i]
        await resource(
            factory,
            manager,
            PositionReport(
                client_command_id=new_uuid7(),
                vehicle_id=vehicle,
                longitude=p["longitude"],
                latitude=p["latitude"],
                observed_at=epoch,
            ),
            epoch,
        )
        vehicles.append(vehicle)
        shifts.append(shift)
    policy = FleetPolicy(
        version="FLEET_TEST_1",
        minimum_interval_seconds=1,
        displacement_m=200,
        destination_divergence_m=100,
        publication_attempts=3,
        search_seconds=1,
        telemetry=TelemetryPolicy(
            max_age_seconds=3600, max_accuracy_m=30, max_speed_m_per_second=30
        ),
        economics=EconomicPolicy(
            version="SYNTHETIC_SECONDS_1",
            additional_vehicle_seconds=300,
            disruption_seconds=10,
            waiting_weight=0,
            window_risk_buffer_seconds=300,
            window_risk_weight=1,
            max_candidate_evaluations=3000,
        ),
        regional=True,
        horizon_seconds=horizon,
        planning_lead_seconds=horizon,
        max_requests=128,
        max_nodes=512,
        max_vehicles=32,
        max_profiles=1,
        lease_seconds=3600,
        maximum_interval_seconds=3600,
        delivery_ack_seconds=600,
    )
    cells = tuple(sorted({h3.latlng_to_cell(p["latitude"], p["longitude"], 7) for p in points}))
    region = await operational(
        factory,
        manager,
        ConfigureRegion(
            client_command_id=new_uuid7(),
            name=f"QA {name} {index}",
            cells=cells,
            vehicle_ids=tuple(vehicles),
            receiving_point_ids=(point, *extra_receiving_points),
            default_warehouse_id=point,
            fleet=policy,
            slot_allowance=AllowancePolicy(
                version=1,
                local_travel_seconds=120,
                clustered_travel_seconds=60,
                facility_travel_seconds=1500,
                stem_seconds=0,
                cluster_threshold=2,
                pickup_start_offset_seconds=900,
                limited_bookings=1,
            ),
        ),
        now,
    )
    regional = RegionalPolicy(
        fleet=policy,
        processes=4,
        max_admitted=400,
        neighbourhood_size=12,
        capacity_multiplier=2,
        max_discovery=10000,
        max_adjustments=200,
        selection_seconds=5,
        starvation_seconds=600,
    )
    await configure_region(
        factory, region_id=region, manager_id=manager, policy=regional, now=epoch
    )
    return dict(
        name=name,
        region_id=region,
        manager=manager,
        vehicles=vehicles,
        riders=riders,
        shifts=shifts,
        epoch=epoch,
        clock=epoch,
        point=point,
        nodes=points,
        horizon=horizon,
        policy=policy,
        bookings=[],
        fleet_size=fleet_size,
        origin=origin,
        service_cells=cells,
    )


def location(p):
    return WKTElement(f"POINT({p['longitude']} {p['latitude']})", srid=4326)


async def arrivals(factory, region, count, cycle):
    now = utc_now()
    # Six-hour appointment book, distinct from the rolling 2/3/4-hour routing horizon.
    customer = new_uuid7()
    nodes = region["nodes"]
    contexts = []
    async with factory() as session, session.begin():
        session.add(AppUser(user_id=customer, status="ACTIVE"))
        await session.flush()
        session.add(UserRole(user_id=customer, role_code="CUSTOMER", granted_at=now))
        for i in range(count):
            ordinal = len(region["bookings"]) + i
            node = nodes[ordinal % 240]
            context = new_uuid7()
            session.add(
                ServiceabilityContext(
                    serviceability_context_id=context,
                    user_id=customer,
                    address_snapshot_encrypted=b"synthetic-public-osm-QA",
                    location=location(node),
                    cell_id=h3.latlng_to_cell(node["latitude"], node["longitude"], 7),
                    status="SERVICEABLE",
                    expires_at=now + timedelta(days=2),
                    created_at=now,
                    resolved_at=now,
                )
            )
            contexts.append((context, node, ordinal))
    records = []
    held = []
    # One authoritative forecast, then atomic holds against the same shared inventory.
    # Booking/payment changes invalidate it; therefore this phase precedes checkout.
    await refresh_slots(factory, region["region_id"], now=utc_now(), days=2, freshness_seconds=3600)
    for context, node, ordinal in contexts:
        now = utc_now()
        slot = region["epoch"] + timedelta(minutes=30 + 30 * (ordinal % 12))
        async with factory() as session:
            projection = await session.get(RegionalSlotProjection, (region["region_id"], slot))
            assert projection is not None, "Missing actual slot projection"
            assert projection.status in {"AVAILABLE", "LIMITED"}, {
                "region": region["name"],
                "ordinal": ordinal,
                "projection": {
                    c.name: getattr(projection, c.name) for c in projection.__table__.columns
                },
            }
        hold = HoldSlot(
            client_command_id=new_uuid7(),
            context_id=context,
            slot_start=slot,
            slot_end=slot + timedelta(minutes=30),
            mass_g=1000,
            volume_ml=1000,
            pickup_seconds=180,
            discharge_seconds=60,
            expected_generation=projection.generation,
        )
        await hold_slot(
            factory,
            customer_id=customer,
            command=hold,
            expires_at=now + timedelta(hours=2),
            now=now,
        )
        held.append((context, node, ordinal, hold))
    for context, node, ordinal, hold in held:
        now = utc_now()
        slot = hold.slot_start
        command = CreateCollectionRequestCommand(
            customer_id=customer,
            client_request_id=new_uuid7(),
            serviceability_context_id=context,
            slot_start=hold.slot_start,
            slot_end=hold.slot_end,
            items=ITEMS,
            payment_expires_at=now + timedelta(minutes=30),
            capacity_reservation_id=hold.client_command_id,
        )
        result = await create_collection_request(
            factory,
            command,
            FixedPricing(),
            idempotency_expires_at=now + timedelta(days=2),
            require_capacity_reservation=True,
            planning_lead_time_minutes=15,
        )
        attempt, _ = await create_ready_attempt(
            factory,
            customer,
            result.request.request_id,
            key=str(new_uuid7()),
            order_id=str(new_uuid7()),
        )
        capture = success_event(attempt, str(new_uuid7()))
        await process_authenticated_payment_event(
            factory, capture, payload_hash=b"synthetic-QA-capture", planning_lead_time_minutes=15
        )
        # Replay actual capture: must not create another acceptance or assignment.
        await process_authenticated_payment_event(
            factory, capture, payload_hash=b"synthetic-QA-capture", planning_lead_time_minutes=15
        )
        records.append(
            dict(
                booking_id=str(result.request.request_id),
                region=region["name"],
                region_id=str(region["region_id"]),
                cycle=cycle,
                ordinal=ordinal,
                arrival_at=(region["clock"] + timedelta(microseconds=ordinal)).isoformat(),
                slot_start=slot.isoformat(),
                slot_end=hold.slot_end.isoformat(),
                **node,
            )
        )
    region["bookings"].extend(records)
    return records
