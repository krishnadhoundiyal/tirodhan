from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from geoalchemy2 import Geometry
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import and_, cast, exists, false, func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.dispatch.models import (
    RiderAssignment,
    RiderAssignmentItem,
    RiderAvailability,
    RiderProfile,
)
from tirodhan.modules.handovers.models import HandoverEvent, HandoverEventItem
from tirodhan.modules.identity.models import AppUser, UserRole
from tirodhan.modules.pickups.models import PickupAttempt
from tirodhan.modules.planning.models import PickupExecution
from tirodhan.modules.planning.routing.contracts import (
    ROUTING_CONTRACT_V1,
    ROUTING_CONTRACT_V2,
    CargoBalance,
    CargoLot,
    ReceivingPointSnapshot,
    ResourceKind,
    RoadMatrix,
    RoutingLimits,
    RoutingProblem,
    RoutingStop,
    StopKind,
    TimeWindow,
    VehicleSnapshot,
)
from tirodhan.modules.planning.routing.osrm import RoadNode
from tirodhan.modules.planning.routing.validation import validate_problem
from tirodhan.modules.receiving_points.models import ReceivingPoint

from .admission_queries import in_ids
from .commands import OperationalInputError, Positive
from .dispatch_models import RouteRevision, RouteRevisionStop, RouteTask, VehicleRoute
from .fleet_models import FacilityRestriction, FleetException
from .models import (
    CargoVehicleObservation,
    OperatingShift,
    OperatingVehicle,
    ReceivingWindow,
    RoutingManifest,
    VehiclePosition,
)
from .orchestration_models import CargoTransfer


class ShadowConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    epoch: AwareDatetime
    horizon_end: AwareDatetime
    policy_version: str = Field(min_length=1, max_length=100)
    max_position_age_seconds: Positive
    max_requests: int = Field(strict=True, ge=1)
    max_vehicles: int = Field(strict=True, ge=1)
    max_nodes: int = Field(strict=True, ge=1)
    max_profiles: int = Field(strict=True, ge=1)
    search_seconds: int = Field(strict=True, ge=1, le=30)
    lease_seconds: int = Field(strict=True, ge=60, le=3600)
    fleet_mode: bool = False
    as_of: AwareDatetime | None = None
    regional: bool = False
    dispatch_readiness_seconds: int = Field(default=0, strict=True, ge=0, le=3600)
    region_id: UUID | None = None
    request_ids: tuple[UUID, ...] | None = None
    confirmed_shifts: bool = False
    admission_mode: bool = False
    pickup_horizon_end: AwareDatetime | None = None

    def stable_json(self) -> str:
        added = {
            "as_of": self.as_of,
            "regional": self.regional,
            "region_id": self.region_id,
            "request_ids": self.request_ids,
            "confirmed_shifts": self.confirmed_shifts,
            "admission_mode": self.admission_mode,
            "dispatch_readiness_seconds": self.dispatch_readiness_seconds,
            "pickup_horizon_end": self.pickup_horizon_end,
        }
        return self.model_dump_json(
            exclude={k for k, v in added.items() if v is None or v is False or v == 0}
        )

    @model_validator(mode="after")
    def ordered(self) -> ShadowConfiguration:
        if (
            self.max_requests > 128
            or self.max_vehicles > 32
            or self.max_nodes > 512
            or self.max_profiles > 8
        ) and not ((self.confirmed_shifts or self.admission_mode) and self.region_id is not None):
            raise ValueError("large discovery requires a regional owner")
        if (
            (self.pickup_horizon_end is not None and self.pickup_horizon_end > self.horizon_end)
            or self.horizon_end <= self.epoch
            or self.horizon_end.timestamp() - self.epoch.timestamp() > 86400
        ):
            raise ValueError("scope must be between zero and one day")
        return self


class MatrixSource(Protocol):
    async def acquire(self, nodes: tuple[RoadNode, ...]) -> RoadMatrix: ...


@dataclass(frozen=True)
class OperationalSnapshot:
    sources: dict[str, Any]
    fingerprint: bytes


def source_digest(
    sources: dict[str, Any],
    *,
    business_only: bool = False,
    position_vehicle_ids: set[UUID] | None = None,
) -> bytes:
    data = dict(sources)
    data["vehicles"] = [
        {
            k: value
            for k, value in v.items()
            if k
            not in (
                {"version", "position_id", "observed_at"}
                | (
                    {"longitude", "latitude"}
                    if business_only
                    or (
                        position_vehicle_ids is not None
                        and v["vehicle_id"] not in position_vehicle_ids
                    )
                    else set()
                )
            )
        }
        for v in sources["vehicles"]
    ]
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, default=str, separators=(",", ":")).encode()
    ).digest()


def _coordinates(column: Any) -> tuple[Any, Any]:
    geometry = cast(column, Geometry)
    return func.ST_X(geometry).label("longitude"), func.ST_Y(geometry).label("latitude")


