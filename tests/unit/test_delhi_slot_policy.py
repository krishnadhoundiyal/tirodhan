from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from tirodhan.modules.collection_requests.scheduling import (
    SERVICE_TIMEZONE,
    DelhiSlotAvailability,
    SchedulingUnavailableError,
    SlotConflictError,
    SlotWindow,
    daily_grid,
    evaluate_slot,
    operating_grid,
)
from tirodhan.modules.planning.policy import PlanningConfigurationError

NOW = datetime(2026, 10, 9, 5, tzinfo=SERVICE_TIMEZONE)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "now,today",
    [
        (NOW, date(2026, 10, 9)),
        (datetime(2026, 10, 9, 18, 29, 59, tzinfo=timezone.utc), date(2026, 10, 9)),
        (datetime(2026, 10, 9, 18, 30, tzinfo=timezone.utc), date(2026, 10, 10)),
        (datetime(2026, 12, 31, 18, 30, tzinfo=timezone.utc), date(2027, 1, 1)),
    ],
)
async def test_seven_inclusive_local_dates_at_midnight_and_year_rollover(
    now: datetime, today: date
) -> None:
    session = AsyncMock(spec=AsyncSession)
    dates = await DelhiSlotAvailability().service_dates(session, cell_id="resolved-cell", now=now)
    assert dates == tuple(today + timedelta(days=i) for i in range(7))
    session.execute.assert_not_called()


def test_operating_grid_preserves_48_grid_and_32_exact_intervals() -> None:
    day = NOW.date()
    full = daily_grid(day)
    slots = operating_grid(day)
    assert len(full) == 48 and len(slots) == 32
    assert slots == full[12:44]
    assert slots[0].start == datetime(2026, 10, 9, 0, 30, tzinfo=timezone.utc)
    assert slots[0].end == datetime(2026, 10, 9, 1, tzinfo=timezone.utc)
    assert slots[-1].start == datetime(2026, 10, 9, 16, tzinfo=timezone.utc)
    assert slots[-1].end == datetime(2026, 10, 9, 16, 30, tzinfo=timezone.utc)
    assert slots[0].slot_id == "kolkata-20261009T0600"
    assert slots[-1].label == "09 Oct · 21:30–22:00"
    assert all(s.end - s.start == timedelta(minutes=30) for s in slots)
    assert len({s.slot_id for s in slots}) == 32


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "index,expected",
    [(0, None), (11, None), (12, "AVAILABLE"), (43, "AVAILABLE"), (44, None), (47, None)],
)
async def test_operating_hours_boundaries(index: int, expected: str | None) -> None:
    session = AsyncMock(spec=AsyncSession)
    slot = daily_grid(NOW.date())[index]
    assert (
        await DelhiSlotAvailability().availability(
            session, cell_id="resolved-cell", slot=slot, now=NOW
        )
        == expected
    )
    session.execute.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "days,expected", [(-1, None), (0, "AVAILABLE"), (6, "AVAILABLE"), (7, None)]
)
async def test_horizon_inclusive_end_and_expiry(days: int, expected: str | None) -> None:
    slot = operating_grid(NOW.date() + timedelta(days=days))[0]
    assert (
        await DelhiSlotAvailability().availability(
            AsyncMock(spec=AsyncSession), cell_id="resolved-cell", slot=slot, now=NOW
        )
        == expected
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("offset", [timedelta(minutes=1), timedelta(), timedelta(seconds=1)])
async def test_current_day_past_or_started_slots_are_ineligible(offset: timedelta) -> None:
    slot = operating_grid(NOW.date())[0]
    assert (
        await DelhiSlotAvailability().availability(
            AsyncMock(spec=AsyncSession),
            cell_id="resolved-cell",
            slot=slot,
            now=slot.start + offset,
        )
        is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("minutes,seconds", [(15, 0), (60, 0), (30, 1)])
async def test_policy_rejects_arbitrary_duration_or_alignment(minutes: int, seconds: int) -> None:
    start = operating_grid(NOW.date())[0].start + timedelta(seconds=seconds)
    assert (
        await DelhiSlotAvailability().availability(
            AsyncMock(spec=AsyncSession),
            cell_id="resolved-cell",
            slot=SlotWindow(start, start + timedelta(minutes=minutes)),
            now=NOW,
        )
        is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("seconds,eligible", [(-1, True), (0, False), (1, False)])
async def test_configured_cutoff_is_exclusive(seconds: int, eligible: bool) -> None:
    slot = operating_grid(NOW.date())[0]
    session = AsyncMock(spec=AsyncSession)
    args = dict(
        cell_id="resolved-cell",
        slot=slot,
        now=slot.start - timedelta(minutes=17) + timedelta(seconds=seconds),
        lead_time_minutes=17,
        frozen=False,
    )
    if eligible:
        assert await evaluate_slot(session, DelhiSlotAvailability(), **args) == "AVAILABLE"
    else:
        with pytest.raises(SlotConflictError) as error:
            await evaluate_slot(session, DelhiSlotAvailability(), **args)
        assert error.value.code == "PLANNING_CUTOFF_REACHED"
    session.execute.assert_not_called()


@pytest.mark.asyncio
async def test_missing_authority_and_lead_time_fail_closed() -> None:
    session = AsyncMock(spec=AsyncSession)
    with pytest.raises(SchedulingUnavailableError):
        await DelhiSlotAvailability().service_dates(session, cell_id="", now=NOW)
    with pytest.raises(PlanningConfigurationError):
        await evaluate_slot(
            session,
            DelhiSlotAvailability(),
            cell_id="resolved-cell",
            slot=operating_grid(NOW.date())[0],
            now=NOW,
            lead_time_minutes=None,
            frozen=False,
        )
    with pytest.raises(SlotConflictError) as error:
        await evaluate_slot(
            session,
            DelhiSlotAvailability(),
            cell_id="resolved-cell",
            slot=operating_grid(NOW.date())[0],
            now=NOW,
            lead_time_minutes=17,
            frozen=True,
        )
    assert error.value.code == "PLANNING_STARTED"
