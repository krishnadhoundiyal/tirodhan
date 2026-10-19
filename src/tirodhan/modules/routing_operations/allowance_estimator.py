"""Non-trained time-bucket forecast. No routing search or feasibility certificate.

All travel assumptions are explicit regional configuration. Integer workload
footprints couple neighbouring slots; interval union preserves calendar gaps.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tirodhan.modules.collection_requests.scheduling import SERVICE_TIMEZONE, SlotWindow

BUCKET_SECONDS = 1800


class TravelBand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    start_minute: int = Field(strict=True, ge=0, lt=1440)
    end_minute: int = Field(strict=True, gt=0, le=1440)
    local_travel_seconds: int = Field(strict=True, ge=0, le=7200)
    clustered_travel_seconds: int = Field(strict=True, ge=0, le=7200)
    facility_travel_seconds: int = Field(strict=True, ge=0, le=7200)


class AllowancePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: int = Field(strict=True, ge=1)
    local_travel_seconds: int = Field(strict=True, ge=0, le=7200)
    clustered_travel_seconds: int = Field(strict=True, ge=0, le=7200)
    facility_travel_seconds: int = Field(strict=True, ge=0, le=7200)
    stem_seconds: int = Field(strict=True, ge=0, le=7200)
    cluster_threshold: int = Field(strict=True, ge=2, le=10000)
    pickup_start_offset_seconds: int = Field(strict=True, ge=0, le=1800)
    limited_bookings: int = Field(strict=True, ge=1, le=100)
    travel_bands: tuple[TravelBand, ...] = Field(default=(), max_length=24)

    @model_validator(mode="after")
    def coherent(self) -> AllowancePolicy:
        if self.clustered_travel_seconds > self.local_travel_seconds:
            raise ValueError("clustered travel cannot exceed dispersed travel")
        previous = 0
        for band in sorted(self.travel_bands, key=lambda b: b.start_minute):
            if (
                band.start_minute < previous
                or band.start_minute >= band.end_minute
                or band.clustered_travel_seconds > band.local_travel_seconds
            ):
                raise ValueError("invalid or overlapping travel bands")
            previous = band.end_minute
        return self

    def at(self, instant: datetime) -> AllowancePolicy:
        local = instant.astimezone(SERVICE_TIMEZONE)
        minute = local.hour * 60 + local.minute
        for band in self.travel_bands:
            if band.start_minute <= minute < band.end_minute:
                return self.model_copy(
                    update=band.model_dump(exclude={"start_minute", "end_minute"})
                )
        return self


@dataclass(frozen=True)
class Workload:
    slot_start: datetime
    slot_end: datetime
    pickup_seconds: int
    discharge_seconds: int
    clustered: bool = False


def footprint(work: Workload, policy: AllowancePolicy) -> dict[datetime, int]:
    """Forecast a nominal service start and both travel legs; never a route.

    New claims always use dispersed travel. Only existing regional cell/slot
    concentrations may receive the configured, bounded consolidation credit.
    """
    policy = policy.at(work.slot_start)
    travel = policy.clustered_travel_seconds if work.clustered else policy.local_travel_seconds
    start = work.slot_start + timedelta(seconds=policy.pickup_start_offset_seconds - travel)
    end = work.slot_start + timedelta(
        seconds=policy.pickup_start_offset_seconds
        + work.pickup_seconds
        + policy.facility_travel_seconds
        + work.discharge_seconds
    )
    cursor = start.replace(minute=30 * (start.minute // 30), second=0, microsecond=0)
    result = {}
    while cursor < end:
        right = cursor + timedelta(seconds=BUCKET_SECONDS)
        seconds = int((min(right, end) - max(cursor, start)).total_seconds())
        if seconds:
            result[cursor] = seconds
        cursor = right
    return result


def union_intervals(ranges: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    result: list[tuple[datetime, datetime]] = []
    for start, end in sorted(ranges):
        if start >= end:
            continue
        if result and start <= result[-1][1]:
            result[-1] = result[-1][0], max(end, result[-1][1])
        else:
            result.append((start, end))
    return result


def free_intervals(
    start: datetime, end: datetime, blocked: list[tuple[datetime, datetime]]
) -> list[tuple[datetime, datetime]]:
    result = []
    cursor = start
    for left, right in union_intervals(blocked):
        if left > cursor:
            result.append((cursor, min(left, end)))
        cursor = max(cursor, right)
        if cursor >= end:
            break
    if cursor < end:
        result.append((cursor, end))
    return [(a, b) for a, b in result if a < b]


def pickup_supply(
    slots: list[SlotWindow],
    free: list[tuple[datetime, datetime]],
    openings: list[tuple[datetime, datetime]],
    *,
    pickup_seconds: int,
    discharge_seconds: int,
    policy: AllowancePolicy,
) -> dict[datetime, int]:
    """Necessary calendar screen, preserving contiguous service and facility tails.

    Service may finish after its service-start slot. This screen is deliberately
    not a reconstruction of a journey; native operational validation is separate.
    """
    starts = []
    facility_travel = max(
        [policy.facility_travel_seconds] + [b.facility_travel_seconds for b in policy.travel_bands]
    )
    tail = timedelta(seconds=pickup_seconds + facility_travel + discharge_seconds)
    for left, right in free:
        for opened, closed in openings:
            low = max(left, opened - timedelta(seconds=policy.facility_travel_seconds))
            high = min(right, closed) - tail
            if low <= high:
                starts.append((low, high))
    # Count eligible pickup-seconds, not indivisible five-minute fragments.
    # Inclusive isolated start points still permit one standard service.
    result = {}
    for slot in slots:
        ranges = [
            (max(a, slot.start), min(b, slot.end))
            for a, b in starts
            if a <= slot.end and b >= slot.start
        ]
        result[slot.start] = sum(
            max(pickup_seconds, int((b - a).total_seconds()))
            for a, b in union_intervals([(a, b + timedelta(microseconds=1)) for a, b in ranges])
        )
    return result


def remaining_allowance(
    work: Workload,
    policy: AllowancePolicy,
    remaining: dict[tuple[datetime, str], int],
) -> int:
    values = [remaining.get((work.slot_start, "PICKUP"), 0) // max(1, work.pickup_seconds)]
    values.extend(
        remaining.get((bucket, "DUTY"), 0) // seconds
        for bucket, seconds in footprint(work, policy).items()
    )
    return max(0, min(values))
