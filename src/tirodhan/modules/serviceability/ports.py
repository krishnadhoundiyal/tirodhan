from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from tirodhan.modules.customers.service import GeoPoint


class LocationResolutionStatus(str, Enum):
    RESOLVED = "RESOLVED"
    UNSERVICEABLE = "UNSERVICEABLE"
    TECHNICAL_FAILURE = "TECHNICAL_FAILURE"


@dataclass(frozen=True, slots=True)
class LocationResolution:
    status: LocationResolutionStatus
    location: GeoPoint | None = None
    failure_code: str | None = None


class LocationResolver(Protocol):
    """Validate supplied coordinates or resolve an address to a serviceable point."""

    async def resolve(
        self, *, address: str, supplied_location: GeoPoint | None
    ) -> LocationResolution: ...


class CellIdDeriver(Protocol):
    """Derive the approved cell identifier from a resolved point."""

    async def derive(self, location: GeoPoint) -> str: ...


class ServiceabilityResolverNotConfiguredError(RuntimeError):
    pass


class UnconfiguredLocationResolver:
    async def resolve(
        self, *, address: str, supplied_location: GeoPoint | None
    ) -> LocationResolution:
        raise ServiceabilityResolverNotConfiguredError(
            "no production location/serviceability resolver is configured"
        )


class UnconfiguredCellIdDeriver:
    async def derive(self, location: GeoPoint) -> str:
        raise ServiceabilityResolverNotConfiguredError(
            "no approved cell technology or resolution is configured"
        )