def _seconds(value: datetime, epoch: datetime, *, start: bool = False) -> int:
    # Conservative integer projection: start upward, end/deadline downward.
    seconds = (value - epoch).total_seconds()
    return math.ceil(seconds) if start else math.floor(seconds)


async def rider_has_obligations(session: AsyncSession, rider: UUID) -> bool:
    from .orchestration_models import CargoTransfer

    handed = exists().where(
        HandoverEventItem.pickup_execution_id == PickupExecution.pickup_execution_id,
        HandoverEventItem.status == "VALIDATED",
    )
    latest_custodian = (
        select(CargoTransfer.to_rider_id)
        .where(CargoTransfer.request_id == PickupExecution.request_id)
        .order_by(CargoTransfer.sequence.desc())
        .limit(1)
        .correlate(PickupExecution)
        .scalar_subquery()
    )
    owned = exists().where(
        RiderAssignmentItem.assignment_id == RiderAssignment.assignment_id,
        RiderAssignmentItem.pickup_execution_id == PickupExecution.pickup_execution_id,
        RiderAssignmentItem.released_at.is_(None),
        RiderAssignment.rider_id == rider,
    )
    return bool(
        await session.scalar(
            select(
                exists().where(
                    ~handed,
                    or_(
                        and_(PickupExecution.status != "COLLECTED", owned),
                        and_(
                            PickupExecution.status == "COLLECTED",
                            or_(latest_custodian == rider, and_(latest_custodian.is_(None), owned)),
                        ),
                    ),
                )
            )
        )
    )


async def capture_snapshot(
    factory: async_sessionmaker[AsyncSession], config: ShadowConfiguration
) -> OperationalSnapshot:
    # One MVCC snapshot across positions, authorization, bookings, ownership and custody.
    async with factory() as session:
        await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        async with session.begin_nested():
            sources = await _read_sources(session, config)
        await session.rollback()
    digest = hashlib.sha256(
        json.dumps(sources, sort_keys=True, default=str, separators=(",", ":")).encode()
    ).digest()
    if config.fleet_mode:
        digest = source_digest(sources)
    return OperationalSnapshot(sources, digest)


