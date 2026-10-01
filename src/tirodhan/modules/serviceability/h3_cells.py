from __future__ import annotations

import math

import h3  # type: ignore[import-untyped]  # Upstream H3 bindings do not ship typing metadata.

from tirodhan.modules.customers.service import GeoPoint
from tirodhan.modules.serviceability.service import InvalidServiceabilityInputError

H3_RESOLUTION = 7


def validate_point(point: GeoPoint) -> None:
    if not (
        math.isfinite(point.latitude)
        and math.isfinite(point.longitude)
        and -90 <= point.latitude <= 90
        and -180 <= point.longitude <= 180
    ):
        raise InvalidServiceabilityInputError("location coordinates are invalid")


class H3CellIdDeriver:
    async def derive(self, location: GeoPoint) -> str:
        validate_point(location)
        return str(h3.latlng_to_cell(location.latitude, location.longitude, H3_RESOLUTION))
