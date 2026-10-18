"""Single-owner capacity movement. Domain rows precede regional/source publication fences."""

from collections.abc import Callable
from datetime import datetime
from typing import Literal
from uuid import UUID

import h3  # type: ignore[import-untyped]
from pydantic import Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from .authority import RouteAuthorityError, lock_resources
from .capacity_models import RegionCapacityTransfer
from .commands import Command
from .models import OperatingShift, OperatingVehicle
from .orchestration import wake
from .orchestration_models import OperatingRegion, RegionCell, RegionVehicle
from .physical import cargo_load


class TransferRegionalCapacity(Command):
    operation: Literal["CAPACITY_REGION_TRANSFER"] = "CAPACITY_REGION_TRANSFER"
    shift_id: UUID
    from_region_id: UUID
    to_region_id: UUID
    expected_membership_version: int = Field(strict=True, ge=1)
    expected_from_version: int = Field(strict=True, ge=1)
    expected_to_version: int = Field(strict=True, ge=1)


async def transfer_capacity(
    session: AsyncSession,
    actor: UUID,
    command: TransferRegionalCapacity,
    now: datetime,
    clock: Callable[[], datetime] | None = None,
) -> UUID:
    from tirodhan.modules.dispatch.models import RiderAvailability

    from .admission_models import AdmissionSourceHead
    from .regional_projection import effective_shift_end

    shift = await session.get(OperatingShift, command.shift_id)
    if shift is None:
        raise RouteAuthorityError("SHIFT_UNAVAILABLE")
    await lock_resources(session, [shift.rider_id], [shift.vehicle_id])
    await session.refresh(shift)
    availability = await session.get(RiderAvailability, shift.rider_id)
    if (
        shift.closed_at is not None
        or not shift.starts_at
        <= now
        < await effective_shift_end(session, shift.shift_id, shift.ends_at)
        or availability is None
        or availability.availability_intent != "AVAILABLE"
        or availability.work_state != "IDLE"
    ):
        raise RouteAuthorityError("AVAILABLE_ACTIVE_RIDER_REQUIRED")
    regions = list(
        await session.scalars(
            select(OperatingRegion)
            .where(OperatingRegion.region_id.in_((command.from_region_id, command.to_region_id)))
            .order_by(OperatingRegion.region_id)
            .with_for_update()
        )
    )
    expected = {
        command.from_region_id: command.expected_from_version,
        command.to_region_id: command.expected_to_version,
    }
    if len(regions) != 2 or any(r.version != expected[r.region_id] for r in regions):
        raise RouteAuthorityError("REGION_VERSION_CHANGED")
    # Publication and transfer share these heads; no certificate can pass between the
    # commitment check and membership change. SERIALIZABLE covers new predicate matches.
    await session.execute(
        select(AdmissionSourceHead)
        .where(AdmissionSourceHead.region_id.in_(expected))
        .order_by(AdmissionSourceHead.region_id)
        .with_for_update()
    )
    now = max(now, clock()) if clock else now
    if (
        not shift.starts_at
        <= now
        < await effective_shift_end(session, shift.shift_id, shift.ends_at)
    ):
        raise RouteAuthorityError("AVAILABLE_ACTIVE_RIDER_REQUIRED")
    membership = await session.get(RegionVehicle, shift.vehicle_id, populate_existing=True)
    if (
        membership is None
        or membership.region_id != command.from_region_id
        or membership.version != command.expected_membership_version
    ):
        raise RouteAuthorityError("CAPACITY_OWNER_CHANGED")
    cells = list(
        await session.scalars(
            select(RegionCell.cell_id).where(RegionCell.region_id == command.from_region_id)
        )
    )
    adjacent = {n for c in cells for n in h3.grid_disk(c, 1)} - set(cells)
    if not adjacent or not await session.scalar(
        select(RegionCell.cell_id)
        .where(RegionCell.cell_id.in_(adjacent), RegionCell.region_id == command.to_region_id)
        .limit(1)
    ):
        raise RouteAuthorityError("ADJACENT_REGION_REQUIRED")
    if await cargo_load(session, shift.vehicle_id) != (0, 0):
        raise RouteAuthorityError("CUSTODY_MUST_DRAIN_BEFORE_REGION_TRANSFER")
    blocked = await session.scalar(
        text("""SELECT
      EXISTS(SELECT 1 FROM rider_assignment WHERE rider_id=:rider AND status='ACTIVE') OR
      EXISTS(SELECT 1 FROM vehicle_route WHERE vehicle_id=:vehicle AND status<>'CLOSED') OR
      EXISTS(SELECT 1 FROM cargo_transfer_intent i JOIN operating_shift s
        ON s.shift_id=i.to_shift_id WHERE i.resolved_at IS NULL
        AND (i.from_vehicle_id=:vehicle OR s.vehicle_id=:vehicle)) OR
      routing_capacity_committed(:vehicle,:now)
      """),
        {"rider": shift.rider_id, "vehicle": shift.vehicle_id, "now": now},
    )
    if blocked:
        raise RouteAuthorityError("CAPACITY_HAS_COMMITMENTS")
    membership.version += 1
    transfer = RegionCapacityTransfer(
        transfer_id=command.client_command_id,
        vehicle_id=shift.vehicle_id,
        rider_id=shift.rider_id,
        from_region_id=command.from_region_id,
        to_region_id=command.to_region_id,
        membership_version=membership.version,
        authorized_by=actor,
        transferred_at=now,
    )
    session.add(transfer)
    await session.flush([transfer])
    membership.region_id = command.to_region_id
    vehicle = await session.get(OperatingVehicle, shift.vehicle_id)
    assert vehicle is not None
    vehicle.version += 1
    for region in regions:
        region.version += 1
    await session.flush()
    for region in regions:
        await wake(session, command.operation, now, urgent=True, region_id=region.region_id)
    return command.client_command_id
