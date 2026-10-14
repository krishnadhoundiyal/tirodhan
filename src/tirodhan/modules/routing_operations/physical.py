"""Shared effective custody and approved amendments; historical facts remain immutable."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tirodhan.modules.handovers.models import HandoverEventItem
from tirodhan.modules.planning.models import PickupExecution

from .models import CargoVehicleObservation, RoutingManifest
from .orchestration_models import (
    AppointmentConsent,
    CargoTransfer,
    DischargeAuthorization,
    PhysicalLoadObservation,
)


async def cargo_load(session: AsyncSession, vehicle_id: UUID) -> tuple[int, int]:
    latest = select(CargoTransfer.to_vehicle_id).where(
        CargoTransfer.request_id == RoutingManifest.request_id
    )
    current_vehicle = (
        latest.order_by(CargoTransfer.sequence.desc())
        .limit(1)
        .correlate(RoutingManifest)
        .scalar_subquery()
    )
    handed = exists().where(
        HandoverEventItem.pickup_execution_id == PickupExecution.pickup_execution_id,
        HandoverEventItem.status == "VALIDATED",
    )
    mass_observed = (
        select(PhysicalLoadObservation.mass_g)
        .where(PhysicalLoadObservation.request_id == RoutingManifest.request_id)
        .order_by(
            PhysicalLoadObservation.observed_at.desc(),
            PhysicalLoadObservation.observation_id.desc(),
        )
        .limit(1)
        .correlate(RoutingManifest)
        .scalar_subquery()
    )
    volume_observed = (
        select(PhysicalLoadObservation.volume_ml)
        .where(PhysicalLoadObservation.request_id == RoutingManifest.request_id)
        .order_by(
            PhysicalLoadObservation.observed_at.desc(),
            PhysicalLoadObservation.observation_id.desc(),
        )
        .limit(1)
        .correlate(RoutingManifest)
        .scalar_subquery()
    )
    mass, volume = (
        await session.execute(
            select(
                func.coalesce(func.sum(func.coalesce(mass_observed, RoutingManifest.mass_g)), 0),
                func.coalesce(
                    func.sum(func.coalesce(volume_observed, RoutingManifest.volume_ml)), 0
                ),
            )
            .join(
                CargoVehicleObservation,
                CargoVehicleObservation.request_id == RoutingManifest.request_id,
            )
            .join(PickupExecution, PickupExecution.request_id == RoutingManifest.request_id)
            .where(
                func.coalesce(current_vehicle, CargoVehicleObservation.vehicle_id) == vehicle_id,
                ~handed,
            )
        )
    ).one()
    return int(mass), int(volume)


async def effective_manifest(session: AsyncSession, original: RoutingManifest) -> RoutingManifest:
    authorization = await session.scalar(
        select(DischargeAuthorization)
        .where(DischargeAuthorization.request_id == original.request_id)
        .order_by(
            DischargeAuthorization.authorized_at.desc(),
            DischargeAuthorization.authorization_id.desc(),
        )
        .limit(1)
    )
    observation = await session.scalar(
        select(PhysicalLoadObservation)
        .where(PhysicalLoadObservation.request_id == original.request_id)
        .order_by(
            PhysicalLoadObservation.observed_at.desc(),
            PhysicalLoadObservation.observation_id.desc(),
        )
        .limit(1)
    )
    if authorization is None and observation is None:
        return original
    values = {c.name: getattr(original, c.name) for c in RoutingManifest.__table__.columns}
    if authorization is not None:
        values.update(
            receiving_point_id=authorization.receiving_point_id,
            discharge_deadline=authorization.discharge_deadline,
        )
    if observation is not None:
        values.update(mass_g=observation.mass_g, volume_ml=observation.volume_ml)
    return RoutingManifest(**values)  # Detached view, never attached or persisted.


async def appointment_window(
    session: AsyncSession, request_id: UUID, start: datetime, end: datetime
) -> tuple[datetime, datetime]:
    consent = await session.scalar(
        select(AppointmentConsent)
        .where(AppointmentConsent.request_id == request_id)
        .order_by(AppointmentConsent.consented_at.desc(), AppointmentConsent.consent_id.desc())
        .limit(1)
    )
    return (consent.slot_start, consent.slot_end) if consent else (start, end)


async def unresolved_load(session: AsyncSession, request_id: UUID) -> bool:
    from .fleet_models import FleetException

    issue_at = await session.scalar(
        select(func.max(FleetException.created_at)).where(
            FleetException.request_id == request_id, FleetException.reason == "LOAD_UNCERTAIN"
        )
    )
    if issue_at is None:
        return False
    observation_at = await session.scalar(
        select(func.max(PhysicalLoadObservation.observed_at)).where(
            PhysicalLoadObservation.request_id == request_id
        )
    )
    return observation_at is None or observation_at < issue_at
