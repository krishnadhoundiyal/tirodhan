from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from test_collection_payment import ITEMS, FixedPricing, create_context, create_user
from test_customer_mobile_core import app_for
from test_operational_api import _token_for

from tirodhan.api.routes import collection_requests, customer_reads
from tirodhan.db import values
from tirodhan.modules.collection_requests import service
from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.collection_requests.scheduling import (
    SERVICE_TIMEZONE,
    DelhiSlotAvailability,
    SlotConflictError,
    SlotWindow,
    daily_grid,
    operating_grid,
)
from tirodhan.modules.collection_requests.service import CreateCollectionRequestCommand
from tirodhan.modules.payments.models import Payment
from tirodhan.modules.planning import locking
from tirodhan.modules.planning.models import PlanningBatch
from tirodhan.modules.planning.service import PlanningWorkUnit, freeze_planning_batch
from tirodhan.modules.reliability.models import IdempotencyRecord
from tirodhan.modules.serviceability.models import ServiceabilityContext

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
def schedule_clock(monkeypatch: pytest.MonkeyPatch) -> list[datetime]:
    clock = [
        values.utc_now()
        .astimezone(SERVICE_TIMEZONE)
        .replace(hour=5, minute=0, second=0, microsecond=0)
    ]
    for module in (values, service, collection_requests, customer_reads):
        monkeypatch.setattr(module, "utc_now", lambda: clock[0].astimezone(timezone.utc))
    return clock


async def setup_context(
    factory: async_sessionmaker[AsyncSession], clock: list[datetime]
) -> tuple[Any, ServiceabilityContext]:
    owner = await create_user(factory)
    context = await create_context(factory, owner.user_id)
    async with factory() as session, session.begin():
        context.expires_at = clock[0] + timedelta(days=10)
        session.add(context)
    return owner, context


def command_for(
    owner: Any, context: ServiceabilityContext, slot: SlotWindow, now: datetime
) -> CreateCollectionRequestCommand:
    return CreateCollectionRequestCommand(
        owner.user_id,
        values.new_uuid7(),
        context.serviceability_context_id,
        slot.start,
        slot.end,
        ITEMS,
        now + timedelta(hours=1),
    )


async def book(
    factory: async_sessionmaker[AsyncSession],
    command: CreateCollectionRequestCommand,
    now: datetime,
    *,
    lead: int | None = 17,
) -> Any:
    # Exercise the production default rather than injecting a synthetic policy.
    return await service.create_collection_request(
        factory,
        command,
        FixedPricing(),
        idempotency_expires_at=now + timedelta(days=1),
        planning_lead_time_minutes=lead,
    )


async def seed_freeze(
    factory: async_sessionmaker[AsyncSession],
    context: ServiceabilityContext,
    slot: SlotWindow,
    now: datetime,
    *,
    cell_id: str | None = None,
) -> None:
    async with factory() as session, session.begin():
        session.add(
            PlanningBatch(
                planning_batch_id=values.new_uuid7(),
                cell_id=cell_id or context.cell_id,
                slot_start=slot.start,
                slot_end=slot.end,
                status="READY",
                max_attempts_snapshot=1,
                created_at=now,
            )
        )


async def test_default_schedule_is_backend_owned_224_windows_utc_and_one_freeze_query(
    database_session_factory: async_sessionmaker[AsyncSession],
    database_engine: AsyncEngine,
    schedule_clock: list[datetime],
) -> None:
    owner, context = await setup_context(database_session_factory, schedule_clock)
    app = app_for(database_session_factory, owner.user_id)
    assert isinstance(app.state.slot_availability, DelhiSlotAvailability)
    statements: list[str] = []

    def capture(
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _execution_context: Any,
        _many: bool,
    ) -> None:
        statements.append(statement)

    event.listen(database_engine.sync_engine, "before_cursor_execute", capture)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            params = {
                "serviceability_context_id": str(context.serviceability_context_id),
                "date": "1900-01-01",
            }
            response = await client.get("/v1/customer/pickup-slots", params=params)
            assert response.status_code == 200, response.text
            assert len(statements) == 3
            assert sum("FROM planning_batch" in s for s in statements) == 1
            slots = response.json()["slots"]
            assert len(slots) == 224
            today = schedule_clock[0].date()
            expected = [s for i in range(7) for s in operating_grid(today + timedelta(days=i))]
            assert [s["start"] for s in slots] == [
                s.start.isoformat().replace("+00:00", "Z") for s in expected
            ]
            assert [s["end"] for s in slots] == [
                s.end.isoformat().replace("+00:00", "Z") for s in expected
            ]
            assert [s["slot_id"] for s in slots] == [s.slot_id for s in expected]
            assert [s["label"] for s in slots] == [s.label for s in expected]
            assert {s["availability"] for s in slots} == {"AVAILABLE"}
            assert "no-store" in response.headers["cache-control"]
            statements.clear()
            params["date"] = "2099-12-31"
            assert (
                await client.get("/v1/customer/pickup-slots", params=params)
            ).json() == response.json()
            assert len(statements) == 3
    finally:
        event.remove(database_engine.sync_engine, "before_cursor_execute", capture)


