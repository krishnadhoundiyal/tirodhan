from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from geoalchemy2 import Geometry
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import and_, cast, exists, func, or_, select
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

from .commands import OperationalInputError, Positive
from .models import (
    CargoVehicleObservation,
    OperatingShift,
    OperatingVehicle,
    ReceivingWindow,
    RoutingManifest,
    VehiclePosition,
)


class ShadowConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    epoch: AwareDatetime
    horizon_end: AwareDatetime
    policy_version: str = Field(min_length=1, max_length=100)
    max_position_age_seconds: Positive
    max_requests: int = Field(strict=True, ge=1, le=128)
    max_vehicles: int = Field(strict=True, ge=1, le=32)
    max_nodes: int = Field(strict=True, ge=1, le=512)
    max_profiles: int = Field(strict=True, ge=1, le=8)
    search_seconds: int = Field(strict=True, ge=1, le=30)
    lease_seconds: int = Field(strict=True, ge=60, le=3600)

    @model_validator(mode="after")
    def ordered(self) -> ShadowConfiguration:
        if (
            self.horizon_end <= self.epoch
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


def _coordinates(column: Any) -> tuple[Any, Any]:
    geometry = cast(column, Geometry)
    return func.ST_X(geometry).label("longitude"), func.ST_Y(geometry).label("latitude")


def _seconds(value: datetime, epoch: datetime, *, start: bool = False) -> int:
    # Conservative integer projection: start upward, end/deadline downward.
    seconds = (value - epoch).total_seconds()
    return math.ceil(seconds) if start else math.floor(seconds)


async def rider_has_obligations(session: AsyncSession, rider: UUID) -> bool:
    handed = exists().where(
        HandoverEventItem.pickup_execution_id == PickupExecution.pickup_execution_id,
        HandoverEventItem.status == "VALIDATED",
        HandoverEventItem.handover_event_id == HandoverEvent.handover_event_id,
        HandoverEvent.status == "VALIDATED",
    )
    return bool(
        await session.scalar(
            select(
                exists().where(
                    RiderAssignment.rider_id == rider,
                    RiderAssignmentItem.assignment_id == RiderAssignment.assignment_id,
                    RiderAssignmentItem.pickup_execution_id == PickupExecution.pickup_execution_id,
                    RiderAssignmentItem.released_at.is_(None),
                    ~handed,
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
    return OperationalSnapshot(sources, digest)


async def _read_sources(session: AsyncSession, config: ShadowConfiguration) -> dict[str, Any]:
    latest = (
        select(VehiclePosition.vehicle_id, func.max(VehiclePosition.revision).label("revision"))
        .group_by(VehiclePosition.vehicle_id)
        .subquery()
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
                    OperatingShift.shift_id,
                    OperatingShift.rider_id,
                    OperatingShift.starts_at,
                    OperatingShift.ends_at,
                    VehiclePosition.position_id,
                    VehiclePosition.observed_at,
                    *_coordinates(VehiclePosition.location),
                    RiderProfile.status.label("rider_status"),
                    AppUser.status.label("user_status"),
                    role.label("has_role"),
                    RiderAvailability.work_state,
                    RiderAvailability.availability_intent,
                )
                .join(OperatingShift, OperatingShift.vehicle_id == OperatingVehicle.vehicle_id)
                .join(RiderProfile, RiderProfile.rider_id == OperatingShift.rider_id)
                .join(AppUser, AppUser.user_id == OperatingShift.rider_id)
                .outerjoin(RiderAvailability, RiderAvailability.rider_id == OperatingShift.rider_id)
                .outerjoin(latest, latest.c.vehicle_id == OperatingVehicle.vehicle_id)
                .outerjoin(
                    VehiclePosition,
                    and_(
                        VehiclePosition.vehicle_id == latest.c.vehicle_id,
                        VehiclePosition.revision == latest.c.revision,
                    ),
                )
                .where(
                    OperatingShift.closed_at.is_(None),
                    OperatingShift.resource_kind == "COMMITTED",
                    OperatingShift.ends_at > config.epoch,
                    OperatingShift.starts_at < config.horizon_end,
                )
                .order_by(OperatingVehicle.vehicle_id)
                .limit(config.max_vehicles + 1)
            )
        ).mappings()
    ]
    if len(vehicles) > config.max_vehicles:
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
    requests = [
        dict(row)
        for row in (
            await session.execute(
                select(
                    CollectionRequest.request_id,
                    CollectionRequest.status,
                    CollectionRequest.slot_start,
                    CollectionRequest.slot_end,
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
                        current.rider_id.in_(riders),
                    ),
                    ~handed,
                    or_(
                        and_(
                            or_(
                                PickupExecution.status.is_(None),
                                PickupExecution.status != "COLLECTED",
                            ),
                            or_(
                                and_(
                                    CollectionRequest.slot_start < config.horizon_end,
                                    CollectionRequest.slot_end >= config.epoch,
                                ),
                                current.rider_id.in_(riders),
                            ),
                        ),
                        and_(
                            PickupExecution.status == "COLLECTED",
                            or_(
                                collector.c.collector_id.in_(riders),
                                CargoVehicleObservation.vehicle_id.in_(
                                    [v["vehicle_id"] for v in vehicles]
                                ),
                            ),
                        ),
                    ),
                )
                .order_by(CollectionRequest.request_id)
                .limit(config.max_requests + 1)
            )
        ).mappings()
    ]
    if len(requests) > config.max_requests:
        raise OperationalInputError("REQUEST_SCOPE_LIMIT")
    point_ids = {v["end_receiving_point_id"] for v in vehicles} | {
        r["receiving_point_id"] for r in requests if r["receiving_point_id"]
    }
    points = [
        dict(row)
        for row in (
            await session.execute(
                select(
                    ReceivingPoint.receiving_point_id,
                    ReceivingPoint.status,
                    ReceivingPoint.version,
                    *_coordinates(ReceivingPoint.location),
                )
                .where(ReceivingPoint.receiving_point_id.in_(point_ids))
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
                    ReceivingWindow.receiving_point_id.in_(point_ids),
                    ReceivingWindow.ends_at >= config.epoch,
                    ReceivingWindow.starts_at <= config.horizon_end,
                )
                .order_by(
                    ReceivingWindow.receiving_point_id,
                    ReceivingWindow.starts_at,
                    ReceivingWindow.window_id,
                )
                .limit(config.max_nodes * 16 + 1)
            )
        ).mappings()
    ]
    if len(windows) > config.max_nodes * 16:
        raise OperationalInputError("RECEIVING_WINDOW_SCOPE_LIMIT")
    return {"vehicles": vehicles, "requests": requests, "points": points, "windows": windows}


