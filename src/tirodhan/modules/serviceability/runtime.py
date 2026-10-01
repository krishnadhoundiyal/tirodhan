from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx

from tirodhan.core.config import Settings
from tirodhan.modules.customers.address_protection import AesGcmAddressProtector
from tirodhan.modules.customers.ports import AddressProtector, UnconfiguredAddressProtector
from tirodhan.modules.serviceability.google_geocoding import GoogleGeocodingLocationResolver
from tirodhan.modules.serviceability.h3_cells import H3CellIdDeriver
from tirodhan.modules.serviceability.ports import LocationResolver, UnconfiguredLocationResolver


def address_protector_from_settings(settings: Settings) -> AddressProtector:
    if (
        settings.address_encryption_active_key_id is None
        or settings.address_encryption_keys is None
    ):
        return UnconfiguredAddressProtector()
    return AesGcmAddressProtector.from_configuration(
        active_key_id=settings.address_encryption_active_key_id,
        encryption_keys_json=settings.address_encryption_keys.get_secret_value(),
    )


@dataclass(frozen=True)
class ServiceabilityRuntime:
    protector: AddressProtector
    resolver: LocationResolver
    cells: H3CellIdDeriver


@asynccontextmanager
async def serviceability_runtime(
    settings: Settings,
    *,
    client: httpx.AsyncClient | None = None,
    resolver: LocationResolver | None = None,
    protector: AddressProtector | None = None,
) -> AsyncIterator[ServiceabilityRuntime]:
    configured_protector = protector or address_protector_from_settings(settings)
    owns_client = False
    if resolver is None:
        if (
            settings.google_maps_api_key is None
            or settings.google_maps_http_timeout_seconds is None
        ):
            resolver = UnconfiguredLocationResolver()
        else:
            if client is None:
                client = httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(retries=0))
                owns_client = True
            try:
                resolver = GoogleGeocodingLocationResolver(
                    client=client,
                    api_key=settings.google_maps_api_key.get_secret_value(),
                    timeout_seconds=settings.google_maps_http_timeout_seconds,
                    delhi_admin_aliases=settings.google_maps_delhi_admin_aliases,
                )
            except BaseException:
                if owns_client:
                    await client.aclose()
                raise
    try:
        yield ServiceabilityRuntime(configured_protector, resolver, H3CellIdDeriver())
    finally:
        if owns_client and client is not None:
            await client.aclose()
