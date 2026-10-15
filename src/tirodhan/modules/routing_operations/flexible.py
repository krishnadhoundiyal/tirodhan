"""Supplementary offers confer no assignment until accepted and independently activated."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tirodhan.modules.dispatch.models import RiderAvailability
from tirodhan.modules.reliability.primitives import append_outbox_event

from .models import OperatingShift, OperatingVehicle
from .orchestration_models import (
    CapacityAdmissionRequest,
    FlexibleCommitment,
    FlexibleRouteOffer,
    RegionVehicle,
)


async def offer_supplementary(
    session: AsyncSession, request: dict[str, Any], now: datetime, planning_lead_seconds: int
) -> bool:
    region = request.get("region_id")
    if region is None:
        return False
    # The response allowance is operational. It never modifies the customer's window.
    expiry = request["slot_end"] - timedelta(seconds=planning_lead_seconds)
    if expiry <= now:
        return False
    if await session.get(FlexibleCommitment, request["request_id"]) is not None:
        return True
    candidates = (
        await session.execute(
            select(OperatingShift, OperatingVehicle)
            .join(OperatingVehicle)
            .join(RegionVehicle)
            .join(RiderAvailability, RiderAvailability.rider_id == OperatingShift.rider_id)
            .where(
                RegionVehicle.region_id == region,
                OperatingShift.resource_kind == "FLEXIBLE",
                OperatingShift.closed_at.is_(None),
                OperatingShift.starts_at <= now,
                OperatingShift.ends_at >= request["slot_start"],
                OperatingVehicle.blocked_reason.is_(None),
                RiderAvailability.availability_intent == "AVAILABLE",
                OperatingVehicle.mass_capacity_g >= request["mass_g"],
                OperatingVehicle.volume_capacity_ml >= request["volume_ml"],
            )
            .order_by(OperatingShift.shift_id)
            .limit(32)
        )
    ).all()
    offered = False
    for shift, vehicle in candidates:
        from .physical import cargo_load

        load = await cargo_load(session, vehicle.vehicle_id)
        if (
            load[0] + request["mass_g"] > vehicle.mass_capacity_g
            or load[1] + request["volume_ml"] > vehicle.volume_capacity_ml
        ):
            continue
        existing = await session.scalar(
            select(FlexibleRouteOffer)
            .where(
                FlexibleRouteOffer.request_id == request["request_id"],
                FlexibleRouteOffer.shift_id == shift.shift_id,
                FlexibleRouteOffer.expires_at > now,
            )
            .limit(1)
        )
        if existing is None:
            row = FlexibleRouteOffer(
                region_id=region,
                shift_id=shift.shift_id,
                request_id=request["request_id"],
                expires_at=expiry,
                created_at=now,
            )
            session.add(row)
            await session.flush()
            await append_outbox_event(
                session,
                event_key=f"flexible-route-offer:{row.offer_id}",
                aggregate_type="FlexibleRouteOffer",
                aggregate_id=row.offer_id,
                event_type="FlexibleRouteOfferAvailable",
                payload={"offer_id": str(row.offer_id)},
            )
        offered = True
    return offered


async def offer_admission(
    session: AsyncSession, admission: CapacityAdmissionRequest, now: datetime
) -> bool:
    from tirodhan.modules.serviceability.models import ServiceabilityContext

    from .orchestration_models import AdmissionFlexibleOffer, OperatingRegion, RegionCell

    context = await session.get(ServiceabilityContext, admission.context_id)
    mapping = await session.get(RegionCell, context.cell_id) if context else None
    if mapping is None:
        return False
    region = await session.get(OperatingRegion, mapping.region_id)
    assert region is not None
    lead = region.policy["fleet"].get("planning_lead_seconds", 0)
    expiry = min(admission.expires_at, admission.slot_end - timedelta(seconds=lead))
    if expiry <= now:
        return False
    safety = region.policy["safety_percent"]
    candidates = list(
        await session.scalars(
            select(OperatingShift)
            .join(OperatingVehicle)
            .join(RegionVehicle)
            .join(RiderAvailability, RiderAvailability.rider_id == OperatingShift.rider_id)
            .where(
                RegionVehicle.region_id == region.region_id,
                OperatingShift.resource_kind == "FLEXIBLE",
                OperatingShift.closed_at.is_(None),
                OperatingShift.starts_at <= admission.slot_end,
                OperatingShift.ends_at > admission.slot_start,
                OperatingVehicle.blocked_reason.is_(None),
                RiderAvailability.availability_intent == "AVAILABLE",
                OperatingVehicle.mass_capacity_g >= (admission.mass_g * safety + 99) // 100,
                OperatingVehicle.volume_capacity_ml >= (admission.volume_ml * safety + 99) // 100,
            )
            .order_by(OperatingShift.shift_id)
            .limit(32)
        )
    )
    for shift in candidates:
        existing = await session.scalar(
            select(AdmissionFlexibleOffer).where(
                AdmissionFlexibleOffer.admission_id == admission.admission_id,
                AdmissionFlexibleOffer.shift_id == shift.shift_id,
            )
        )
        if existing is None:
            session.add(
                AdmissionFlexibleOffer(
                    admission_id=admission.admission_id, shift_id=shift.shift_id, expires_at=expiry
                )
            )
    return bool(candidates)