async def build_problem(
    snapshot: OperationalSnapshot,
    config: ShadowConfiguration,
    run_id: UUID,
    providers: dict[str, MatrixSource],
    *,
    now: datetime,
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
    if len(profiles) > config.max_profiles:
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
    eligible = tuple(cargo)
    for r in requests:
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
        pickup_id = None if collected else uuid5(NAMESPACE_URL, f"tirodhan:pickup:{request_id}")
        deadline = _seconds(r["discharge_deadline"], epoch)
        if deadline < 0 or (not collected and r["slot_end"] < epoch):
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
        allowed = (bound,) if bound else eligible
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
                    TimeWindow(
                        max(0, _seconds(r["slot_start"], epoch, start=True)),
                        _seconds(r["slot_end"], epoch),
                    ),
                    True,
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
                True,
                allowed,
                bound,
                r["receiving_point_id"],
            )
        )
    if len(nodes) > config.max_nodes:
        raise OperationalInputError("NODE_SCOPE_LIMIT")
    matrices: list[RoadMatrix] = []
    ordered_nodes = tuple(nodes[key] for key in sorted(nodes, key=str))
    for profile in profiles:
        if profile not in providers:
            raise OperationalInputError("MATRIX_PROFILE_NOT_CONFIGURED")
        matrix = await providers[profile].acquire(ordered_nodes)
        if matrix.profile_version != profile or matrix.node_ids != tuple(
            n.node_id for n in ordered_nodes
        ):
            raise OperationalInputError("MATRIX_PROVENANCE_MISMATCH")
        matrices.append(matrix)
    by_profile = {m.profile_version: m.matrix_id for m in matrices}
    facility_snapshots = []
    for p in data["points"]:
        ranges = sorted(
            (max(0, _seconds(w["starts_at"], epoch, start=True)), _seconds(w["ends_at"], epoch))
            for w in data["windows"]
            if w["receiving_point_id"] == p["receiving_point_id"]
        )
        # Union overlapping operator-declared opening intervals; no fabricated 24-hour opening.
        merged: list[TimeWindow] = []
        for start, end in ranges:
            if merged and start <= merged[-1].end:
                merged[-1] = TimeWindow(merged[-1].start, max(end, merged[-1].end))
            else:
                merged.append(TimeWindow(start, end))
        if not merged:
            raise OperationalInputError("RECEIVING_WINDOWS_REQUIRED")
        facility_snapshots.append(
            ReceivingPointSnapshot(
                p["receiving_point_id"],
                p["receiving_point_id"],
                p["version"],
                p["status"] == "ACTIVE",
                tuple(merged),
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
                ResourceKind.COMMITTED,
                v["shift_id"],
                by_profile[v["profile_version"]],
                v["position_id"],
                v["end_receiving_point_id"],
                start,
                TimeWindow(start, end),
                (v["mass_capacity_g"], v["volume_capacity_ml"]),
                tuple(cargo[v["vehicle_id"]]),
                (),
            )
        )
    problem = RoutingProblem(
        run_id,
        run_id,
        ROUTING_CONTRACT_V1,
        config.policy_version,
        epoch,
        ("MASS_G", "VOLUME_ML"),
        RoutingLimits(
            config.max_nodes, config.max_requests * 2, config.max_vehicles, config.max_profiles
        ),
        tuple(matrices),
        tuple(vehicle_snapshots),
        tuple(facility_snapshots),
        tuple(lots),
        tuple(stops),
    )
    validate_problem(problem)
    return problem