async def test_read_excludes_past_cutoff_and_exact_frozen_work_units(
    database_session_factory: async_sessionmaker[AsyncSession],
    schedule_clock: list[datetime],
) -> None:
    owner, context = await setup_context(database_session_factory, schedule_clock)
    today = schedule_clock[0].date()
    frozen = operating_grid(today + timedelta(days=1))[0]
    other_cell = operating_grid(today + timedelta(days=1))[1]
    await seed_freeze(database_session_factory, context, frozen, schedule_clock[0])
    await seed_freeze(
        database_session_factory, context, other_cell, schedule_clock[0], cell_id="different-cell"
    )
    app = app_for(database_session_factory, owner.user_id)
    app.state.settings.planning_lead_time_minutes = 17
    schedule_clock[0] = schedule_clock[0].replace(hour=12, minute=13)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/v1/customer/pickup-slots",
            params={"serviceability_context_id": str(context.serviceability_context_id)},
        )
        assert response.status_code == 200, response.text
        slots = response.json()["slots"]
        assert (
            slots[0]["slot_id"] == operating_grid(today)[14].slot_id
        )  # 13:00; 12:30 is at cutoff.
        assert frozen.slot_id not in {s["slot_id"] for s in slots}
        assert other_cell.slot_id in {s["slot_id"] for s in slots}
        schedule_clock[0] -= timedelta(microseconds=1)
        before = (
            await client.get(
                "/v1/customer/pickup-slots",
                params={"serviceability_context_id": str(context.serviceability_context_id)},
            )
        ).json()["slots"]
        assert before[0]["slot_id"] == operating_grid(today)[13].slot_id


@pytest.mark.parametrize(
    "state,expected",
    [
        ("expired", 409),
        ("UNSERVICEABLE", 409),
        ("TECHNICAL_FAILURE", 503),
        ("PENDING", 409),
        ("foreign", 404),
        ("no_cell", 409),
        ("no_location", 409),
        ("missing", 404),
    ],
)
async def test_authoritative_context_required_for_slot_read(
    database_session_factory: async_sessionmaker[AsyncSession],
    schedule_clock: list[datetime],
    state: str,
    expected: int,
) -> None:
    owner, context = await setup_context(database_session_factory, schedule_clock)
    changes: dict[str, Any] = {}
    if state == "expired":
        changes["expires_at"] = schedule_clock[0]
    elif state == "foreign":
        other = await create_user(database_session_factory)
        changes["user_id"] = other.user_id
    elif state == "no_cell":
        changes["cell_id"] = None
    elif state == "no_location":
        changes["location"] = None
    elif state != "missing":
        changes["status"] = state
    if changes:
        async with database_session_factory() as session, session.begin():
            await session.execute(
                update(ServiceabilityContext)
                .where(
                    ServiceabilityContext.serviceability_context_id
                    == context.serviceability_context_id
                )
                .values(**changes)
            )
    app = app_for(database_session_factory, owner.user_id)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/v1/customer/pickup-slots",
            params={
                "serviceability_context_id": str(
                    values.new_uuid7() if state == "missing" else context.serviceability_context_id
                )
            },
        )
        assert response.status_code == expected
        assert "slots" not in response.json()


