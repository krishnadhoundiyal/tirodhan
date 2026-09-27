from __future__ import annotations

from datetime import datetime, timedelta, timezone


class PlanningConfigurationError(RuntimeError):
    pass


def _positive_minutes(value: int | None, *, setting_name: str) -> int:
    if value is None:
        raise PlanningConfigurationError(f"{setting_name} is not configured")
    if value <= 0:
        raise PlanningConfigurationError(f"{setting_name} must be positive")
    return value


def as_utc_instant(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("planning timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def planning_cutoff_time(slot_start: datetime, lead_time_minutes: int | None) -> datetime:
    lead_time = _positive_minutes(
        lead_time_minutes,
        setting_name="PLANNING_LEAD_TIME_MINUTES",
    )
    return as_utc_instant(slot_start) - timedelta(minutes=lead_time)


def planning_cutoff_reached(
    slot_start: datetime,
    lead_time_minutes: int | None,
    *,
    now: datetime,
) -> bool:
    return as_utc_instant(now) >= planning_cutoff_time(slot_start, lead_time_minutes)


def latest_due_slot_start(now: datetime, lead_time_minutes: int | None) -> datetime:
    lead_time = _positive_minutes(
        lead_time_minutes,
        setting_name="PLANNING_LEAD_TIME_MINUTES",
    )
    return as_utc_instant(now) + timedelta(minutes=lead_time)


def require_planning_max_attempts(value: int | None) -> int:
    if value is None:
        raise PlanningConfigurationError("PLANNING_MAX_ATTEMPTS is not configured")
    if value <= 0:
        raise PlanningConfigurationError("PLANNING_MAX_ATTEMPTS must be positive")
    return value
