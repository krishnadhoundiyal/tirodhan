"""Capacity certificates: solve outside locks, atomically reserve under regional CAS."""

from __future__ import annotations

import hashlib
import math
from dataclasses import replace
from datetime import datetime, timedelta
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.collection_requests.scheduling import validate_slot
from tirodhan.modules.planning.routing.adapters import ORTOOLS_VERSION, VROOM_VERSION
from tirodhan.modules.planning.routing.contracts import (
    OptimizerEngine,
    RouteOptimizer,
    RouteProposal,
)
from tirodhan.modules.planning.routing.execution import RoutingAdapterError
from tirodhan.modules.planning.routing.validation import RoutingValidationError, validate_proposal
from tirodhan.modules.serviceability.models import ServiceabilityContext

from .commands import OperationalInputError
from .economics import insertion_search, measure
from .fleet_priority import propose_prioritized
from .models import RoutingManifest
from .orchestration import wake
from .orchestration_models import CapacityReservation, OperatingRegion, RegionCell
from .rolling import FleetPolicy, incumbent_orders, ready_snapshot
from .service import PROPOSAL_CODEC
from .snapshot import (
    MatrixSource,
    OperationalSnapshot,
    _read_sources,
    build_problem,
    capture_snapshot,
    source_digest,
)


class ReserveCapacity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    client_command_id: UUID
    context_id: UUID
    reattempt_request_id: UUID | None = None
    slot_start: AwareDatetime
    slot_end: AwareDatetime
    mass_g: int = Field(strict=True, gt=0, le=2**31 - 1)
    volume_ml: int = Field(strict=True, gt=0, le=2**31 - 1)
    pickup_seconds: int = Field(strict=True, ge=0, le=86400)
    discharge_seconds: int = Field(strict=True, ge=0, le=86400)