async def test_missing_config_empty_eligible_set_and_local_midnight_rollover(
    database_session_factory: async_sessionmaker[AsyncSession],
    schedule_clock: list[datetime],
) -> None:
    owner, context = await setup_context(database_session_factory, schedule_clock)
    app = app_for(database_session_factory, owner.user_id)
    params = {"serviceability_context_id": str(context.serviceability_context_id)}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        app.state.settings.planning_lead_time_minutes = None
        assert (await client.get("/v1/customer/pickup-slots", params=params)).status_code == 503
        app.state.settings.planning_lead_time_minutes = 7 * 24 * 60
        assert (await client.get("/v1/customer/pickup-slots", params=params)).json()["slots"] == []
        app.state.settings.planning_lead_time_minutes = 17
        schedule_clock[0] = schedule_clock[0].replace(hour=23, minute=59, second=59)
        before = (await client.get("/v1/customer/pickup-slots", params=params)).json()["slots"]
        assert len(before) == 192
        schedule_clock[0] += timedelta(seconds=1)
        after = (await client.get("/v1/customer/pickup-slots", params=params)).json()["slots"]
        assert len(after) == 224
        assert before[0] == after[0]
        assert before[-1]["slot_id"] != after[-1]["slot_id"]


@pytest.mark.parametrize(
    "case",
    [
        "before_hours",
        "after_hours",
        "outside_horizon",
        "duration",
        "alignment",
        "seconds",
        "microseconds",
        "past",
        "cutoff",
    ],
)
async def test_booking_independently_rejects_ineligible_client_windows(
    database_session_factory: async_sessionmaker[AsyncSession],
    schedule_clock: list[datetime],
    case: str,
) -> None:
    owner, context = await setup_context(database_session_factory, schedule_clock)
    today = schedule_clock[0].date()
    slot = operating_grid(today + timedelta(days=1))[0]
    if case == "before_hours":
        slot = daily_grid(today + timedelta(days=1))[11]
    elif case == "after_hours":
        slot = daily_grid(today + timedelta(days=1))[44]
    elif case == "outside_horizon":
        slot = operating_grid(today + timedelta(days=7))[0]
    elif case == "duration":
        slot = SlotWindow(slot.start, slot.end + timedelta(minutes=30))
    elif case in ("alignment", "seconds", "microseconds"):
        offset = {
            "alignment": timedelta(minutes=1),
            "seconds": timedelta(seconds=1),
            "microseconds": timedelta(microseconds=1),
        }[case]
        slot = SlotWindow(slot.start + offset, slot.end + offset)
    elif case == "past":
        slot = operating_grid(today)[0]
        schedule_clock[0] = slot.start
    elif case == "cutoff":
        slot = operating_grid(today)[0]
        schedule_clock[0] = slot.start - timedelta(minutes=17)
    cmd = command_for(owner, context, slot, schedule_clock[0])
    app = app_for(database_session_factory, owner.user_id)
    app.state.settings.planning_lead_time_minutes = 17
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/collection-requests",
            json={
                "client_request_id": str(cmd.client_request_id),
                "serviceability_context_id": str(cmd.serviceability_context_id),
                "slot_start": cmd.slot_start.isoformat(),
                "slot_end": cmd.slot_end.isoformat(),
                "items": [{"item_category_code": i.item_category_code} for i in ITEMS],
            },
        )
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == (
            "PLANNING_CUTOFF_REACHED" if case == "cutoff" else "SLOT_UNAVAILABLE"
        )
    async with database_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(CollectionRequest)) == 0
        assert await session.scalar(select(func.count()).select_from(IdempotencyRecord)) == 0