async def _read_sources(session: AsyncSession, config: ShadowConfiguration) -> dict[str, Any]:
    from .orchestration_models import (
        AdmissionFlexibleCommitment,
        AppointmentConsent,
        FlexibleCommitment,
        RegionCell,
        RegionVehicle,
        ShiftExtension,
    )

    extended_end = (
        select(func.max(ShiftExtension.ends_at))
        .where(ShiftExtension.shift_id == OperatingShift.shift_id)
        .correlate(OperatingShift)
        .scalar_subquery()
    )
    effective_end = func.greatest(
        OperatingShift.ends_at, func.coalesce(extended_end, OperatingShift.ends_at)
    )
    latest = (
        select(VehiclePosition.position_id)
        .where(VehiclePosition.vehicle_id == OperatingVehicle.vehicle_id)
        .order_by(VehiclePosition.revision.desc())
        .limit(1)
        .correlate(OperatingVehicle)
        .scalar_subquery()
    )
    role = exists().where(
        UserRole.user_id == OperatingShift.rider_id,
        UserRole.role_code == "RIDER",
        UserRole.revoked_at.is_(None),
    )
    vehicles = [
        dict(row)
        for row in (
            await session.execute(
                select(
                    OperatingVehicle.vehicle_id,
                    OperatingVehicle.mass_capacity_g,
                    OperatingVehicle.volume_capacity_ml,
                    OperatingVehicle.profile_version,
                    OperatingVehicle.end_receiving_point_id,
                    OperatingVehicle.version,
                    OperatingVehicle.safety_version,
                    OperatingVehicle.blocked_reason,
                    OperatingShift.shift_id,
                    OperatingShift.resource_kind,
                    OperatingShift.rider_id,
                    OperatingShift.starts_at,
                    OperatingShift.ends_at,
                    OperatingShift.commitment_id,
                    VehiclePosition.position_id,
                    VehiclePosition.observed_at,
                    *_coordinates(VehiclePosition.location),
                    RiderProfile.status.label("rider_status"),
                    AppUser.status.label("user_status"),
                    role.label("has_role"),
                    exists()
                    .where(
                        RiderAssignment.rider_id == OperatingShift.rider_id,
                        RiderAssignment.status == "ACTIVE",
                        RiderAssignment.source != "COMMITTED_DISPATCH",
                    )
                    .label("legacy_assignment"),
                    RiderAvailability.work_state,
                    RiderAvailability.availability_intent,
                )
                .join(OperatingShift, OperatingShift.vehicle_id == OperatingVehicle.vehicle_id)
                .join(RiderProfile, RiderProfile.rider_id == OperatingShift.rider_id)
                .join(AppUser, AppUser.user_id == OperatingShift.rider_id)
                .outerjoin(RiderAvailability, RiderAvailability.rider_id == OperatingShift.rider_id)
                .outerjoin(
                    VehiclePosition,
                    VehiclePosition.position_id == latest,
                )
                .where(
                    exists().where(
                        RegionVehicle.vehicle_id == OperatingVehicle.vehicle_id,
                        RegionVehicle.region_id == config.region_id,
                    )
                    if config.region_id
                    else exists().where(RegionVehicle.vehicle_id == OperatingVehicle.vehicle_id)
                    if config.regional
                    else true(),
                    OperatingShift.closed_at.is_(None),
                    or_(
                        OperatingShift.resource_kind == "COMMITTED",
                        exists().where(FlexibleCommitment.shift_id == OperatingShift.shift_id),
                        exists().where(
                            AdmissionFlexibleCommitment.shift_id == OperatingShift.shift_id
                        ),
                        exists().where(
                            CargoTransfer.to_vehicle_id == OperatingVehicle.vehicle_id,
                            CargoTransfer.to_rider_id == OperatingShift.rider_id,
                        ),
                    ),
                    effective_end > config.epoch,
                    OperatingShift.starts_at < config.horizon_end,
                )
                .order_by(OperatingVehicle.vehicle_id)
                .limit(None if config.admission_mode else config.max_vehicles + 1)
            )
        ).mappings()
    ]
    if not config.admission_mode and len(vehicles) > config.max_vehicles:
        raise OperationalInputError("VEHICLE_SCOPE_LIMIT")
    riders = [v["rider_id"] for v in vehicles]
    collector = (
        select(
            PickupAttempt.pickup_execution_id,
            PickupAttempt.pickup_attempt_id,
            RiderAssignment.rider_id.label("collector_id"),
        )
        .join(RiderAssignment, RiderAssignment.assignment_id == PickupAttempt.rider_assignment_id)
        .where(PickupAttempt.outcome == "COLLECTED")
        .subquery()
    )
    current = aliased(RiderAssignment)
    handed = exists().where(
        HandoverEventItem.pickup_execution_id == PickupExecution.pickup_execution_id,
        HandoverEventItem.status == "VALIDATED",
        HandoverEventItem.handover_event_id == HandoverEvent.handover_event_id,
        HandoverEvent.status == "VALIDATED",
    )
    regional_custody = (
        CargoVehicleObservation.vehicle_id.in_(
            select(RegionVehicle.vehicle_id).where(RegionVehicle.region_id == config.region_id)
        )
        if config.confirmed_shifts
        else false()
    )
    requests = [
        dict(row)
        for row in (
            await session.execute(
                select(
                    CollectionRequest.request_id,
                    CollectionRequest.status,
                    CollectionRequest.slot_start,
                    CollectionRequest.slot_end,
                    exists()
                    .where(
                        FleetException.request_id == CollectionRequest.request_id,
                        FleetException.reason == "LOAD_UNCERTAIN",
                    )
                    .label("load_uncertain"),
                    *_coordinates(CollectionRequest.pickup_location),
                    PickupExecution.pickup_execution_id,
                    PickupExecution.status.label("pickup_status"),
                    current.rider_id.label("owner_id"),
                    current.assignment_id,
                    collector.c.collector_id,
                    collector.c.pickup_attempt_id,
                    RoutingManifest.mass_g,
                    RoutingManifest.volume_ml,
                    RoutingManifest.pickup_seconds,
                    RoutingManifest.discharge_seconds,
                    RoutingManifest.discharge_deadline,
                    RoutingManifest.receiving_point_id,
                    CargoVehicleObservation.vehicle_id.label("cargo_vehicle_id"),
                    CargoVehicleObservation.pickup_attempt_id.label("observed_attempt_id"),
                )
                .outerjoin(
                    PickupExecution, PickupExecution.request_id == CollectionRequest.request_id
                )
                .outerjoin(
                    RiderAssignmentItem,
                    and_(
                        RiderAssignmentItem.pickup_execution_id
                        == PickupExecution.pickup_execution_id,
                        RiderAssignmentItem.released_at.is_(None),
                    ),
                )
                .outerjoin(current, current.assignment_id == RiderAssignmentItem.assignment_id)
                .outerjoin(
                    collector,
                    collector.c.pickup_execution_id == PickupExecution.pickup_execution_id,
                )
                .outerjoin(
                    RoutingManifest, RoutingManifest.request_id == CollectionRequest.request_id
                )
                .outerjoin(
                    CargoVehicleObservation,
                    CargoVehicleObservation.request_id == CollectionRequest.request_id,
                )
                .where(
                    or_(
                        CollectionRequest.status.in_(("ACCEPTED", "PRE_PLANNING", "PLANNED")),
                        PickupExecution.status == "COLLECTED",
                        in_ids(current.rider_id, riders),
                    ),
                    or_(
                        (
                            exists().where(
                                RegionCell.cell_id == CollectionRequest.cell_id,
                                RegionCell.region_id == config.region_id,
                            )
                            if config.region_id
                            else exists().where(RegionCell.cell_id == CollectionRequest.cell_id)
                        ),
                        in_ids(current.rider_id, riders),
                        regional_custody,
                        exists().where(
                            CargoTransfer.request_id == CollectionRequest.request_id,
                            in_ids(CargoTransfer.to_rider_id, riders),
                        ),
                    )
                    if config.regional
                    else true(),
                    ~handed,
                    or_(
                        and_(
                            or_(
                                PickupExecution.status.is_(None),
                                PickupExecution.status != "COLLECTED",
                            ),
                            or_(
                                and_(
                                    CollectionRequest.slot_start
                                    < (config.pickup_horizon_end or config.horizon_end),
                                    true()
                                    if config.confirmed_shifts
                                    else CollectionRequest.slot_end >= config.epoch,
                                ),
                                in_ids(current.rider_id, riders),
                                exists().where(
                                    AppointmentConsent.request_id == CollectionRequest.request_id,
                                    AppointmentConsent.slot_end >= config.epoch,
                                    AppointmentConsent.slot_start
                                    < (config.pickup_horizon_end or config.horizon_end),
                                ),
                            ),
                        ),
                        and_(
                            PickupExecution.status == "COLLECTED",
                            or_(
                                in_ids(collector.c.collector_id, riders),
                                regional_custody,
                                exists().where(
                                    CargoTransfer.request_id == CollectionRequest.request_id,
                                    in_ids(CargoTransfer.to_rider_id, riders),
                                ),
                                in_ids(
                                    CargoVehicleObservation.vehicle_id,
                                    [v["vehicle_id"] for v in vehicles],
                                ),
                            ),
                        ),
                    ),
                )
                .where(
                    or_(
                        in_ids(CollectionRequest.request_id, config.request_ids),
                        in_ids(current.rider_id, riders),
                        PickupExecution.status == "COLLECTED",
                    )
                    if config.confirmed_shifts and config.request_ids is not None
                    else in_ids(CollectionRequest.request_id, config.request_ids)
                    if config.request_ids is not None
                    else true()
                )
                .order_by(CollectionRequest.request_id)
                .limit(None if config.admission_mode else config.max_requests + 1)
            )
        ).mappings()
    ]
    if not config.admission_mode and len(requests) > config.max_requests:
        raise OperationalInputError("REQUEST_SCOPE_LIMIT")
    from .regional_projection import project

    operational_inputs = await project(session, config, vehicles, requests)
    point_ids = {v["end_receiving_point_id"] for v in vehicles} | {
        r["receiving_point_id"] for r in requests if r["receiving_point_id"]
    }
    if config.regional:
        from .orchestration_models import RegionReceivingPoint

        point_ids.update(
            await session.scalars(
                select(RegionReceivingPoint.receiving_point_id).where(
                    RegionReceivingPoint.region_id == config.region_id
                )
                if config.region_id
                else select(RegionReceivingPoint.receiving_point_id).where(
                    in_ids(
                        RegionReceivingPoint.region_id,
                        {v.get("region_id") for v in vehicles}
                        | {r.get("region_id") for r in requests},
                    )
                )
            )
        )
    points = [
        dict(row)
        for row in (
            await session.execute(
                select(
                    ReceivingPoint.receiving_point_id,
                    ReceivingPoint.status,
                    ReceivingPoint.unloading_capacity,
                    ReceivingPoint.version,
                    ReceivingPoint.official_identifier,
                    ReceivingPoint.allowed_radius_m,
                    *_coordinates(ReceivingPoint.location),
                )
                .where(in_ids(ReceivingPoint.receiving_point_id, point_ids))
                .order_by(ReceivingPoint.receiving_point_id)
            )
        ).mappings()
    ]
    windows = [
        dict(row)
        for row in (
            await session.execute(
                select(
                    ReceivingWindow.window_id,
                    ReceivingWindow.receiving_point_id,
                    ReceivingWindow.starts_at,
                    ReceivingWindow.ends_at,
                )
                .where(
                    in_ids(ReceivingWindow.receiving_point_id, point_ids),
                    ReceivingWindow.ends_at >= config.epoch,
                    ReceivingWindow.starts_at <= config.horizon_end,
                )
                .order_by(
                    ReceivingWindow.receiving_point_id,
                    ReceivingWindow.starts_at,
                    ReceivingWindow.window_id,
                )
                .limit(None if config.admission_mode else config.max_nodes * 16 + 1)
            )
        ).mappings()
    ]
    if not config.admission_mode and len(windows) > config.max_nodes * 16:
        raise OperationalInputError("RECEIVING_WINDOW_SCOPE_LIMIT")
    routes = [
        dict(row)
        for row in (
            await session.execute(
                select(
                    VehicleRoute.route_id,
                    VehicleRoute.vehicle_id,
                    VehicleRoute.shift_id,
                    VehicleRoute.assignment_id,
                    VehicleRoute.version,
                    VehicleRoute.status,
                )
                .where(
                    in_ids(VehicleRoute.vehicle_id, [v["vehicle_id"] for v in vehicles]),
                    VehicleRoute.status != "CLOSED",
                )
                .order_by(VehicleRoute.vehicle_id)
            )
        ).mappings()
    ]
    tasks = [
        dict(row)
        for row in (
            await session.execute(
                select(
                    RouteTask.task_id,
                    RouteTask.route_id,
                    RouteTask.stop_id,
                    RouteTask.state,
                    RouteTask.kind,
                    RouteTask.started_at,
                    RouteTask.committed_at,
                    RouteRevisionStop.ordinal,
                )
                .join(RouteRevisionStop, RouteRevisionStop.task_id == RouteTask.task_id)
                .join(RouteRevision, RouteRevision.revision_id == RouteRevisionStop.revision_id)
                .join(VehicleRoute, VehicleRoute.route_id == RouteRevision.route_id)
                .where(
                    in_ids(VehicleRoute.route_id, [r["route_id"] for r in routes]),
                    RouteRevision.version == VehicleRoute.version,
                    RouteTask.state.in_(("PENDING", "STARTED")),
                )
                .order_by(RouteTask.route_id, RouteRevisionStop.ordinal)
            )
        ).mappings()
    ]
    released = [
        dict(row)
        for row in (
            await session.execute(
                select(VehicleRoute.vehicle_id, PickupExecution.request_id)
                .join(
                    RiderAssignmentItem,
                    RiderAssignmentItem.assignment_id == VehicleRoute.assignment_id,
                )
                .join(
                    PickupExecution,
                    PickupExecution.pickup_execution_id == RiderAssignmentItem.pickup_execution_id,
                )
                .where(
                    in_ids(VehicleRoute.route_id, [r["route_id"] for r in routes]),
                    RiderAssignmentItem.released_at.is_not(None),
                    in_ids(PickupExecution.request_id, [r["request_id"] for r in requests]),
                )
                .order_by(VehicleRoute.vehicle_id, PickupExecution.request_id)
            )
        ).mappings()
    ]
    restrictions = [
        dict(row)
        for row in (
            await session.execute(
                select(
                    FacilityRestriction.restriction_id,
                    FacilityRestriction.receiving_point_id,
                    FacilityRestriction.starts_at,
                    FacilityRestriction.ends_at,
                    FacilityRestriction.reason,
                )
                .where(
                    in_ids(FacilityRestriction.receiving_point_id, point_ids),
                    FacilityRestriction.ends_at >= config.epoch,
                    FacilityRestriction.starts_at <= config.horizon_end,
                )
                .order_by(FacilityRestriction.restriction_id)
            )
        ).mappings()
    ]
    orphaned = [
        dict(row)
        for row in (
            await session.execute(
                select(
                    VehicleRoute.route_id,
                    VehicleRoute.vehicle_id,
                    VehicleRoute.version,
                    VehicleRoute.status,
                    OperatingShift.rider_id,
                )
                .join(OperatingShift, OperatingShift.shift_id == VehicleRoute.shift_id)
                .where(
                    exists().where(
                        RegionVehicle.vehicle_id == VehicleRoute.vehicle_id,
                        RegionVehicle.region_id == config.region_id,
                    )
                    if config.region_id
                    else true(),
                    VehicleRoute.status != "CLOSED",
                    or_(
                        effective_end <= config.epoch,
                        OperatingShift.closed_at.is_not(None),
                    ),
                )
                .order_by(VehicleRoute.route_id)
                .limit(None if config.admission_mode else config.max_vehicles + 1)
            )
        ).mappings()
    ]
    if not config.admission_mode and len(orphaned) > config.max_vehicles:
        raise OperationalInputError("RECOVERY_SCOPE_LIMIT")
    external_facility_visits = []
    if config.confirmed_shifts:
        external_facility_visits = [
            dict(row)
            for row in (
                await session.execute(
                    select(
                        RouteTask.receiving_point_id,
                        RouteRevisionStop.planned_start,
                        RouteRevisionStop.planned_end,
                    )
                    .join(RouteRevisionStop, RouteRevisionStop.task_id == RouteTask.task_id)
                    .join(RouteRevision, RouteRevision.revision_id == RouteRevisionStop.revision_id)
                    .join(VehicleRoute, VehicleRoute.route_id == RouteRevision.route_id)
                    .where(
                        RouteTask.kind == "DISCHARGE",
                        RouteTask.state.in_(("PENDING", "STARTED")),
                        VehicleRoute.status != "CLOSED",
                        VehicleRoute.vehicle_id.not_in([v["vehicle_id"] for v in vehicles]),
                        VehicleRoute.version == RouteRevision.version,
                        in_ids(
                            RouteTask.receiving_point_id,
                            [
                                point["receiving_point_id"]
                                for point in points
                                if point.get("unloading_capacity") is not None
                            ],
                        ),
                        RouteRevisionStop.planned_end > config.epoch,
                    )
                    .order_by(
                        RouteTask.receiving_point_id,
                        RouteRevisionStop.planned_start,
                        RouteTask.task_id,
                    )
                )
            ).mappings()
        ]
    return {
        "vehicles": vehicles,
        "requests": requests,
        "points": points,
        "windows": windows,
        "routes": routes,
        "tasks": tasks,
        "released": released,
        "restrictions": restrictions,
        "orphaned_routes": orphaned,
        "operational_inputs": operational_inputs,
        **(
            {"external_facility_visits": external_facility_visits}
            if config.confirmed_shifts
            else {}
        ),
    }


