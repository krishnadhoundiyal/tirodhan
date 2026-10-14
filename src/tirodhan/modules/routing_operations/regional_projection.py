"""Project reservations and explicit operational amendments without rewriting history."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, true
from sqlalchemy.ext.asyncio import AsyncSession

from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.planning.models import PickupExecution
from tirodhan.modules.serviceability.models import ServiceabilityContext

from .orchestration_models import (
    AppointmentConsent,
    CapacityReservation,
    CargoTransfer,
    DischargeAuthorization,
    OperatingBreak,
    RegionCell,
    RegionVehicle,
    ShiftExtension,
)


async def project(
    session: AsyncSession,
    config: Any,
    vehicles: list[dict[str, Any]],
    requests: list[dict[str, Any]],
) -> dict[str, Any]:
    """All reads belong to the caller's MVCC snapshot / SERIALIZABLE activation."""
    inputs: dict[str, Any] = {}
    if config.regional:
        from .fleet_models import FleetControl

        control = await session.get(FleetControl, "COMMITTED_FLEET")
        inputs["fleet_policy"] = (control.version, control.configuration) if control else None
        cells: dict[str, UUID] = dict(
            (
                await session.execute(
                    select(RegionCell.cell_id, RegionCell.region_id).where(
                        RegionCell.cell_id.in_(
                            select(CollectionRequest.cell_id).where(
                                CollectionRequest.request_id.in_(
                                    [r["request_id"] for r in requests]
                                )
                            )
                        )
                    )
                )
            )
            .tuples()
            .all()
        )
        memberships: dict[UUID, UUID] = dict(
            (
                await session.execute(
                    select(RegionVehicle.vehicle_id, RegionVehicle.region_id).where(
                        RegionVehicle.vehicle_id.in_([v["vehicle_id"] for v in vehicles])
                    )
                )
            )
            .tuples()
            .all()
        )
        for v in vehicles:
            v["region_id"] = memberships.get(v["vehicle_id"])
        request_cells: dict[UUID, str] = dict(
            (
                await session.execute(
                    select(CollectionRequest.request_id, CollectionRequest.cell_id).where(
                        CollectionRequest.request_id.in_([r["request_id"] for r in requests])
                    )
                )
            )
            .tuples()
            .all()
        )
        for r in requests:
            r["region_id"] = cells.get(request_cells[r["request_id"]])
        if config.region_id:
            vehicles[:] = [v for v in vehicles if v["region_id"] == config.region_id]
        else:
            vehicles[:] = [v for v in vehicles if v["region_id"] is not None]
        inputs["region_cells"] = sorted((str(k), str(v)) for k, v in cells.items())
        inputs["region_vehicles"] = sorted((str(k), str(v)) for k, v in memberships.items())
        from .snapshot import _coordinates

        reservations = (
            await session.execute(
                select(
                    CapacityReservation,
                    *_coordinates(CapacityReservation.pickup_location),
                    CollectionRequest.status,
                    CollectionRequest.payment_expires_at,
                )
                .join(
                    ServiceabilityContext,
                    ServiceabilityContext.serviceability_context_id
                    == CapacityReservation.context_id,
                )
                .outerjoin(
                    CollectionRequest,
                    CollectionRequest.request_id == CapacityReservation.request_id,
                )
                .where(
                    CapacityReservation.region_id == config.region_id
                    if config.region_id
                    else true(),
                    CapacityReservation.expires_at > (config.as_of or config.epoch),
                    CapacityReservation.slot_start
                    < (config.pickup_horizon_end or config.horizon_end),
                    CapacityReservation.slot_end >= config.epoch,
                )
                .order_by(CapacityReservation.reservation_id)
                .limit(config.max_requests + 1)
            )
        ).all()
        for reservation, lon, lat, booked_status, payment_expires_at in reservations:
            if config.region_id and reservation.region_id != config.region_id:
                continue
            if reservation.request_id is not None and (
                booked_status != "PENDING_PAYMENT"
                or payment_expires_at <= (config.as_of or config.epoch)
            ):
                continue
            requests.append(
                {
                    "request_id": reservation.reservation_id,
                    "status": "ACCEPTED",
                    "hold": True,
                    "region_id": reservation.region_id,
                    "slot_start": reservation.slot_start,
                    "slot_end": reservation.slot_end,
                    "longitude": lon,
                    "latitude": lat,
                    "load_uncertain": False,
                    "pickup_execution_id": None,
                    "pickup_status": None,
                    "owner_id": None,
                    "assignment_id": None,
                    "collector_id": None,
                    "pickup_attempt_id": None,
                    "mass_g": reservation.mass_g,
                    "volume_ml": reservation.volume_ml,
                    "pickup_seconds": reservation.pickup_seconds,
                    "discharge_seconds": reservation.discharge_seconds,
                    "discharge_deadline": reservation.discharge_deadline,
                    "receiving_point_id": reservation.receiving_point_id,
                    "cargo_vehicle_id": None,
                    "observed_attempt_id": None,
                }
            )
    ids = [r["request_id"] for r in requests]
    amendments: tuple[tuple[Any, str], ...] = (
        (AppointmentConsent, "consented_at"),
        (CargoTransfer, "sequence"),
        (DischargeAuthorization, "authorized_at"),
    )
    from .orchestration_models import PhysicalLoadObservation

    amendments += ((PhysicalLoadObservation, "observed_at"),)
    for model, timestamp in amendments:
        rows = list(
            await session.scalars(
                select(model)
                .where(model.request_id.in_(ids))
                .distinct(model.request_id)
                .order_by(
                    model.request_id,
                    getattr(model, timestamp).desc(),
                    list(model.__table__.primary_key)[0].desc(),
                )
            )
        )
        inputs[model.__tablename__] = [
            {c.name: getattr(row, c.name) for c in model.__table__.columns} for row in rows
        ]
        latest = {row.request_id: row for row in rows}
        for r in requests:
            amendment = latest.get(r["request_id"])
            if amendment is None:
                continue
            if isinstance(amendment, AppointmentConsent):
                r["slot_start"], r["slot_end"] = amendment.slot_start, amendment.slot_end
            elif isinstance(amendment, CargoTransfer):
                r["collector_id"] = amendment.to_rider_id
                r["cargo_vehicle_id"] = amendment.to_vehicle_id
            elif isinstance(amendment, PhysicalLoadObservation):
                r["mass_g"], r["volume_ml"] = amendment.mass_g, amendment.volume_ml
                if r["load_uncertain"]:
                    from .physical import unresolved_load

                    r["load_uncertain"] = await unresolved_load(session, r["request_id"])
            else:
                r["receiving_point_id"] = amendment.receiving_point_id
                r["discharge_deadline"] = amendment.discharge_deadline
    from tirodhan.modules.pickups.models import PickupAttempt

    from .orchestration_models import PickupReattempt, ReattemptAuthorization

    reattempts = (
        await session.execute(
            select(PickupReattempt, PickupAttempt, PickupExecution.request_id)
            .join(PickupAttempt, PickupAttempt.pickup_attempt_id == PickupReattempt.attempt_id)
            .join(
                PickupExecution,
                PickupExecution.pickup_execution_id == PickupAttempt.pickup_execution_id,
            )
            .where(PickupExecution.request_id.in_(ids))
            .order_by(PickupAttempt.attempt_number)
        )
    ).all()
    authorizations = list(
        await session.scalars(
            select(ReattemptAuthorization).where(ReattemptAuthorization.request_id.in_(ids))
        )
    )
    inputs["reattempts"] = [
        {
            "attempt_id": row.attempt_id,
            "eligible_at": row.eligible_at,
            "attempt_number": attempt.attempt_number,
            "request_id": request_id,
        }
        for row, attempt, request_id in reattempts
    ]
    inputs["reattempt_authorizations"] = [
        {c.name: getattr(a, c.name) for c in ReattemptAuthorization.__table__.columns}
        for a in authorizations
    ]
    for request in requests:
        failed = [
            (row, attempt)
            for row, attempt, request_id in reattempts
            if request_id == request["request_id"]
        ]
        if failed:
            request["retry_eligible_at"] = max(row.eligible_at for row, _ in failed)
            anchors = {
                a.after_attempt_id for a in authorizations if a.request_id == request["request_id"]
            }
            after = max(
                [0]
                + [
                    attempt.attempt_number
                    for _, attempt in failed
                    if attempt.pickup_attempt_id in anchors
                ]
            )
            # Two automatic reattempts, then operational review; no cancellation or new charge.
            request["retry_review_required"] = (
                sum(attempt.attempt_number > after for _, attempt in failed) >= 3
            )
    from .dispatch_models import RouteRevision, VehicleRoute
    from .orchestration_models import FlexibleCommitment, RouteResumeAuthorization

    resume_authorizations = list(
        await session.scalars(
            select(RouteResumeAuthorization)
            .join(RouteRevision, RouteRevision.revision_id == RouteResumeAuthorization.revision_id)
            .join(VehicleRoute, VehicleRoute.route_id == RouteRevision.route_id)
            .where(
                RouteRevision.version == VehicleRoute.version,
                VehicleRoute.vehicle_id.in_([v["vehicle_id"] for v in vehicles]),
            )
        )
    )
    inputs["resume_authorizations"] = [
        {c.name: getattr(r, c.name) for c in RouteResumeAuthorization.__table__.columns}
        for r in resume_authorizations
    ]
    for v in vehicles:
        v["resume_authorized"] = False
        for authorization in resume_authorizations:
            revision = await session.get(RouteRevision, authorization.revision_id)
            root = await session.get(VehicleRoute, revision.route_id) if revision else None
            if (
                root
                and root.vehicle_id == v["vehicle_id"]
                and authorization.vehicle_safety_version == v["safety_version"]
            ):
                v["resume_authorized"] = True
    from .dispatch_models import RouteRevision, VehicleRoute

    commitments = list(
        await session.scalars(
            select(FlexibleCommitment).where(
                FlexibleCommitment.shift_id.in_([v["shift_id"] for v in vehicles])
            )
        )
    )
    inputs["flexible_commitments"] = [
        {c.name: getattr(e, c.name) for c in FlexibleCommitment.__table__.columns}
        for e in commitments
    ]
    for v in vehicles:
        v["accepted_requests"] = [c.request_id for c in commitments if c.shift_id == v["shift_id"]]
    from .orchestration_models import AdmissionFlexibleCommitment

    flexible_admissions = list(
        await session.scalars(
            select(
                AdmissionFlexibleCommitment,
            ).where(AdmissionFlexibleCommitment.shift_id.in_([v["shift_id"] for v in vehicles]))
        )
    )
    inputs["flexible_admissions"] = [
        {c.name: getattr(e, c.name) for c in AdmissionFlexibleCommitment.__table__.columns}
        for e in flexible_admissions
    ]
    for v in vehicles:
        for commitment in flexible_admissions:
            if commitment.shift_id == v["shift_id"]:
                reservation = await session.get(CapacityReservation, commitment.admission_id)
                v["accepted_requests"].append(
                    reservation.request_id
                    if reservation and reservation.request_id
                    else commitment.admission_id
                )
    if config.region_id:
        riders = {v["rider_id"] for v in vehicles}
        requests[:] = [
            r
            for r in requests
            if (
                r["collector_id"] in riders
                if r["pickup_status"] == "COLLECTED"
                else r.get("region_id") == config.region_id or r["owner_id"] in riders
            )
        ]
    from .models import OperatingShift
    from .orchestration_models import CargoTransferIntent

    pending = list(
        (
            await session.execute(
                select(CargoTransferIntent, OperatingShift.vehicle_id)
                .join(OperatingShift, OperatingShift.shift_id == CargoTransferIntent.to_shift_id)
                .where(
                    CargoTransferIntent.resolved_at.is_(None),
                    (CargoTransferIntent.from_vehicle_id.in_([v["vehicle_id"] for v in vehicles]))
                    | (OperatingShift.vehicle_id.in_([v["vehicle_id"] for v in vehicles])),
                )
            )
        ).all()
    )
    inputs["pending_transfers"] = [
        {c.name: getattr(intent, c.name) for c in CargoTransferIntent.__table__.columns}
        for intent, _ in pending
    ]
    for vehicle in vehicles:
        vehicle["transfer_in_progress"] = any(
            vehicle["vehicle_id"] in (intent.from_vehicle_id, target) for intent, target in pending
        )
    recovered_cargo = list(
        await session.scalars(
            select(CargoTransfer)
            .where(CargoTransfer.to_vehicle_id.in_([v["vehicle_id"] for v in vehicles]))
            .distinct(CargoTransfer.request_id)
            .order_by(CargoTransfer.request_id, CargoTransfer.sequence.desc())
        )
    )
    for vehicle in vehicles:
        vehicle["accepted_requests"].extend(
            t.request_id
            for t in recovered_cargo
            if t.to_vehicle_id == vehicle["vehicle_id"] and t.to_rider_id == vehicle["rider_id"]
        )
    shifts = [v["shift_id"] for v in vehicles]
    breaks = list(
        await session.scalars(
            select(OperatingBreak)
            .where(OperatingBreak.shift_id.in_(shifts))
            .order_by(OperatingBreak.starts_at, OperatingBreak.break_id)
        )
    )
    extensions = list(
        await session.scalars(
            select(ShiftExtension)
            .where(ShiftExtension.shift_id.in_(shifts))
            .order_by(ShiftExtension.ends_at, ShiftExtension.extension_id)
        )
    )
    inputs["breaks"] = [
        {c.name: getattr(b, c.name) for c in OperatingBreak.__table__.columns} for b in breaks
    ]
    inputs["extensions"] = [
        {c.name: getattr(e, c.name) for c in ShiftExtension.__table__.columns} for e in extensions
    ]
    for v in vehicles:
        v["breaks"] = [(b.starts_at, b.ends_at) for b in breaks if b.shift_id == v["shift_id"]]
        v["ends_at"] = max(
            [v["ends_at"]] + [e.ends_at for e in extensions if e.shift_id == v["shift_id"]]
        )
    if len(requests) > config.max_requests:
        from .commands import OperationalInputError

        raise OperationalInputError("REQUEST_SCOPE_LIMIT")
    return inputs


async def effective_shift_end(
    session: AsyncSession, shift_id: UUID, original: datetime
) -> datetime:
    ends = list(
        await session.scalars(
            select(ShiftExtension.ends_at).where(ShiftExtension.shift_id == shift_id)
        )
    )
    return max([original] + ends)