async def test_concurrent_bookings_have_no_quota_and_exact_replay_survives_horizon_and_freeze(
    database_session_factory: async_sessionmaker[AsyncSession],
    schedule_clock: list[datetime],
) -> None:
    owner, context = await setup_context(database_session_factory, schedule_clock)
    slot = operating_grid(schedule_clock[0].date())[0]
    cmd = command_for(owner, context, slot, schedule_clock[0])
    second_owner, second_context = await setup_context(database_session_factory, schedule_clock)
    second = command_for(second_owner, second_context, slot, schedule_clock[0])
    results = await asyncio.gather(
        book(database_session_factory, cmd, schedule_clock[0]),
        book(database_session_factory, cmd, schedule_clock[0]),
        book(database_session_factory, second, schedule_clock[0]),
    )
    assert results[0].request.request_id == results[1].request.request_id
    assert results[0].request.request_id != results[2].request.request_id
    async with database_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(CollectionRequest)) == 2
        assert await session.scalar(select(func.count()).select_from(Payment)) == 2
        assert await session.scalar(select(func.count()).select_from(IdempotencyRecord)) == 2
    await seed_freeze(database_session_factory, context, slot, schedule_clock[0])
    schedule_clock[0] += timedelta(days=8)
    pricing = FixedPricing()
    replay = await service.create_collection_request(
        database_session_factory,
        cmd,
        pricing,
        idempotency_expires_at=schedule_clock[0] + timedelta(days=1),
    )
    assert replay.request.request_id == results[0].request.request_id
    assert pricing.calls == 0


@pytest.mark.parametrize("change", ["cutoff", "expired", "midnight"])
async def test_booking_rechecks_time_and_context_after_actual_advisory_lock_wait(
    database_session_factory: async_sessionmaker[AsyncSession],
    schedule_clock: list[datetime],
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    owner, context = await setup_context(database_session_factory, schedule_clock)
    slot = operating_grid(schedule_clock[0].date())[0]
    cmd = command_for(owner, context, slot, schedule_clock[0])
    reached = asyncio.Event()
    original = locking.acquire_work_unit_advisory_lock

    async def signal_lock(session: AsyncSession, **kwargs: Any) -> int:
        reached.set()
        return await original(session, **kwargs)

    from tirodhan.modules.collection_requests import scheduling

    monkeypatch.setattr(scheduling, "acquire_work_unit_advisory_lock", signal_lock)
    if change == "expired":
        async with database_session_factory() as session, session.begin():
            await session.execute(
                update(ServiceabilityContext)
                .where(
                    ServiceabilityContext.serviceability_context_id
                    == context.serviceability_context_id
                )
                .values(expires_at=schedule_clock[0] + timedelta(minutes=1))
            )
    async with database_session_factory() as session, session.begin():
        await original(session, cell_id=context.cell_id, slot_start=slot.start, slot_end=slot.end)
        task = asyncio.create_task(book(database_session_factory, cmd, schedule_clock[0]))
        await asyncio.wait_for(reached.wait(), timeout=10)
        schedule_clock[0] = (
            slot.start - timedelta(minutes=17)
            if change == "cutoff"
            else schedule_clock[0] + timedelta(minutes=2)
            if change == "expired"
            else schedule_clock[0] + timedelta(days=1)
        )
    with pytest.raises((SlotConflictError, service.CustomerReadError)) as error:
        await asyncio.wait_for(task, timeout=10)
    assert error.value.code == (
        "SERVICEABILITY_EXPIRED" if change == "expired" else "PLANNING_CUTOFF_REACHED"
    )
    async with database_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(CollectionRequest)) == 0


