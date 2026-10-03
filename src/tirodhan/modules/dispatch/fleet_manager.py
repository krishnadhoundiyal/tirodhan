from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.dispatch.models import (
    Fleet,
    FleetMembership,
    FleetServiceCell,
    RiderProfile,
    RiderServiceCell,
)
from tirodhan.modules.identity.authorization import lock_active_user_role
from tirodhan.modules.identity.service import ROLE_RIDER


class FleetManagerError(RuntimeError):
    pass


async def create_fleet(session: AsyncSession, name: str) -> Fleet:
    fleet = Fleet(fleet_id=new_uuid7(), name=name, status="ACTIVE")
    session.add(fleet)
    await session.flush()
    return fleet


async def set_fleet_status(session: AsyncSession, fleet_id: UUID, status: str) -> Fleet:
    fleet = await session.scalar(select(Fleet).where(Fleet.fleet_id == fleet_id).with_for_update())
    if not fleet:
        raise FleetManagerError("Fleet not found")
    if status not in ("ACTIVE", "INACTIVE"):
        raise FleetManagerError("Invalid status")
    fleet.status = status
    fleet.updated_at = utc_now()
    await session.flush()
    return fleet


async def add_rider_to_fleet(
    session: AsyncSession, fleet_id: UUID, rider_id: UUID
) -> FleetMembership:
    profile = await session.scalar(
        select(RiderProfile).where(RiderProfile.rider_id == rider_id).with_for_update()
    )
    if not profile or profile.status != "ACTIVE":
        raise FleetManagerError("Rider is not active")
    if not await lock_active_user_role(session, user_id=rider_id, role_code=ROLE_RIDER):
        raise FleetManagerError("Rider role is not active")
    fleet = await session.scalar(select(Fleet).where(Fleet.fleet_id == fleet_id))
    if not fleet or fleet.status != "ACTIVE":
        raise FleetManagerError("Fleet is not active")
    existing = await session.scalar(
        select(FleetMembership)
        .where(FleetMembership.rider_id == rider_id, FleetMembership.left_at.is_(None))
        .with_for_update()
    )
    if existing:
        if existing.fleet_id == fleet_id:
            return existing
        existing.left_at = utc_now()
    membership = FleetMembership(
        fleet_membership_id=new_uuid7(), fleet_id=fleet_id, rider_id=rider_id
    )
    session.add(membership)
    await session.flush()
    return membership


async def end_rider_fleet_membership(session: AsyncSession, rider_id: UUID) -> None:
    existing = await session.scalar(
        select(FleetMembership)
        .where(FleetMembership.rider_id == rider_id, FleetMembership.left_at.is_(None))
        .with_for_update()
    )
    if existing:
        existing.left_at = utc_now()
        await session.flush()


async def activate_fleet_cell(
    session: AsyncSession, fleet_id: UUID, cell_id: str
) -> FleetServiceCell:
    existing = await session.scalar(
        select(FleetServiceCell)
        .where(
            FleetServiceCell.fleet_id == fleet_id,
            FleetServiceCell.cell_id == cell_id,
            FleetServiceCell.deactivated_at.is_(None),
        )
        .with_for_update()
    )
    if existing:
        return existing
    cell = FleetServiceCell(fleet_service_cell_id=new_uuid7(), fleet_id=fleet_id, cell_id=cell_id)
    session.add(cell)
    await session.flush()
    return cell


async def deactivate_fleet_cell(session: AsyncSession, fleet_id: UUID, cell_id: str) -> None:
    existing = await session.scalar(
        select(FleetServiceCell)
        .where(
            FleetServiceCell.fleet_id == fleet_id,
            FleetServiceCell.cell_id == cell_id,
            FleetServiceCell.deactivated_at.is_(None),
        )
        .with_for_update()
    )
    if existing:
        existing.deactivated_at = utc_now()
        await session.flush()


async def activate_rider_cell(
    session: AsyncSession, rider_id: UUID, cell_id: str
) -> RiderServiceCell:
    existing = await session.scalar(
        select(RiderServiceCell)
        .where(
            RiderServiceCell.rider_id == rider_id,
            RiderServiceCell.cell_id == cell_id,
            RiderServiceCell.deactivated_at.is_(None),
        )
        .with_for_update()
    )
    if existing:
        return existing
    cell = RiderServiceCell(rider_service_cell_id=new_uuid7(), rider_id=rider_id, cell_id=cell_id)
    session.add(cell)
    await session.flush()
    return cell


async def deactivate_rider_cell(session: AsyncSession, rider_id: UUID, cell_id: str) -> None:
    existing = await session.scalar(
        select(RiderServiceCell)
        .where(
            RiderServiceCell.rider_id == rider_id,
            RiderServiceCell.cell_id == cell_id,
            RiderServiceCell.deactivated_at.is_(None),
        )
        .with_for_update()
    )
    if existing:
        existing.deactivated_at = utc_now()
        await session.flush()