async def build_problem(
    snapshot: OperationalSnapshot,
    config: ShadowConfiguration,
    run_id: UUID,
    providers: Mapping[str, MatrixSource],
    *,
    now: datetime,
    route_orders: dict[UUID, tuple[UUID, ...]] | None = None,
    insertion_lot: UUID | None = None,
) -> RoutingProblem:
    epoch = config.epoch.astimezone(timezone.utc)
    if epoch.microsecond or config.horizon_end.microsecond:
        raise OperationalInputError("WHOLE_SECOND_SCOPE_REQUIRED")
    data = snapshot.sources
    vehicles, requests = data["vehicles"], data["requests"]
    if not vehicles:
        raise OperationalInputError("NO_COMMITTED_CAPACITY")
    by_rider = {v["rider_id"]: v["vehicle_id"] for v in vehicles}
    profiles = sorted({v["profile_version"] for v in vehicles})
    if not config.admission_mode and len(profiles) > config.max_profiles:
        raise OperationalInputError("PROFILE_SCOPE_LIMIT")
    nodes: dict[UUID, RoadNode] = {}
    for v in vehicles:
        if v["user_status"] != "ACTIVE" or v["rider_status"] != "ACTIVE" or not v["has_role"]:
            raise OperationalInputError("RESOURCE_AUTHORIZATION_STALE")
        if (
            v["position_id"] is None
            or not 0 <= (now - v["observed_at"]).total_seconds() <= config.max_position_age_seconds
        ):
            raise OperationalInputError("FRESH_POSITION_REQUIRED")
        nodes[v["position_id"]] = RoadNode(v["position_id"], v["longitude"], v["latitude"])
    for p in data["points"]:
        nodes[p["receiving_point_id"]] = RoadNode(
            p["receiving_point_id"], p["longitude"], p["latitude"]
        )
    lots: list[CargoLot] = []
    stops: list[RoutingStop] = []
    cargo: dict[UUID, list[CargoBalance]] = {v["vehicle_id"]: [] for v in vehicles}
    by_vehicle = {v["vehicle_id"]: v for v in vehicles}
    recovery = {r["vehicle_id"] for r in data["routes"] if r["status"] == "RECOVERY_REQUIRED"}
    tasks_by_stop = {t["stop_id"]: t for t in data["tasks"]}
    protected = {
        t["stop_id"]
        for t in data["tasks"]
        if t["state"] == "STARTED" or t["committed_at"] is not None
    }
    released_pairs = {(r["vehicle_id"], r["request_id"]) for r in data["released"]}
    accepted = {v["vehicle_id"]: set(v.get("accepted_requests", ())) for v in vehicles}
    eligible = tuple(
        v["vehicle_id"]
        for v in vehicles
        if v["blocked_reason"] is None
        and (v["vehicle_id"] not in recovery or v.get("resume_authorized"))
    )
    for r in requests:
        if r["load_uncertain"]:
            raise OperationalInputError("AUDITED_LOAD_CORRECTION_REQUIRED")
        if r["mass_g"] is None:
            raise OperationalInputError("MANIFEST_REQUIRED")
        request_id = r["request_id"]
        discharge_id = uuid5(NAMESPACE_URL, f"tirodhan:discharge:{request_id}")
        bound = by_rider.get(r["owner_id"])
        collected = r["pickup_status"] == "COLLECTED"
        if collected != (r["collector_id"] is not None):
            raise OperationalInputError("PICKUP_CUSTODY_INCONSISTENT")
        if not collected and r["status"] not in ("ACCEPTED", "PRE_PLANNING", "PLANNED"):
            raise OperationalInputError("INCUMBENT_BOOKING_STATE_CONFLICT")
        if collected:
            bound = by_rider.get(r["collector_id"])
            if (
                bound is None
                or r["cargo_vehicle_id"] != bound
                or r["observed_attempt_id"] != r["pickup_attempt_id"]
            ):
                raise OperationalInputError("CARGO_VEHICLE_OBSERVATION_REQUIRED")
            cargo[bound].append(CargoBalance(request_id, 1))
        elif r["owner_id"] is not None and bound is None:
            raise OperationalInputError("INCUMBENT_RESOURCE_OUTSIDE_SCOPE")
        protected_lot = (
            discharge_id in protected
            or uuid5(NAMESPACE_URL, f"tirodhan:pickup:{request_id}") in protected
        )
        if config.fleet_mode and not collected and not protected_lot:
            bound = None  # Future uncommitted ownership may move only in activation's transaction.
        pickup_id = None if collected else uuid5(NAMESPACE_URL, f"tirodhan:pickup:{request_id}")
        deadline = _seconds(r["discharge_deadline"], epoch)
        task = tasks_by_stop.get(pickup_id)
        started = task if task and task["state"] == "STARTED" else None
        if deadline < 0 or (not collected and r["slot_end"] < epoch and started is None):
            raise OperationalInputError("COMMITMENT_WINDOW_EXPIRED")
        lots.append(
            CargoLot(
                request_id,
                request_id,
                1,
                "HOUSEHOLD_LOT",
                (r["mass_g"], r["volume_ml"]),
                (r["receiving_point_id"],),
                deadline,
                pickup_id,
            )
        )
        region_eligible = tuple(
            v
            for v in eligible
            if not config.regional
            or (
                by_vehicle[v].get("region_id") == r.get("region_id")
                and r.get("region_id") is not None
                and (
                    by_vehicle[v]["resource_kind"] == "COMMITTED" or r["request_id"] in accepted[v]
                )
            )
        )
        if r.get("replacement_shift_id") is not None and not collected:
            region_eligible = tuple(
                v for v in region_eligible if by_vehicle[v]["shift_id"] == r["replacement_shift_id"]
            )
        allowed = (
            (bound,)
            if bound
            else tuple(
                v
                for v in region_eligible
                if (v, r.get("reattempt_of") or request_id) not in released_pairs
            )
        )
        if pickup_id:
            nodes[request_id] = RoadNode(request_id, r["longitude"], r["latitude"])
            stops.append(
                RoutingStop(
                    pickup_id,
                    request_id,
                    StopKind.PICKUP,
                    request_id,
                    1,
                    r["pickup_seconds"],
                    TimeWindow(0, _seconds(config.horizon_end, epoch))
                    if started
                    else TimeWindow(
                        max(
                            0,
                            _seconds(r["slot_start"], epoch, start=True),
                            _seconds(
                                now + timedelta(seconds=config.dispatch_readiness_seconds),
                                epoch,
                                start=True,
                            )
                            if config.regional and r["owner_id"] is None
                            else 0,
                            _seconds(
                                r.get("retry_eligible_at", r["slot_start"]), epoch, start=True
                            ),
                        ),
                        _seconds(r["slot_end"], epoch),
                    ),
                    config.admission_mode
                    or r.get("hold", False)
                    or not config.fleet_mode
                    or r["owner_id"] is not None,
                    allowed,
                    bound,
                    None,
                )
            )
        stops.append(
            RoutingStop(
                discharge_id,
                r["receiving_point_id"],
                StopKind.DISCHARGE,
                request_id,
                1,
                r["discharge_seconds"],
                TimeWindow(0, deadline),
                config.admission_mode
                or r.get("hold", False)
                or not config.fleet_mode
                or collected
                or r["owner_id"] is not None,
                allowed,
                bound,
                r["receiving_point_id"],
            )
        )
    if not config.admission_mode and len(nodes) > config.max_nodes:
        raise OperationalInputError("NODE_SCOPE_LIMIT")
    matrices: list[RoadMatrix] = []
    ordered_nodes = tuple(nodes[key] for key in sorted(nodes, key=str))
    for profile in profiles:
        if profile not in providers:
            raise OperationalInputError("MATRIX_PROFILE_NOT_CONFIGURED")
        source = providers[profile]
        if route_orders is not None:
            from .admission_roads import DirectedRoadCache

            cache = source if isinstance(source, DirectedRoadCache) else DirectedRoadCache(source)
            by_stop = {s.stop_id: s for s in stops}
            new_nodes = {s.node_id for s in stops if s.lot_id == insertion_lot}
            pairs: set[tuple[UUID, UUID]] = set()
            for v in vehicles:
                if v["profile_version"] != profile:
                    continue
                order = route_orders.get(v["vehicle_id"], ())
                path = (
                    (v["position_id"],)
                    + tuple(by_stop[s].node_id for s in order if s in by_stop)
                    + (v["end_receiving_point_id"],)
                )
                pairs.update(zip(path, path[1:], strict=False))
                pairs.update((a, b) for a in path for b in new_nodes)
                pairs.update((a, b) for a in new_nodes for b in path)
            pairs.update((a, b) for a in new_nodes for b in new_nodes)
            matrix = await cache.acquire_arcs(ordered_nodes, pairs)
        else:
            matrix = await source.acquire(ordered_nodes)
        if matrix.profile_version != profile or matrix.node_ids != tuple(
            n.node_id for n in ordered_nodes
        ):
            raise OperationalInputError("MATRIX_PROVENANCE_MISMATCH")
        matrices.append(matrix)
    by_profile = {m.profile_version: m.matrix_id for m in matrices}
    facility_snapshots = []
    for p in data["points"]:
        ranges = sorted(
            (
                max(0, _seconds(w["starts_at"], epoch, start=True)),
                min(_seconds(config.horizon_end, epoch), _seconds(w["ends_at"], epoch)),
            )
            for w in data["windows"]
            if w["receiving_point_id"] == p["receiving_point_id"]
            and w["ends_at"] >= epoch
            and w["starts_at"] <= config.horizon_end
        )
        # Union overlapping operator-declared opening intervals; no fabricated 24-hour opening.
        merged: list[TimeWindow] = []
        for start, end in ranges:
            if merged and start <= merged[-1].end:
                merged[-1] = TimeWindow(merged[-1].start, max(end, merged[-1].end))
            else:
                merged.append(TimeWindow(start, end))
        for restriction in data["restrictions"]:
            if restriction["receiving_point_id"] != p["receiving_point_id"]:
                continue
            blocked_start = _seconds(restriction["starts_at"], epoch)
            blocked_end = _seconds(restriction["ends_at"], epoch, start=True)
            intervals: list[TimeWindow] = []
            for window in merged:
                if blocked_end <= window.start or blocked_start >= window.end:
                    intervals.append(window)
                else:
                    if window.start < blocked_start:
                        intervals.append(TimeWindow(window.start, blocked_start))
                    if blocked_end < window.end:
                        intervals.append(TimeWindow(blocked_end, window.end))
            merged = intervals
        if not merged and not data["restrictions"]:
            raise OperationalInputError("RECEIVING_WINDOWS_REQUIRED")
        facility_snapshots.append(
            ReceivingPointSnapshot(
                p["receiving_point_id"],
                p["receiving_point_id"],
                p["version"],
                p["status"] == "ACTIVE" and bool(merged),
                tuple(merged) if merged else (TimeWindow(0, 0),),
            )
        )
    vehicle_snapshots = []
    for v in vehicles:
        start = max(
            0, _seconds(v["starts_at"], epoch, start=True), _seconds(now, epoch, start=True)
        )
        end = min(_seconds(v["ends_at"], epoch), _seconds(config.horizon_end, epoch))
        if start > end:
            raise OperationalInputError("SHIFT_EXPIRED")
        vehicle_snapshots.append(
            VehicleSnapshot(
                v["vehicle_id"],
                v["rider_id"],
                v["version"],
                ResourceKind(v["resource_kind"]),
                v["shift_id"],
                by_profile[v["profile_version"]],
                v["position_id"],
                v["end_receiving_point_id"],
                start,
                TimeWindow(start, end),
                (v["mass_capacity_g"], v["volume_capacity_ml"]),
                tuple(cargo[v["vehicle_id"]]),
                tuple(
                    t["stop_id"]
                    for t in data["tasks"]
                    if (t["state"] == "STARTED" or t["committed_at"] is not None)
                    and t["route_id"]
                    in {r["route_id"] for r in data["routes"] if r["vehicle_id"] == v["vehicle_id"]}
                ),
                tuple(
                    TimeWindow(max(0, _seconds(a, epoch)), _seconds(b, epoch))
                    for a, b in v.get("breaks", ())
                    if b > epoch
                ),
            )
        )
    problem = RoutingProblem(
        run_id,
        run_id,
        "DYNAMIC_COLLECTION_ROUTING_V3"
        if any(m.sparse_arcs is not None for m in matrices)
        else ROUTING_CONTRACT_V2
        if any(v.breaks for v in vehicle_snapshots)
        else ROUTING_CONTRACT_V1,
        config.policy_version,
        epoch,
        ("MASS_G", "VOLUME_ML"),
        RoutingLimits(
            max(1, len(nodes)) if config.admission_mode else config.max_nodes,
            max(1, len(stops)) if config.admission_mode else config.max_requests * 2,
            max(1, len(vehicles)) if config.admission_mode else config.max_vehicles,
            max(1, len(matrices)) if config.admission_mode else config.max_profiles,
        ),
        tuple(matrices),
        tuple(vehicle_snapshots),
        tuple(facility_snapshots),
        tuple(lots),
        tuple(stops),
    )
    validate_problem(problem)
    return problem