async def test_real_planning_freeze_wins_against_blocked_fresh_booking(
    database_session_factory: async_sessionmaker[AsyncSession],
    schedule_clock: list[datetime],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, context = await setup_context(database_session_factory, schedule_clock)
    slot = operating_grid(schedule_clock[0].date())[0]
    cmd = command_for(owner, context, slot, schedule_clock[0])
    existing = await book(database_session_factory, cmd, schedule_clock[0])
    async with database_session_factory() as session, session.begin():
        await session.execute(
            update(CollectionRequest)
            .where(CollectionRequest.request_id == existing.request.request_id)
            .values(status="ACCEPTED", accepted_at=schedule_clock[0])
        )
    entered = asyncio.Event()
    continue_freeze = asyncio.Event()
    original = locking.acquire_work_unit_advisory_lock

    async def pause_freeze(session: AsyncSession, **kwargs: Any) -> int:
        key = await original(session, **kwargs)
        entered.set()
        await continue_freeze.wait()
        return key

    from tirodhan.modules.planning import service as planning_service

    monkeypatch.setattr(planning_service, "acquire_work_unit_advisory_lock", pause_freeze)
    freeze = asyncio.create_task(
        freeze_planning_batch(
            database_session_factory,
            PlanningWorkUnit(context.cell_id, slot.start, slot.end),
            lead_time_minutes=17,
            max_attempts=1,
            compaction_distance_m=100,
            max_group_requests=2,
            now=slot.start - timedelta(minutes=17),
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=10)
    booking_reached = asyncio.Event()

    async def booking_lock(session: AsyncSession, **kwargs: Any) -> int:
        booking_reached.set()
        return await original(session, **kwargs)

    from tirodhan.modules.collection_requests import scheduling

    monkeypatch.setattr(scheduling, "acquire_work_unit_advisory_lock", booking_lock)
    fresh_owner, fresh_context = await setup_context(database_session_factory, schedule_clock)
    fresh = asyncio.create_task(
        book(
            database_session_factory,
            command_for(fresh_owner, fresh_context, slot, schedule_clock[0]),
            schedule_clock[0],
        )
    )
    await asyncio.wait_for(booking_reached.wait(), timeout=10)
    continue_freeze.set()
    frozen = await asyncio.wait_for(freeze, timeout=10)
    assert frozen.created and frozen.transitioned_request_count == 1
    with pytest.raises(SlotConflictError) as error:
        await asyncio.wait_for(fresh, timeout=10)
    assert error.value.code == "PLANNING_STARTED"
    async with database_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(CollectionRequest)) == 1
        assert await session.scalar(select(func.count()).select_from(IdempotencyRecord)) == 1
        assert (
            await session.get(CollectionRequest, existing.request.request_id)
        ).status == "PRE_PLANNING"


async def test_live_customer_role_required_for_production_schedule(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    owner = await create_user(database_session_factory)
    context = await create_context(database_session_factory, owner.user_id)
    app = app_for(database_session_factory, None)
    token, _ = await _token_for(database_session_factory, owner.user_id, roles=("RIDER",))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        path = "/v1/customer/pickup-slots"
        params = {"serviceability_context_id": str(context.serviceability_context_id)}
        assert (await client.get(path, params=params)).status_code == 401
        assert (
            await client.get(path, params=params, headers={"Authorization": f"Bearer {token}"})
        ).status_code == 403
        customer_token, _ = await _token_for(
            database_session_factory, owner.user_id, roles=("CUSTOMER",)
        )
        assert (
            await client.get(
                path, params=params, headers={"Authorization": f"Bearer {customer_token}"}
            )
        ).status_code == 200


async def test_offered_slot_does_not_reserve_and_final_horizon_window_can_be_booked(
    database_session_factory: async_sessionmaker[AsyncSession],
    schedule_clock: list[datetime],
) -> None:
    owner, context = await setup_context(database_session_factory, schedule_clock)
    app = app_for(database_session_factory, owner.user_id)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/v1/customer/pickup-slots",
            params={"serviceability_context_id": str(context.serviceability_context_id)},
        )
        offered = response.json()["slots"]
        first = operating_grid(schedule_clock[0].date())[0]
        await seed_freeze(database_session_factory, context, first, schedule_clock[0])
        body = {
            "client_request_id": str(values.new_uuid7()),
            "serviceability_context_id": str(context.serviceability_context_id),
            "slot_start": offered[0]["start"],
            "slot_end": offered[0]["end"],
            "items": [{"item_category_code": item.item_category_code} for item in ITEMS],
        }
        rejected = await client.post("/v1/collection-requests", json=body)
        assert rejected.status_code == 409
        assert rejected.json()["error"]["code"] == "PLANNING_STARTED"
        final = offered[-1]
        body.update(
            client_request_id=str(values.new_uuid7()),
            slot_start=final["start"],
            slot_end=final["end"],
        )
        accepted = await client.post("/v1/collection-requests", json=body)
        assert accepted.status_code == 201, accepted.text
        replay = await client.post("/v1/collection-requests", json=body)
        assert replay.json() == accepted.json()
        async with database_session_factory() as session:
            stored = await session.get(CollectionRequest, UUID(accepted.json()["request_id"]))
            assert stored is not None
            assert stored.slot_start == datetime.fromisoformat(
                final["start"].replace("Z", "+00:00")
            )
            assert stored.slot_end == datetime.fromisoformat(final["end"].replace("Z", "+00:00"))