async def reserve_capacity(
    factory: async_sessionmaker[AsyncSession],
    *,
    customer_id: UUID,
    command: ReserveCapacity,
    providers: dict[str, MatrixSource],
    optimizers: dict[OptimizerEngine, RouteOptimizer],
    expires_at: datetime,
    now: datetime | None = None,
    lease_token: UUID | None = None,
) -> UUID:
    now = now or utc_now()
    fingerprint = hashlib.sha256(command.model_dump_json().encode()).digest()
    async with factory() as session:
        existing = await session.scalar(
            select(CapacityReservation).where(
                CapacityReservation.reservation_id == command.client_command_id
            )
        )
        if existing is not None:
            if existing.customer_id != customer_id or existing.fingerprint != fingerprint:
                raise OperationalInputError("RESERVATION_REPLAY_CONFLICT")
            return existing.reservation_id
        validate_slot(command.slot_start, command.slot_end, now=now)
        original = (
            await session.get(CollectionRequest, command.reattempt_request_id)
            if command.reattempt_request_id
            else None
        )
        if command.reattempt_request_id:
            from tirodhan.modules.planning.models import PickupExecution

            pickup = await session.scalar(
                select(PickupExecution).where(
                    PickupExecution.request_id == command.reattempt_request_id
                )
            )
            if (
                original is None
                or original.customer_id != customer_id
                or original.serviceability_context_id != command.context_id
                or original.status not in ("ACCEPTED", "PLANNED")
                or (pickup is not None and pickup.status != "PENDING_ASSIGNMENT")
            ):
                raise OperationalInputError("REATtempt_BOOKING_REQUIRED".upper())
        context = await session.get(ServiceabilityContext, command.context_id)
        if (
            context is None
            or context.user_id != customer_id
            or (original is None and context.status != "SERVICEABLE")
            or (context.expires_at <= now and original is None)
        ):
            raise OperationalInputError("SERVICEABILITY_CONTEXT_REQUIRED")
        mapping = await session.get(RegionCell, original.cell_id if original else context.cell_id)
        if mapping is None:
            raise OperationalInputError("OPERATING_REGION_REQUIRED")
        region = await session.get(OperatingRegion, mapping.region_id)
        assert region is not None
        from .fleet_models import FleetControl

        control = await session.get(FleetControl, "COMMITTED_FLEET")
        if control is None or not control.configuration.get("regional", False):
            raise OperationalInputError("REGIONAL_FLEET_POLICY_REQUIRED")
        policy = FleetPolicy.model_validate(control.configuration)
        safety_percent = region.policy["safety_percent"]
        pickup_seconds = max(command.pickup_seconds, region.policy["minimum_pickup_seconds"])
        discharge_seconds = max(
            command.discharge_seconds, region.policy["minimum_discharge_seconds"]
        )
        version = region.version
    # Future bookings use declared future shifts, never a requirement for a current rider.
    epoch = max(now.replace(microsecond=0), command.slot_start - timedelta(hours=12))
    config = policy.snapshot_configuration(epoch).model_copy(
        update={
            "region_id": region.region_id,
            "regional": True,
            "admission_mode": True,
            "horizon_end": epoch + timedelta(days=1),
            "pickup_horizon_end": None,
            "as_of": now,
        }
    )
    snapshot = await capture_snapshot(factory, config)
    ready, issues = ready_snapshot(snapshot, config, now)
    if issues:
        raise OperationalInputError("REGIONAL_INPUTS_REQUIRE_ATTENTION")
    mass = (command.mass_g * safety_percent + 99) // 100
    volume = (command.volume_ml * safety_percent + 99) // 100
    # Location is server-owned; never accept a second client coordinate for the context.
    from .snapshot import _coordinates

    async with factory() as session:
        location = CollectionRequest.pickup_location if original else ServiceabilityContext.location
        selector = select(*_coordinates(location))
        selector = (
            selector.where(CollectionRequest.request_id == original.request_id)
            if original
            else selector.where(
                ServiceabilityContext.serviceability_context_id == command.context_id
            )
        )
        lon, lat = (await session.execute(selector)).one()

    candidate_sources = dict(ready.sources)
    candidate_sources["requests"] = [
        r
        for r in ready.sources["requests"]
        if original is None or r["request_id"] != original.request_id
    ] + [
        {
            "request_id": command.client_command_id,
            "status": "ACCEPTED",
            "hold": True,
            "reattempt_of": original.request_id if original else None,
            "region_id": region.region_id,
            "slot_start": command.slot_start,
            "slot_end": command.slot_end,
            "longitude": lon,
            "latitude": lat,
            "load_uncertain": False,
            "pickup_execution_id": None,
            "pickup_status": None,
            "owner_id": None,
            "assignment_id": None,
            "collector_id": None,
            "pickup_attempt_id": None,
            "mass_g": mass,
            "volume_ml": volume,
            "pickup_seconds": pickup_seconds,
            "discharge_seconds": discharge_seconds,
            "discharge_deadline": config.horizon_end,
            "receiving_point_id": region.default_warehouse_id,
            "cargo_vehicle_id": None,
            "observed_attempt_id": None,
        }
    ]
    problem = await build_problem(
        OperationalSnapshot(candidate_sources, b""), config, new_uuid7(), providers, now=now
    )
    orders = incumbent_orders(ready)
    candidates: list[RouteProposal] = []
    incremental = False
    # Exact-source incremental certificates avoid rerunning two solvers for every admission.
    async with factory() as session:
        previous = await session.scalar(
            select(CapacityReservation)
            .where(CapacityReservation.region_id == region.region_id)
            .order_by(
                CapacityReservation.created_at.desc(), CapacityReservation.reservation_id.desc()
            )
            .limit(1)
        )
    if previous is not None and previous.region_version == version:
        seed = PROPOSAL_CODEC.validate_python(previous.certificate["proposal"])
        valid_stops = {s.stop_id for s in problem.stops}
        if all(s in valid_stops for r in seed.routes for s in r.stop_ids):
            seed = replace(
                seed,
                problem_id=problem.problem_id,
                snapshot_id=problem.snapshot_id,
                unserved_stop_ids=tuple(valid_stops - {s for r in seed.routes for s in r.stop_ids}),
            )
            try:
                insertion_problem = replace(
                    problem,
                    stops=tuple(
                        replace(s, required=False) if s.lot_id == command.client_command_id else s
                        for s in problem.stops
                    ),
                )
                improved, _, _ = insertion_search(
                    insertion_problem, seed, policy.economics, orders, max_wall_seconds=10
                )
                validate_proposal(problem, improved)
                candidates.append(improved)
                incremental = True
            except RoutingValidationError:
                pass
    if not candidates:
        for engine in OptimizerEngine:
            try:
                candidate = await propose_prioritized(
                    optimizers[engine],
                    problem,
                    search_seconds=policy.search_seconds,
                    economics=policy.economics,
                )
                if (
                    candidate.engine != engine
                    or candidate.engine_version
                    != {
                        OptimizerEngine.VROOM: VROOM_VERSION,
                        OptimizerEngine.OR_TOOLS: ORTOOLS_VERSION,
                    }[engine]
                ):
                    raise RoutingValidationError("ENGINE_IDENTITY_MISMATCH")
                validate_proposal(problem, candidate)
                candidates.append(candidate)
            except (RoutingAdapterError, RoutingValidationError):
                continue
    if not candidates:
        raise OperationalInputError("NO_FEASIBLE_REGIONAL_CAPACITY")
    selected = min(candidates, key=lambda p: measure(problem, p, policy.economics, orders).rank)
    async with factory() as session:
        await session.connection(execution_options={"isolation_level": "SERIALIZABLE"})
        async with session.begin_nested():
            locked = await session.scalar(
                select(OperatingRegion)
                .where(OperatingRegion.region_id == region.region_id)
                .with_for_update()
            )
            assert locked is not None
            replay = await session.get(CapacityReservation, command.client_command_id)
            if replay is not None:
                if replay.customer_id != customer_id or replay.fingerprint != fingerprint:
                    raise OperationalInputError("RESERVATION_REPLAY_CONFLICT")
                return replay.reservation_id
            current = await _read_sources(session, config)
            if locked.version != version or source_digest(current) != source_digest(
                snapshot.sources
            ):
                raise OperationalInputError("CAPACITY_PROJECTION_STALE_RETRY")
            # A certificate must also fit the pilot coordinator's technical working set.
            # This is an infrastructure limit, not a cell booking-count capacity quota.
            global_sources = await _read_sources(
                session, config.model_copy(update={"region_id": None})
            )
            if len(global_sources["requests"]) + 1 > config.max_requests:
                raise OperationalInputError("PLANNING_WORKING_SET_REQUIRES_PARTITION")
            current_time = max(now, utc_now())
            if lease_token is not None:
                from .orchestration_models import CapacityAdmissionRequest

                job = await session.scalar(
                    select(CapacityAdmissionRequest)
                    .where(CapacityAdmissionRequest.admission_id == command.client_command_id)
                    .with_for_update()
                )
                if (
                    job is None
                    or job.lease_token != lease_token
                    or job.lease_until is None
                    or job.lease_until <= current_time
                ):
                    raise OperationalInputError("ADMISSION_LEASE_LOST")
            if expires_at <= current_time:
                raise OperationalInputError("CAPACITY_RESERVATION_EXPIRED")
            from .commands import require_live_role

            await require_live_role(session, customer_id, "CUSTOMER")
            fresh_context = await session.get(
                ServiceabilityContext, command.context_id, populate_existing=True
            )
            if (
                fresh_context is None
                or fresh_context.user_id != customer_id
                or (original is None and fresh_context.status != "SERVICEABLE")
                or (original is None and fresh_context.expires_at <= current_time)
            ):
                raise OperationalInputError("SERVICEABILITY_CONTEXT_CHANGED")
            current_lon, current_lat = (await session.execute(selector)).one()
            if (current_lon, current_lat) != (lon, lat):
                raise OperationalInputError("SERVICEABILITY_CONTEXT_CHANGED")
            live = replace(
                problem,
                vehicles=tuple(
                    replace(
                        v,
                        available_at=max(
                            v.available_at,
                            math.ceil((current_time - problem.epoch).total_seconds()),
                        ),
                    )
                    for v in problem.vehicles
                ),
            )
            validate_proposal(live, selected)
            locked.version += 1
            await session.flush([locked])
            from geoalchemy2.elements import WKTElement

            reservation = CapacityReservation(
                reservation_id=command.client_command_id,
                pickup_location=WKTElement(f"POINT({lon} {lat})", srid=4326),
                region_id=region.region_id,
                region_version=locked.version,
                customer_id=customer_id,
                context_id=command.context_id,
                slot_start=command.slot_start,
                slot_end=command.slot_end,
                mass_g=mass,
                volume_ml=volume,
                pickup_seconds=pickup_seconds,
                discharge_seconds=discharge_seconds,
                receiving_point_id=region.default_warehouse_id,
                discharge_deadline=config.horizon_end,
                expires_at=expires_at,
                fingerprint=fingerprint,
                created_at=now,
                certificate={
                    "region_version": locked.version,
                    "proposal": PROPOSAL_CODEC.dump_python(selected, mode="json"),
                    "incremental": incremental,
                    "safety_percent": safety_percent,
                },
            )
            session.add(reservation)
            await wake(session, "CAPACITY_RESERVED", now)
        await session.commit()
    return command.client_command_id


