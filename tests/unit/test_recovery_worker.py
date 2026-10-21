"""Exercise the real worker entry point without policy or external infrastructure."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_configured_regional_worker_maintains_before_planning_without_policy(monkeypatch):
    from tirodhan.modules.routing_operations import capacity_maintenance
    from tirodhan.workers import routing_regional as worker

    order = []
    engine = SimpleNamespace(dispose=AsyncMock())
    monkeypatch.setattr(worker, "create_database_engine", lambda _: engine)
    monkeypatch.setattr(worker, "create_session_factory", lambda _: object())

    @asynccontextmanager
    async def roads(profiles):
        yield {}

    async def maintain(*args, **kwargs):
        order.append("maintenance")

    async def run(*args, **kwargs):
        order.append("planning")

    monkeypatch.setattr(worker, "road_sources", roads)
    monkeypatch.setattr(worker, "run_region", run)
    monkeypatch.setattr(capacity_maintenance, "maintain_capacity", maintain)
    settings = SimpleNamespace(
        routing_dispatch_enabled=True,
        routing_fleet_enabled=True,
        routing_orchestration_enabled=True,
    )
    assert await worker.execute(uuid4(), [], settings) == "NOT_DUE_OR_OWNED"
    assert order == ["maintenance", "planning"]
    engine.dispose.assert_awaited_once()
