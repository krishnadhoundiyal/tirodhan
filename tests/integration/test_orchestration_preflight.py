"""Cheap preflight respects authoritative cycle ownership and coalescing deadlines."""

from datetime import timedelta

import pytest
from sqlalchemy import func, select
from test_operational_orchestration import regional

from tirodhan.core.config import Settings
from tirodhan.db.values import new_uuid7
from tirodhan.modules.routing_operations.fleet_models import FleetControl
from tirodhan.modules.routing_operations.models import VehiclePosition
from tirodhan.modules.routing_operations.orchestration import wake
from tirodhan.modules.routing_operations.retention import retention_inventory
from tirodhan.workers import routing_probe

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_probe_does_not_load_roads_before_cycle_deadline_or_during_owner_lease(
    database_session_factory,
    monkeypatch,
):
    factory = database_session_factory
    data = await regional(factory)
    now = data[6][0]
    monkeypatch.setattr(routing_probe, "utc_now", lambda: now)
    settings = Settings(
        _env_file=None,
        database_url=factory.kw["bind"].url.render_as_string(hide_password=False),
        routing_fleet_enabled=True,
        routing_dispatch_enabled=True,
        routing_orchestration_enabled=True,
    )
    async with factory() as session, session.begin():
        control = await session.get(FleetControl, "COMMITTED_FLEET")
        control.next_due_at = now + timedelta(seconds=60)
    # The initial source differs, but the authoritative worker is not claimable yet.
    assert not (await routing_probe.execute(settings))["native_work_due"]
    async with factory() as session, session.begin():
        await wake(session, "URGENT_CANCEL", now, urgent=True)
    assert (await routing_probe.execute(settings))["native_work_due"]
    async with factory() as session, session.begin():
        control = await session.get(FleetControl, "COMMITTED_FLEET")
        control.lease_token = new_uuid7()
        control.lease_until = now + timedelta(seconds=300)
    assert not (await routing_probe.execute(settings))["native_work_due"]
    async with factory() as session:
        control = await session.scalar(select(FleetControl))
        assert control.next_due_at == now + timedelta(seconds=60)
        assert control.lease_until == now + timedelta(seconds=300)
        positions_before = await session.scalar(select(func.count()).select_from(VehiclePosition))
    inventory = await retention_inventory(factory, now=now + timedelta(days=8))
    assert inventory["closed_routes_older_than_90_days"] == 0
    async with factory() as session:
        assert (
            await session.scalar(select(func.count()).select_from(VehiclePosition))
            == positions_before
        )