async def bind_reservation(
    session: AsyncSession, request: CollectionRequest, reservation_id: UUID | None
) -> None:
    if reservation_id is None:
        raise OperationalInputError("CAPACITY_RESERVATION_REQUIRED")
    row = await session.scalar(
        select(CapacityReservation)
        .where(CapacityReservation.reservation_id == reservation_id)
        .with_for_update()
    )
    if (
        row is None
        or row.customer_id != request.customer_id
        or row.context_id != request.serviceability_context_id
        or row.slot_start != request.slot_start
        or row.slot_end != request.slot_end
    ):
        raise OperationalInputError("CAPACITY_RESERVATION_MISMATCH")
    if row.request_id is not None and row.request_id != request.request_id:
        raise OperationalInputError("CAPACITY_RESERVATION_ALREADY_USED")
    if row.expires_at <= utc_now() or row.expires_at < request.payment_expires_at:
        raise OperationalInputError("CAPACITY_RESERVATION_EXPIRED")
    from geoalchemy2 import Geometry
    from sqlalchemy import cast, func

    same_location = await session.scalar(
        select(
            func.ST_Equals(
                cast(CapacityReservation.pickup_location, Geometry),
                cast(CollectionRequest.pickup_location, Geometry),
            )
        ).where(
            CapacityReservation.reservation_id == reservation_id,
            CollectionRequest.request_id == request.request_id,
        )
    )
    if not same_location:
        raise OperationalInputError("CAPACITY_LOCATION_MISMATCH")
    from tirodhan.modules.collection_requests.models import CollectionRequestItem

    known_weight = await session.scalar(
        select(func.coalesce(func.sum(CollectionRequestItem.declared_weight_grams), 0)).where(
            CollectionRequestItem.request_id == request.request_id
        )
    )
    if known_weight is not None and known_weight > row.mass_g:
        raise OperationalInputError("CAPACITY_BELOW_DECLARED_WEIGHT")
    row.request_id = request.request_id
    session.add(
        RoutingManifest(
            request_id=request.request_id,
            mass_g=row.mass_g,
            volume_ml=row.volume_ml,
            pickup_seconds=row.pickup_seconds,
            discharge_seconds=row.discharge_seconds,
            receiving_point_id=row.receiving_point_id,
            discharge_deadline=row.discharge_deadline,
            recorded_by=request.customer_id,
        )
    )
    await wake(session, "BOOKING_BOUND", utc_now())
