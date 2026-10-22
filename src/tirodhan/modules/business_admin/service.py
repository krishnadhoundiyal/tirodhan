"""Typed manager changes with row-version fencing and immutable transactional history."""

import hashlib
from datetime import datetime
from typing import Any
from uuid import UUID

import h3  # type: ignore[import-untyped]
from geoalchemy2 import Geometry
from sqlalchemy import cast, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.db.values import utc_now
from tirodhan.modules.customer_reads.models import (
    CatalogueArtwork,
    CatalogueCategory,
    CatalogueGroup,
    CatalogueMedia,
)
from tirodhan.modules.dispatch.models import Fleet, RiderAvailability, RiderProfile
from tirodhan.modules.identity.models import AppUser, UserRole
from tirodhan.modules.receiving_points.models import ReceivingPoint
from tirodhan.modules.reliability.primitives import (
    append_outbox_event,
    claim_idempotency_record,
    complete_idempotency_record,
)
from tirodhan.modules.routing_operations.capacity_commitments import has_capacity_commitments
from tirodhan.modules.routing_operations.commands import OperationalInputError, require_live_role
from tirodhan.modules.routing_operations.dispatch_models import VehicleRoute
from tirodhan.modules.routing_operations.models import OperatingVehicle
from tirodhan.modules.routing_operations.orchestration_models import (
    CapacityReservation,
    OperatingRegion,
    RegionCell,
    RegionReceivingPoint,
    RegionVehicle,
)

from .commands import (
    ArtworkEdit,
    BusinessCommand,
    CategoryEdit,
    Edit,
    FleetEdit,
    GroupEdit,
    MediaRegister,
    PriceEdit,
    ReceivingEdit,
    RegionEdit,
    RiderEdit,
    RiderRoleEdit,
    VehicleEdit,
    WeightPriceEdit,
)
from .history import attribute_change, current_version
from .models import PickupPrice, WeightPricingPolicy

MODELS: dict[type[Edit], tuple[Any, str]] = {
    GroupEdit: (CatalogueGroup, "group_id"),
    CategoryEdit: (CatalogueCategory, "category_id"),
    ArtworkEdit: (CatalogueArtwork, "artwork_id"),
    MediaRegister: (CatalogueMedia, "media_id"),
    PriceEdit: (PickupPrice, "price_id"),
    WeightPriceEdit: (WeightPricingPolicy, "policy_id"),
    RiderEdit: (RiderProfile, "rider_id"),
    RiderRoleEdit: (UserRole, "user_role_id"),
    ReceivingEdit: (ReceivingPoint, "receiving_point_id"),
    VehicleEdit: (OperatingVehicle, "vehicle_id"),
    RegionEdit: (OperatingRegion, "region_id"),
    FleetEdit: (Fleet, "fleet_id"),
}


async def region_has_promises(session: AsyncSession, region_id: UUID, now: datetime) -> bool:
    # Existing accepted reservations remain guarded, even if their original hold expired.
    from tirodhan.modules.collection_requests.models import CollectionRequest

    if await session.scalar(
        select(CollectionRequest.request_id)
        .join(RegionCell, RegionCell.cell_id == CollectionRequest.cell_id)
        .where(
            RegionCell.region_id == region_id,
            CollectionRequest.status.in_(("ACCEPTED", "PRE_PLANNING", "PLANNED")),
        )
        .limit(1)
    ):
        return True

    return bool(
        await session.scalar(
            select(CapacityReservation.reservation_id)
            .outerjoin(
                CollectionRequest, CollectionRequest.request_id == CapacityReservation.request_id
            )
            .where(
                CapacityReservation.region_id == region_id,
                (
                    (CapacityReservation.request_id.is_(None))
                    & (CapacityReservation.expires_at > now)
                )
                | CollectionRequest.status.in_(("ACCEPTED", "PRE_PLANNING", "PLANNED")),
            )
            .limit(1)
        )
    )


