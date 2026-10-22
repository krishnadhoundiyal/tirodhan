"""Capacity certificates: solve outside locks, atomically reserve under regional CAS."""

from __future__ import annotations

import hashlib
import math
import time
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.collection_requests.scheduling import validate_slot
from tirodhan.modules.planning.routing.adapters import ORTOOLS_VERSION, VROOM_VERSION
from tirodhan.modules.planning.routing.certificates import compose
from tirodhan.modules.planning.routing.contracts import (
    OptimizerEngine,
    ProposedRoute,
    ResourceKind,
    RouteOptimizer,
    RouteProposal,
    RoutingProblem,
    SelectionEngine,
    StopKind,
)
from tirodhan.modules.planning.routing.execution import RoutingAdapterError
from tirodhan.modules.planning.routing.scheduling import InsertionCursor, RouteSchedule
from tirodhan.modules.planning.routing.validation import (
    FeasibilityGate,
    RoutingValidationError,
    validate_proposal,
)
from tirodhan.modules.serviceability.models import ServiceabilityContext

from .admission_store import (
    certificate_document,
    current_proofs,
    load_certificate,
    notice_stamp,
    persist_certificate,
)
from .commands import OperationalInputError
from .economics import make_candidate, measure
from .fleet_priority import propose_prioritized
from .models import RoutingManifest
from .orchestration import wake
from .orchestration_models import CapacityReservation, OperatingRegion, RegionCell
from .rolling import FleetPolicy, incumbent_orders
from .snapshot import (
    MatrixSource,
    OperationalSnapshot,
    build_problem,
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


def insert_obligation(
    problem: RoutingProblem,
    seed: RouteProposal,
    lot_id: UUID,
    *,
    max_evaluations: int,
    max_wall_seconds: float = 10,
    progress: dict[str, Any] | None = None,
) -> RouteProposal | None:
    """Certify one new household, preserving every existing sequence and obligation.

    This is a feasibility certificate, not fleet reoptimization. Every candidate passes
    the full independent validator. Exhaustion returns to native solving; it never
    proves physical infeasibility or establishes operational assignment authority.
    """
    optional = replace(
        problem,
        stops=tuple(replace(s, required=False) if s.lot_id == lot_id else s for s in problem.stops),
    )
    validate_proposal(optional, seed)
    pair = [s for s in problem.stops if s.lot_id == lot_id]
    if len(pair) != 2 or {s.kind for s in pair} != {StopKind.PICKUP, StopKind.DISCHARGE}:
        return None
    pickup = next(s for s in pair if s.kind == StopKind.PICKUP)
    discharge = next(s for s in pair if s.kind == StopKind.DISCHARGE)
    gate = FeasibilityGate(problem)
    orders = {r.vehicle_id: r.stop_ids for r in seed.routes}
    deadline, evaluations = time.monotonic() + max_wall_seconds, 0
    resume_vehicle = progress.get("vehicle") if progress else None
    resuming = resume_vehicle is not None
    for vehicle in sorted(
        problem.vehicles,
        key=lambda v: (
            v.resource_kind != ResourceKind.COMMITTED,
            len(orders.get(v.vehicle_id, ())),
            str(v.vehicle_id),
        ),
    ):
        if vehicle.vehicle_id not in pickup.eligible_vehicle_ids:
            continue
        if resuming and str(vehicle.vehicle_id) != resume_vehicle:
            continue
        resuming = False
        cursor = (
            InsertionCursor(progress["pickup"], progress["discharge"])
            if progress and str(vehicle.vehicle_id) == resume_vehicle
            else None
        )
        old = orders.get(vehicle.vehicle_id, ())
        result = RouteSchedule(problem, vehicle, old, gate.context).insert(
            pickup,
            discharge,
            max_evaluations=max(0, max_evaluations - evaluations),
            deadline=deadline,
            cursor=cursor,
        )
        evaluations += result.evaluations
        if result.order is not None:
            candidate = replace(
                make_candidate(seed, problem, orders | {vehicle.vehicle_id: result.order}),
                engine=SelectionEngine.COMPOSITE,
                engine_version="ADMISSION_INSERTION_V2",
            )
            gate.assess(candidate)
            return candidate
        if not result.exhausted:
            if progress is not None:
                assert result.cursor is not None
                progress.update(
                    vehicle=str(vehicle.vehicle_id),
                    pickup=result.cursor.pickup_gap,
                    discharge=result.cursor.discharge_gap,
                )
                raise RoutingAdapterError("UNKNOWN_SEARCH_QUANTUM")
            return None
    return None


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
    regional_lease_token: UUID | None = None,
    projection_time: datetime | None = None,
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
        if lease_token is not None:
            from .orchestration_models import CapacityAdmissionRequest

            intent = await session.get(CapacityAdmissionRequest, command.client_command_id)
            if intent is not None and intent.region_id != region.region_id:
                raise OperationalInputError("ADMISSION_REGION_CHANGED")
        from .fleet_models import FleetControl

        control = await session.get(FleetControl, f"REGION:{region.region_id}")
        if control is None:
            control = await session.get(FleetControl, "COMMITTED_FLEET")
        configuration = control.configuration.get("fleet", control.configuration) if control else {}
        if not configuration.get("regional", False):
            raise OperationalInputError("REGIONAL_FLEET_POLICY_REQUIRED")
        policy = FleetPolicy.model_validate(configuration)
        safety_percent = region.policy["safety_percent"]
        pickup_seconds = max(command.pickup_seconds, region.policy["minimum_pickup_seconds"])
        discharge_seconds = max(
            command.discharge_seconds, region.policy["minimum_discharge_seconds"]
        )
        version = region.version
    # Future bookings use declared future shifts, never a requirement for a current rider.
    epoch = max(
        (projection_time or now).replace(microsecond=0), command.slot_start - timedelta(hours=12)
    )
    config = policy.snapshot_configuration(
        epoch, region_id=region.region_id, admission_mode=True
    ).model_copy(
        update={
            "region_id": region.region_id,
            "regional": True,
            "admission_mode": True,
            "horizon_end": epoch + timedelta(days=1),
            "pickup_horizon_end": None,
            "as_of": now,
        }
    )
    from .admission_state import read_cache

    cache = read_cache(factory)
    snapshot, source_stamp = await cache.capture(factory, config, version)
    from .regional_planner import regional_ready

    ready, issues = regional_ready(snapshot, config, now)
    quarantined = {row["route_id"] for row in snapshot.sources["routes"]} | {
        row["route_id"] for row in snapshot.sources["orphaned_routes"]
    }
    quarantined -= {row["route_id"] for row in ready.sources["routes"]}
    if any(route not in quarantined for _, route, _ in issues):
        raise OperationalInputError("REGIONAL_INPUTS_REQUIRE_ATTENTION")
    active_profiles = {v["profile_version"] for v in ready.sources["vehicles"]}
    roads = cache.roads(
        {key: source for key, source in providers.items() if key in active_profiles}
    )
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
    async with factory() as session:
        previous = await session.scalar(
            select(CapacityReservation)
            .where(CapacityReservation.region_id == region.region_id)
            .where(CapacityReservation.admission_basis == "EXACT_CERTIFICATE")
            .order_by(CapacityReservation.region_version.desc())
            .limit(1)
        )
        seed = await load_certificate(session, previous) if previous else None
        aliases = (
            await session.execute(
                select(CapacityReservation.reservation_id, CapacityReservation.request_id).where(
                    CapacityReservation.region_id == region.region_id,
                    CapacityReservation.request_id.is_not(None),
                )
            )
        ).all()
    if seed is not None:
        accepted_bookings = {
            r["request_id"] for r in ready.sources["requests"] if not r.get("hold", False)
        }
        stop_aliases = {
            uuid5(NAMESPACE_URL, f"tirodhan:{kind}:{reserved}"): uuid5(
                NAMESPACE_URL, f"tirodhan:{kind}:{booked}"
            )
            for reserved, booked in aliases
            if booked in accepted_bookings
            for kind in ("pickup", "discharge")
        }
        seed = replace(
            seed,
            routes=tuple(
                replace(r, stop_ids=tuple(stop_aliases.get(s, s) for s in r.stop_ids))
                for r in seed.routes
            ),
        )
    orders = incumbent_orders(ready)
    seed_orders = {r.vehicle_id: r.stop_ids for r in seed.routes} if seed else orders
    problem = await build_problem(
        OperationalSnapshot(candidate_sources, b""),
        config,
        new_uuid7(),
        roads,
        now=now,
        route_orders=seed_orders,
        insertion_lot=command.client_command_id,
    )
    if seed is None:
        seed = RouteProposal(
            problem.problem_id,
            problem.snapshot_id,
            problem.contract_version,
            problem.policy_version,
            SelectionEngine.COMPOSITE,
            "ADMISSION_EMPTY_SEED_V2",
            # A cold commercial writer can still have live operational routes.
            # Keep those outside any native repair neighbourhood as well as in
            # its closure; an empty seed would omit their required obligations.
            tuple(ProposedRoute(vehicle, order) for vehicle, order in orders.items() if order),
            (),
        )
    valid_stops = {s.stop_id for s in problem.stops}
    # Removed/cancelled obligations are not carried forward. The new connecting
    # directed arcs were acquired above; this modified seed must pass the gate.
    seed = replace(
        seed,
        problem_id=problem.problem_id,
        snapshot_id=problem.snapshot_id,
        contract_version=problem.contract_version,
        policy_version=problem.policy_version,
        routes=tuple(
            replace(r, stop_ids=tuple(s for s in r.stop_ids if s in valid_stops))
            for r in seed.routes
            if r.vehicle_id in {v.vehicle_id for v in problem.vehicles}
        ),
        unserved_stop_ids=tuple(
            valid_stops - {s for r in seed.routes for s in r.stop_ids if s in valid_stops}
        ),
    )
    candidates: list[RouteProposal] = []
    incremental = False
    progress_key = {
        "intent": str(command.client_command_id),
        "region_version": version,
        "source": source_stamp.hex(),
        "epoch": problem.epoch.isoformat(),
        "graphs": [[m.graph_version, m.profile_version] for m in problem.matrices],
    }
    progress: dict[str, Any] = {}
    if regional_lease_token is not None:
        from .orchestration_models import CapacityAdmissionRequest

        async with factory() as session:
            preparation = await session.scalar(
                select(CapacityAdmissionRequest.preparation_cursor).where(
                    CapacityAdmissionRequest.admission_id == command.client_command_id
                )
            )
            if preparation and preparation.get("key") == progress_key:
                progress = dict(preparation.get("search", {}))
    try:
        improved = insert_obligation(
            problem,
            seed,
            command.client_command_id,
            max_evaluations=policy.economics.max_candidate_evaluations,
            max_wall_seconds=2,
            progress=progress if regional_lease_token else None,
        )
        if improved is not None:
            candidates.append(improved)
            incremental = True
    except RoutingAdapterError as error:
        if error.code == "UNKNOWN_SEARCH_QUANTUM" and regional_lease_token is not None:
            from .admission_leases import progress as save_progress

            assert lease_token is not None
            await save_progress(
                factory,
                region.region_id,
                regional_lease_token,
                command.client_command_id,
                lease_token,
                max(now, utc_now()),
                {"key": progress_key, "search": progress},
            )
        raise
    except RoutingValidationError:
        pass
    if not candidates:
        from .admission_native import native_neighbourhoods

        async for augmented, local, chosen in native_neighbourhoods(
            problem,
            seed_orders,
            command.client_command_id,
            candidate_sources,
            roads,
        ):
            for engine in OptimizerEngine:
                try:
                    result = await propose_prioritized(
                        optimizers[engine],
                        local,
                        search_seconds=policy.search_seconds,
                        economics=policy.economics,
                    )
                    if (
                        result.engine != engine
                        or result.engine_version
                        != {
                            OptimizerEngine.VROOM: VROOM_VERSION,
                            OptimizerEngine.OR_TOOLS: ORTOOLS_VERSION,
                        }[engine]
                    ):
                        raise RoutingValidationError("ENGINE_IDENTITY_MISMATCH")
                    outside = {
                        r.vehicle_id: r.stop_ids for r in seed.routes if r.vehicle_id not in chosen
                    }
                    candidate = make_candidate(
                        result,
                        augmented,
                        outside | {r.vehicle_id: r.stop_ids for r in result.routes},
                    )
                    candidate = replace(
                        candidate,
                        engine=SelectionEngine.COMPOSITE,
                        engine_version=f"ADMISSION_NATIVE_V2:{engine.value}:{result.engine_version}",
                    )
                    validate_proposal(augmented, candidate)
                    candidates.append(candidate)
                except (RoutingAdapterError, RoutingValidationError):
                    continue
            if candidates:
                problem = augmented
                break
    if not candidates:
        raise OperationalInputError("UNKNOWN_SEARCH_EXHAUSTED")
    selected = (
        candidates[0]
        if len(candidates) == 1
        else min(candidates, key=lambda p: measure(problem, p, policy.economics, orders).rank)
    )
    async with factory() as session:
        await session.connection(execution_options={"isolation_level": "SERIALIZABLE"})
        async with session.begin_nested():
            locked = await session.scalar(
                select(OperatingRegion)
                .where(OperatingRegion.region_id == region.region_id)
                .with_for_update()
            )
            assert locked is not None
            # Source writers lock only this separate head, after their domain rows;
            # they never acquire operating-region authority from an invalidation trigger.
            from .admission_models import AdmissionSourceHead

            await session.scalar(
                select(AdmissionSourceHead)
                .where(AdmissionSourceHead.region_id == region.region_id)
                .with_for_update()
            )
            replay = await session.get(CapacityReservation, command.client_command_id)
            if replay is not None:
                if replay.customer_id != customer_id or replay.fingerprint != fingerprint:
                    raise OperationalInputError("RESERVATION_REPLAY_CONFLICT")
                return replay.reservation_id
            if (
                locked.version != version
                or await notice_stamp(session, region.region_id) != source_stamp
            ):
                raise OperationalInputError("CAPACITY_PROJECTION_STALE_RETRY")
            current_time = max(now, utc_now())
            if regional_lease_token is not None:
                from .admission_leases import fence

                await fence(session, region.region_id, regional_lease_token, current_time)
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
            fresh_membership = await session.get(
                RegionCell,
                original.cell_id if original else fresh_context.cell_id,
                populate_existing=True,
            )
            if fresh_membership is None or fresh_membership.region_id != region.region_id:
                raise OperationalInputError("ADMISSION_REGION_CHANGED")
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
            proofs = compose(live, selected, await current_proofs(session, region.region_id))
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
                certificate=certificate_document(
                    selected, locked.version, incremental, safety_percent
                ),
            )
            session.add(reservation)
            await session.flush([reservation])
            await persist_certificate(
                session,
                reservation,
                live,
                selected,
                proofs,
                candidate_sources,
                await notice_stamp(session, region.region_id),
            )
            await wake(session, "CAPACITY_RESERVED", now, region_id=region.region_id)
        await session.commit()
    cached = cache.states.get(region.region_id)
    if cached is not None and cached.version == version:
        sources = dict(cached.sources)
        sources["requests"] = list(cached.sources["requests"]) + [
            r for r in candidate_sources["requests"] if r["request_id"] == command.client_command_id
        ]
        cache.put(
            region.region_id,
            locked.version,
            cached.signature,
            sources,
            min(expires_at, cached.expires_at) if cached.expires_at else expires_at,
        )
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
    from tirodhan.modules.business_admin.models import BookingWeightQuote
    from tirodhan.modules.business_admin.weight_pricing import declaration_fingerprint
    from tirodhan.modules.collection_requests.ports import DeclaredRequestItem

    weight_quote = await session.get(BookingWeightQuote, request.request_id)
    if weight_quote is not None:
        items = list(
            await session.scalars(
                select(CollectionRequestItem)
                .where(CollectionRequestItem.request_id == request.request_id)
                .order_by(CollectionRequestItem.created_at, CollectionRequestItem.request_item_id)
            )
        )
        # Item order in the request is preserved by UUIDv7 allocation, as in customer reads.
        from typing import cast as type_cast

        from tirodhan.modules.business_admin.weight_contracts import WeightBand

        fingerprint = declaration_fingerprint(
            tuple(
                DeclaredRequestItem(
                    i.item_category_code,
                    i.declared_quantity,
                    i.declared_weight_grams,
                    type_cast(WeightBand, i.weight_band),
                    i.weight_kg,
                    i.declared_volume_ml,
                )
                for i in items
            )
        )
        if (
            row.declared_mass_g != weight_quote.total_weight_grams
            or row.declaration_fingerprint != fingerprint
            or row.weight_quote_fingerprint != weight_quote.quote_fingerprint
        ):
            raise OperationalInputError("HOLD_DECLARATION_OR_QUOTE_MISMATCH")
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
    await wake(session, "BOOKING_BOUND", utc_now(), request_id=request.request_id)
