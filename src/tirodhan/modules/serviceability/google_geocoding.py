from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

import httpx

from tirodhan.modules.customers.service import GeoPoint
from tirodhan.modules.serviceability.h3_cells import validate_point
from tirodhan.modules.serviceability.ports import (
    LocationResolution,
    LocationResolutionStatus,
    ServiceabilityResolverNotConfiguredError,
)

GEOCODING_URL = "https://maps.googleapis.com/maps/api/geocode/json"
DEFAULT_DELHI_ALIASES = ("Delhi", "DL", "National Capital Territory of Delhi", "NCT of Delhi")


class GoogleGeocodingConfigurationError(ServiceabilityResolverNotConfiguredError):
    pass


def _failure(code: str, *, technical: bool = False) -> LocationResolution:
    return LocationResolution(
        LocationResolutionStatus.TECHNICAL_FAILURE
        if technical
        else LocationResolutionStatus.UNSERVICEABLE,
        failure_code=code,
    )


class GoogleGeocodingLocationResolver:
    """Stable v3 REST. The caller owns the reusable client; no provider payload escapes."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        api_key: str,
        timeout_seconds: float,
        delhi_admin_aliases: Sequence[str] = DEFAULT_DELHI_ALIASES,
    ) -> None:
        if (
            not api_key.strip()
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 60
        ):
            raise GoogleGeocodingConfigurationError("Google Geocoding configuration is invalid")
        self._aliases = frozenset(alias.strip().casefold() for alias in delhi_admin_aliases)
        if not self._aliases or "" in self._aliases:
            raise GoogleGeocodingConfigurationError("Delhi administrative aliases are invalid")
        self._client = client
        self._key = api_key
        self._timeout = httpx.Timeout(timeout_seconds)

    async def resolve(
        self, *, address: str, supplied_location: GeoPoint | None
    ) -> LocationResolution:
        params = {"key": self._key}
        if supplied_location is not None:
            validate_point(supplied_location)
            params["latlng"] = f"{supplied_location.latitude},{supplied_location.longitude}"
        else:
            params.update(address=address, components="country:IN", region="in")
        try:
            response = await self._client.get(
                GEOCODING_URL, params=params, timeout=self._timeout, follow_redirects=False
            )
        except httpx.TimeoutException:
            return _failure("PROVIDER_TIMEOUT", technical=True)
        except httpx.RequestError:
            return _failure("PROVIDER_UNAVAILABLE", technical=True)
        if response.status_code in (401, 403):
            raise GoogleGeocodingConfigurationError("Google Geocoding configuration is invalid")
        if response.status_code == 429:
            return _failure("PROVIDER_RATE_LIMITED", technical=True)
        if response.status_code >= 500:
            return _failure("PROVIDER_UNAVAILABLE", technical=True)
        if not response.is_success:
            return _failure("PROVIDER_RESPONSE_INVALID", technical=True)
        try:
            body = response.json()
            if not isinstance(body, dict):
                raise ValueError
            status = body.get("status")
            if status in ("REQUEST_DENIED", "INVALID_REQUEST"):
                raise GoogleGeocodingConfigurationError("Google Geocoding configuration is invalid")
            if status == "ZERO_RESULTS":
                return _failure("NO_LOCATION_MATCH")
            if status in ("OVER_QUERY_LIMIT", "OVER_DAILY_LIMIT"):
                return _failure("PROVIDER_RATE_LIMITED", technical=True)
            if status == "UNKNOWN_ERROR":
                return _failure("PROVIDER_UNAVAILABLE", technical=True)
            results = body.get("results")
            if status != "OK" or not isinstance(results, list) or not results:
                raise ValueError
            # Reverse geocoding legitimately returns several levels for one pin.
            # Validate consistent administrative identity, not Google's snapped geometry.
            areas = [self._area(result) for result in results]
            if len(set(areas)) != 1:
                return _failure("AMBIGUOUS_LOCATION")
            if not areas[0]:
                return _failure("OUTSIDE_SERVICE_AREA")
            if supplied_location is not None:
                return LocationResolution(LocationResolutionStatus.RESOLVED, supplied_location)
            if len(results) != 1:
                return _failure("AMBIGUOUS_LOCATION")
            result = results[0]
            geometry = result.get("geometry")
            if not isinstance(geometry, dict):
                raise ValueError
            types = result.get("types")
            if (
                result.get("partial_match", False) is not False
                or geometry.get("location_type") not in ("ROOFTOP", "RANGE_INTERPOLATED")
                or not isinstance(types, list)
                or not set(types).intersection({"street_address", "premise", "subpremise"})
            ):
                return _failure("AMBIGUOUS_LOCATION")
            coordinates = geometry.get("location")
            if not isinstance(coordinates, dict):
                raise ValueError
            lat, lng = coordinates.get("lat"), coordinates.get("lng")
            if (
                isinstance(lat, bool)
                or isinstance(lng, bool)
                or not isinstance(lat, (int, float))
                or not isinstance(lng, (int, float))
            ):
                raise ValueError
            point = GeoPoint(latitude=float(lat), longitude=float(lng))
            validate_point(point)
            return LocationResolution(LocationResolutionStatus.RESOLVED, point)
        except (ValueError, TypeError, KeyError):
            return _failure("PROVIDER_RESPONSE_INVALID", technical=True)

    def _area(self, result: Any) -> bool:
        if not isinstance(result, dict) or not isinstance(result.get("address_components"), list):
            raise ValueError
        countries: set[str] = set()
        admins: set[str] = set()
        for component in result["address_components"]:
            if not isinstance(component, dict) or not isinstance(component.get("types"), list):
                raise ValueError
            types = component["types"]
            if "country" in types:
                short = component.get("short_name")
                if not isinstance(short, str):
                    raise ValueError
                countries.add(short.strip().casefold())
            if "administrative_area_level_1" in types:
                for name in ("long_name", "short_name"):
                    value = component.get(name)
                    if not isinstance(value, str):
                        raise ValueError
                    admins.add(value.strip().casefold())
        if not countries or not admins:
            raise ValueError
        return countries == {"in"} and bool(admins.intersection(self._aliases))
