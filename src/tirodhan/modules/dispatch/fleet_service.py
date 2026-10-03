"""Small manager commands; history changes use scoped command idempotency."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast
from uuid import UUID

import h3  # type: ignore[import-untyped]
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.db.values import utc_now
from tirodhan.modules.dispatch.models import (
    Fleet,
    FleetMembership,
    FleetServiceCell,
    RiderServiceCell,
)
from tirodhan.modules.dispatch.service import _lock_rider, _require_fresh_rider_authorization
from tirodhan.modules.reliability.primitives import (
    claim_idempotency_record,
    complete_idempotency_record,
)


class FleetCommandError(Exception):
    pass


@dataclass(frozen=True)
class FleetCommand:
    operation: Literal["CREATE", "STATUS", "JOIN", "LEAVE", "FLEET_CELL", "RIDER_CELL"]
    client_command_id: UUID
    fleet_id: UUID | None = None
    rider_id: UUID | None = None
    membership_id: UUID | None = None
    name: str | None = None
    status: str | None = None
    cell_id: str | None = None
    active: bool | None = None


async def execute_fleet_command(
    factory: async_sessionmaker[AsyncSession],
    *,
    manager_id: UUID,
    command: FleetCommand,
    idempotency_expires_at: datetime,
) -> UUID:
    fingerprint = hashlib.sha256(
        json.dumps(
            {key: str(value) for key, value in vars(command).items() if key != "client_command_id"},
            sort_keys=True,
        ).encode()
    ).digest()
    async with factory() as session, session.begin():
        claim = await claim_idempotency_record(
            session,
            scope=f"fleet.command:{manager_id}",
            idempotency_key=str(command.client_command_id),
            request_fingerprint=fingerprint,
            expires_at=idempotency_expires_at,
        )
        if claim.record.status == "COMPLETED":
            if claim.record.result_resource_id is None:
                raise FleetCommandError("completed fleet command has no result")
            return claim.record.result_resource_id
        if not claim.created:
            raise FleetCommandError("fleet command is in progress")
        resource_id = await _apply_command(session, command)
        await complete_idempotency_record(
            session, claim.record, result_resource_id=resource_id, result_status_code=200
        )
        return resource_id


async def _apply_command(session: AsyncSession, command: FleetCommand) -> UUID:
    now = utc_now()
    fleet: Fleet | None
    if command.operation == "CREATE":
        if command.name is None or not command.name.strip() or len(command.name) > 200:
            raise FleetCommandError("fleet name is required")
        fleet = Fleet(name=command.name, status="ACTIVE", created_at=now, updated_at=now)
        session.add(fleet)
        await session.flush()
        return fleet.fleet_id
    fleet = None
    if command.fleet_id is not None:
        fleet = await session.scalar(
            select(Fleet).where(Fleet.fleet_id == command.fleet_id).with_for_update()
        )
        if fleet is None:
            raise FleetCommandError("fleet not found")
    if command.operation == "STATUS":
        if fleet is None or command.status not in ("ACTIVE", "INACTIVE"):
            raise FleetCommandError("invalid fleet status command")
        fleet.status = command.status
        fleet.updated_at = now
        return fleet.fleet_id
    if command.operation in ("JOIN", "LEAVE", "RIDER_CELL"):
        if command.rider_id is None:
            raise FleetCommandError("rider is required")
        profile, _availability = await _lock_rider(session, command.rider_id)
        if command.operation != "LEAVE":
            if profile.status != "ACTIVE":
                raise FleetCommandError("rider profile must be ACTIVE")
            await _require_fresh_rider_authorization(session, command.rider_id)
    if command.operation == "JOIN":
        if fleet is None or fleet.status != "ACTIVE":
            raise FleetCommandError("fleet must be ACTIVE")
        member = await session.scalar(
            select(FleetMembership).where(
                FleetMembership.rider_id == command.rider_id, FleetMembership.left_at.is_(None)
            )
        )
        if member is not None:
            if member.fleet_id != fleet.fleet_id:
                raise FleetCommandError("rider already belongs to another fleet")
            return member.fleet_membership_id
        member = FleetMembership(
            fleet_id=fleet.fleet_id, rider_id=command.rider_id, joined_at=now, created_at=now
        )
        session.add(member)
        await session.flush()
        return member.fleet_membership_id
    if command.operation == "LEAVE":
        member = cast(
            FleetMembership | None,
            await session.scalar(
                select(FleetMembership)
                .where(
                    FleetMembership.fleet_membership_id == command.membership_id,
                    FleetMembership.fleet_id == command.fleet_id,
                    FleetMembership.rider_id == command.rider_id,
                )
                .with_for_update()
            ),
        )
        if member is None or fleet is None:
            raise FleetCommandError("fleet membership not found")
        if member.left_at is None:
            member.left_at = now
        return member.fleet_membership_id
    if command.operation not in ("FLEET_CELL", "RIDER_CELL"):
        raise FleetCommandError("invalid fleet command")
    if (
        command.cell_id is None
        or not h3.is_valid_cell(command.cell_id)
        or h3.get_resolution(command.cell_id) != 7
        or command.active is None
    ):
        raise FleetCommandError("coverage requires a canonical H3 resolution-7 cell")
    if command.operation == "FLEET_CELL":
        if fleet is None:
            raise FleetCommandError("fleet is required")
        cell = await session.scalar(
            select(FleetServiceCell).where(
                FleetServiceCell.fleet_id == fleet.fleet_id,
                FleetServiceCell.cell_id == command.cell_id,
                FleetServiceCell.deactivated_at.is_(None),
            )
        )
        if cell is None:
            if not command.active:
                raise FleetCommandError("active fleet coverage not found")
            cell = FleetServiceCell(
                fleet_id=fleet.fleet_id, cell_id=command.cell_id, activated_at=now, created_at=now
            )
            session.add(cell)
        elif not command.active:
            cell.deactivated_at = now
        await session.flush()
        return cell.fleet_service_cell_id
    rider_cell = await session.scalar(
        select(RiderServiceCell).where(
            RiderServiceCell.rider_id == command.rider_id,
            RiderServiceCell.cell_id == command.cell_id,
            RiderServiceCell.deactivated_at.is_(None),
        )
    )
    if rider_cell is None:
        if not command.active:
            raise FleetCommandError("active rider coverage not found")
        rider_cell = RiderServiceCell(
            rider_id=command.rider_id, cell_id=command.cell_id, activated_at=now, created_at=now
        )
        session.add(rider_cell)
    elif not command.active:
        rider_cell.deactivated_at = now
    await session.flush()
    return rider_cell.rider_service_cell_id