async def execute_business_command(
    factory: async_sessionmaker[AsyncSession],
    *,
    actor_id: UUID,
    command: BusinessCommand,
    expires_at: datetime,
    media_verified: bool = False,
    now: datetime | None = None,
) -> UUID:
    now = now or utc_now()
    async with factory() as session, session.begin():
        await session.connection(execution_options={"isolation_level": "SERIALIZABLE"})
        await require_live_role(session, actor_id, "MANAGER")
        claim = await claim_idempotency_record(
            session,
            scope=f"business.config:{actor_id}",
            idempotency_key=str(command.client_command_id),
            request_fingerprint=hashlib.sha256(command.model_dump_json().encode()).digest(),
            expires_at=expires_at,
            now=now,
        )
        if claim.record.status == "COMPLETED":
            assert claim.record.result_resource_id is not None
            return claim.record.result_resource_id
        if not claim.created:
            raise OperationalInputError("COMMAND_IN_PROGRESS")
        await attribute_change(session, actor_id, command.client_command_id, command.reason)
        if isinstance(command, (PriceEdit, WeightPriceEdit)):
            from .weight_pricing import lock_pricing

            await lock_pricing(session, exclusive=True)
        model, key = MODELS[type(command)]
        row = await session.scalar(
            select(model).where(getattr(model, key) == command.resource_id).with_for_update()
        )
        revision = await current_version(session, model.__tablename__, command.resource_id)
        if revision != command.expected_version or (row is None) != (revision == 0):
            raise OperationalInputError("BUSINESS_VERSION_CHANGED")
        values = command.model_dump(
            exclude={"operation", "client_command_id", "resource_id", "expected_version", "reason"}
        )
        if row is not None and isinstance(command, (GroupEdit, CategoryEdit)):
            code = "group_code" if isinstance(command, GroupEdit) else "category_code"
            if getattr(row, code) != getattr(command, code):
                raise OperationalInputError("CATALOGUE_CODE_IMMUTABLE")
        if isinstance(command, (RiderEdit, FleetEdit)):
            values["updated_at"] = now
        if isinstance(command, MediaRegister):
            if (
                row is not None
                or not media_verified
                or command.resource_id != command.client_command_id
            ):
                raise OperationalInputError("NEW_VERIFIED_PRODUCT_ASSET_REQUIRED")
            values["object_key"] = f"product-art/{command.resource_id}"
        if isinstance(command, RiderRoleEdit):
            await session.execute(
                select(AppUser).where(AppUser.user_id == command.rider_id).with_for_update()
            )
            if row is not None and (row.user_id != command.rider_id or row.role_code != "RIDER"):
                raise OperationalInputError("RIDER_ROLE_IDENTITY_IMMUTABLE")
            if row is not None:
                if not command.active and row.revoked_at is None:
                    row.revoked_at, row.revoked_by_user_id = now, actor_id
                elif command.active and row.revoked_at is not None:
                    raise OperationalInputError("NEW_ROLE_GRANT_ID_REQUIRED")
            else:
                if not command.active:
                    raise OperationalInputError("ROLE_GRANT_REQUIRED")
                session.add(
                    UserRole(
                        user_role_id=command.resource_id,
                        user_id=command.rider_id,
                        role_code="RIDER",
                        granted_at=now,
                        granted_by_user_id=actor_id,
                    )
                )
        else:
            if isinstance(command, RiderEdit) and row is None:
                values.update(created_at=now, updated_at=now)
                if await session.get(AppUser, command.resource_id) is None:
                    raise OperationalInputError("EXISTING_HUMAN_IDENTITY_REQUIRED")
                row = RiderProfile(rider_id=command.resource_id, **values)
                session.add(row)
                await session.flush([row])
                session.add(
                    RiderAvailability(
                        rider_id=command.resource_id,
                        availability_intent="OFFLINE",
                        work_state="IDLE",
                        version=1,
                        updated_at=now,
                    )
                )
            if isinstance(command, ReceivingEdit):
                if row is not None:
                    old_location = (
                        await session.execute(
                            select(
                                func.ST_X(cast(ReceivingPoint.location, Geometry)),
                                func.ST_Y(cast(ReceivingPoint.location, Geometry)),
                            ).where(ReceivingPoint.receiving_point_id == row.receiving_point_id)
                        )
                    ).one()
                    structural = any(
                        getattr(row, k) != values[k]
                        for k in (
                            "allowed_radius_m",
                            "status",
                            "unloading_capacity",
                            "official_identifier",
                        )
                    )
                    structural |= tuple(old_location) != (command.longitude, command.latitude)
                    if structural:
                        from sqlalchemy import exists

                        from tirodhan.modules.collection_requests.models import CollectionRequest
                        from tirodhan.modules.handovers.models import HandoverEventItem
                        from tirodhan.modules.planning.models import PickupExecution
                        from tirodhan.modules.routing_operations.models import RoutingManifest
                        from tirodhan.modules.routing_operations.orchestration_models import (
                            DischargeAuthorization,
                        )

                        amended_destination = (
                            select(DischargeAuthorization.receiving_point_id)
                            .where(DischargeAuthorization.request_id == RoutingManifest.request_id)
                            .order_by(
                                DischargeAuthorization.authorized_at.desc(),
                                DischargeAuthorization.authorization_id.desc(),
                            )
                            .limit(1)
                            .correlate(RoutingManifest)
                            .scalar_subquery()
                        )
                        handed = exists().where(
                            HandoverEventItem.pickup_execution_id
                            == PickupExecution.pickup_execution_id,
                            HandoverEventItem.status == "VALIDATED",
                        )
                        if await session.scalar(
                            select(RoutingManifest.request_id)
                            .join(CollectionRequest)
                            .outerjoin(
                                PickupExecution,
                                PickupExecution.request_id == RoutingManifest.request_id,
                            )
                            .where(
                                func.coalesce(
                                    amended_destination, RoutingManifest.receiving_point_id
                                )
                                == command.resource_id,
                                CollectionRequest.status.in_(
                                    ("ACCEPTED", "PRE_PLANNING", "PLANNED")
                                )
                                | ((PickupExecution.status == "COLLECTED") & ~handed),
                            )
                            .limit(1)
                        ):
                            raise OperationalInputError("RECEIVING_COMMITMENTS_REQUIRE_RECOVERY")
                        regions = list(
                            await session.scalars(
                                select(RegionReceivingPoint.region_id).where(
                                    RegionReceivingPoint.receiving_point_id == command.resource_id
                                )
                            )
                        )
                        if any([await region_has_promises(session, r, now) for r in regions]):
                            raise OperationalInputError("RECEIVING_COMMITMENTS_REQUIRE_RECOVERY")
                        if await session.scalar(
                            select(VehicleRoute.route_id)
                            .join(
                                OperatingVehicle,
                                OperatingVehicle.vehicle_id == VehicleRoute.vehicle_id,
                            )
                            .where(
                                OperatingVehicle.end_receiving_point_id == command.resource_id,
                                VehicleRoute.status != "CLOSED",
                            )
                            .limit(1)
                        ):
                            raise OperationalInputError("RECEIVING_COMMITMENTS_REQUIRE_RECOVERY")
                values.pop("longitude")
                values.pop("latitude")
                values["location"] = f"SRID=4326;POINT({command.longitude} {command.latitude})"
                if row is None:
                    values.update(created_at=now, updated_at=now, version=1)
                else:
                    values.update(version=row.version + 1, updated_at=now)
            if isinstance(command, VehicleEdit):
                if row is None:
                    raise OperationalInputError("USE_EXISTING_VEHICLE_CREATE_COMMAND")
                membership = await session.get(RegionVehicle, command.resource_id)
                if (
                    await has_capacity_commitments(session, command.resource_id, now)
                    or (
                        membership is not None
                        and await region_has_promises(session, membership.region_id, now)
                    )
                    or await session.scalar(
                        select(VehicleRoute.route_id)
                        .where(
                            VehicleRoute.vehicle_id == command.resource_id,
                            VehicleRoute.status != "CLOSED",
                        )
                        .limit(1)
                    )
                ):
                    raise OperationalInputError("VEHICLE_COMMITMENTS_REQUIRE_RECOVERY")
                values.update(version=row.version + 1, safety_version=row.safety_version + 1)
            if isinstance(command, RegionEdit):
                if row is None:
                    raise OperationalInputError("USE_EXISTING_REGION_CREATE_COMMAND")
                if await region_has_promises(session, row.region_id, now):
                    raise OperationalInputError("REGION_COMMITMENTS_REQUIRE_RECOVERY")
                vehicles = list(
                    await session.scalars(
                        select(RegionVehicle.vehicle_id)
                        .where(RegionVehicle.region_id == row.region_id)
                        .order_by(RegionVehicle.vehicle_id)
                    )
                )
                if any([await has_capacity_commitments(session, v, now) for v in vehicles]):
                    raise OperationalInputError("REGION_COMMITMENTS_REQUIRE_RECOVERY")
                if command.default_warehouse_id not in command.receiving_point_ids or not all(
                    h3.is_valid_cell(c) and h3.get_resolution(c) == 7 for c in command.cells
                ):
                    raise OperationalInputError("INVALID_REGION_COVERAGE")
                await session.execute(
                    delete(RegionCell).where(RegionCell.region_id == row.region_id)
                )
                await session.execute(
                    delete(RegionReceivingPoint).where(
                        RegionReceivingPoint.region_id == row.region_id
                    )
                )
                session.add_all(
                    [
                        RegionCell(region_id=row.region_id, cell_id=c)
                        for c in sorted(set(command.cells))
                    ]
                )
                session.add_all(
                    [
                        RegionReceivingPoint(region_id=row.region_id, receiving_point_id=p)
                        for p in sorted(set(command.receiving_point_ids))
                    ]
                )
                values = {
                    "name": command.name,
                    "default_warehouse_id": command.default_warehouse_id,
                    "version": row.version + 1,
                }
            if row is None:
                session.add(model(**{key: command.resource_id}, **values))
            else:
                for field, value in values.items():
                    setattr(row, field, value)
        await session.flush()
        await append_outbox_event(
            session,
            event_key=f"business.config:{command.client_command_id}",
            event_type="business.configuration.changed",
            aggregate_type=model.__tablename__,
            aggregate_id=command.resource_id,
            payload={"resource_id": str(command.resource_id)},
            available_at=now,
        )
        await complete_idempotency_record(
            session,
            claim.record,
            result_resource_id=command.resource_id,
            result_status_code=200,
            completed_at=now,
        )
        return command.resource_id
