from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import h3
import httpx
import pytest
from pydantic import SecretStr, ValidationError

from tirodhan.core.config import Settings
from tirodhan.core.logging import configure_logging
from tirodhan.modules.customers.service import GeoPoint
from tirodhan.modules.reliability.publisher import RoutedMessage
from tirodhan.modules.reliability.service_bus import (
    AzureServiceabilityDelivery,
    AzureServiceBusPublisher,
)
from tirodhan.modules.serviceability.google_geocoding import (
    GoogleGeocodingConfigurationError,
    GoogleGeocodingLocationResolver,
)
from tirodhan.modules.serviceability.h3_cells import H3CellIdDeriver
from tirodhan.modules.serviceability.ports import (
    LocationResolutionStatus,
    ServiceabilityResolverNotConfiguredError,
)
from tirodhan.modules.serviceability.runtime import serviceability_runtime
from tirodhan.modules.serviceability.service import InvalidServiceabilityInputError

pytestmark = pytest.mark.asyncio


def google_body() -> dict[str, Any]:
    return {
        "status": "OK",
        "results": [
            {
                "address_components": [
                    {"types": ["country"], "long_name": "India", "short_name": "IN"},
                    {
                        "types": ["administrative_area_level_1"],
                        "long_name": "Delhi",
                        "short_name": "DL",
                    },
                ],
                "types": ["street_address"],
                "geometry": {
                    "location_type": "ROOFTOP",
                    "location": {"lat": 28.6139, "lng": 77.209},
                },
                "formatted_address": "not authoritative",
                "address_descriptor": {"landmarks": ["private supplemental descriptor"]},
            }
        ],
    }


async def resolve(body: Any, *, pin: GeoPoint | None = None) -> Any:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    ) as client:
        return await GoogleGeocodingLocationResolver(
            client=client,
            api_key="test-secret",
            timeout_seconds=3,
        ).resolve(address="synthetic household", supplied_location=pin)


@pytest.mark.parametrize("precision", ["ROOFTOP", "RANGE_INTERPOLATED"])
async def test_precise_household_forward_and_descriptor_minimization(precision: str) -> None:
    body = google_body()
    body["results"][0]["geometry"]["location_type"] = precision
    outcome = await resolve(body)
    assert outcome.status == LocationResolutionStatus.RESOLVED
    assert outcome.location == GeoPoint(28.6139, 77.209)
    assert outcome.failure_code is None
    assert not hasattr(outcome, "address_descriptor")


async def test_reverse_preserves_confirmed_pin_not_provider_geometry() -> None:
    pin = GeoPoint(28.61, 77.21)
    body = google_body()
    body["results"].append(copy.deepcopy(body["results"][0]))
    assert (await resolve(body, pin=pin)).location == pin


@pytest.mark.parametrize(
    "alias", [" Delhi ", "dl", "NATIONAL CAPITAL TERRITORY OF DELHI", " NCT of Delhi "]
)
async def test_alias_normalization(alias: str) -> None:
    body = google_body()
    body["results"][0]["address_components"][1].update(long_name=alias, short_name="unmatched")
    assert (await resolve(body)).status == LocationResolutionStatus.RESOLVED


@pytest.mark.parametrize("country,admin", [("US", "Delhi"), ("IN", "Haryana")])
async def test_structured_area_not_formatted_address(country: str, admin: str) -> None:
    body = google_body()
    body["results"][0]["formatted_address"] = "Delhi India household"
    body["results"][0]["address_components"][0]["short_name"] = country
    body["results"][0]["address_components"][1].update(long_name=admin, short_name=admin)
    outcome = await resolve(body)
    assert outcome.status == LocationResolutionStatus.UNSERVICEABLE
    assert outcome.failure_code == "OUTSIDE_SERVICE_AREA"
    assert outcome.location is None


@pytest.mark.parametrize(
    "precision,types,partial",
    [
        ("GEOMETRIC_CENTER", ["street_address"], False),
        ("APPROXIMATE", ["premise"], False),
        ("ROOFTOP", ["locality"], False),
        ("ROOFTOP", ["postal_code"], False),
        ("ROOFTOP", ["route"], False),
        ("ROOFTOP", ["administrative_area_level_1"], False),
        ("ROOFTOP", ["street_address"], True),
    ],
)
async def test_weak_household_results_fail_closed(
    precision: str, types: list[str], partial: bool
) -> None:
    body = google_body()
    body["results"][0].update(types=types, partial_match=partial)
    body["results"][0]["geometry"]["location_type"] = precision
    outcome = await resolve(body)
    assert outcome.status == LocationResolutionStatus.UNSERVICEABLE
    assert outcome.failure_code == "AMBIGUOUS_LOCATION"


async def test_multiple_forward_results_are_ambiguous() -> None:
    body = google_body()
    body["results"] *= 2
    assert (await resolve(body)).failure_code == "AMBIGUOUS_LOCATION"


@pytest.mark.parametrize(
    "body,code",
    [
        ({"status": "ZERO_RESULTS", "results": []}, "NO_LOCATION_MATCH"),
        ({"status": "OVER_QUERY_LIMIT"}, "PROVIDER_RATE_LIMITED"),
        ({"status": "UNKNOWN_ERROR"}, "PROVIDER_UNAVAILABLE"),
        ([], "PROVIDER_RESPONSE_INVALID"),
        ({"status": "OK", "results": [{}]}, "PROVIDER_RESPONSE_INVALID"),
    ],
)
async def test_provider_status_and_shape_failures(body: Any, code: str) -> None:
    assert (await resolve(body)).failure_code == code


async def test_missing_structured_admin_components_is_technical_failure() -> None:
    body = google_body()
    body["results"][0]["address_components"] = []
    outcome = await resolve(body)
    assert outcome.status == LocationResolutionStatus.TECHNICAL_FAILURE
    assert outcome.failure_code == "PROVIDER_RESPONSE_INVALID"


@pytest.mark.parametrize(
    "status,code",
    [
        (429, "PROVIDER_RATE_LIMITED"),
        (503, "PROVIDER_UNAVAILABLE"),
        (400, "PROVIDER_RESPONSE_INVALID"),
    ],
)
async def test_http_failure_no_retry(status: int, code: str) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, text="private provider response")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        outcome = await GoogleGeocodingLocationResolver(
            client=client,
            api_key="secret",
            timeout_seconds=3,
        ).resolve(address="private address", supplied_location=None)
    assert calls == 1
    assert outcome.failure_code == code


@pytest.mark.parametrize("status", [401, 403])
async def test_auth_configuration_error_safe_and_fail_closed(status: int, tmp_path: Path) -> None:
    log_path = tmp_path / "application.jsonl"
    configure_logging("DEBUG", log_path)
    try:
        logging.getLogger("test.serviceability").info("logging_capture_active")
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(status, text="private provider payload")
            )
        ) as client:
            with pytest.raises(GoogleGeocodingConfigurationError) as caught:
                await GoogleGeocodingLocationResolver(
                    client=client,
                    api_key="private-key",
                    timeout_seconds=3,
                ).resolve(address="private-household", supplied_location=GeoPoint(28.61, 77.21))
        combined = log_path.read_text(encoding="utf-8") + str(caught.value)
        assert "logging_capture_active" in combined
    finally:
        for handler in logging.getLogger().handlers:
            handler.close()
        configure_logging("INFO")
    for sensitive in (
        "private-key",
        "private-household",
        "28.61",
        "77.21",
        "private provider payload",
        "maps.googleapis.com",
    ):
        assert sensitive not in combined


@pytest.mark.parametrize(
    "kind,code",
    [
        ("timeout", "PROVIDER_TIMEOUT"),
        ("network", "PROVIDER_UNAVAILABLE"),
        ("json", "PROVIDER_RESPONSE_INVALID"),
    ],
)
async def test_transport_and_malformed_json_safe(kind: str, code: str) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if kind == "timeout":
            raise httpx.ReadTimeout("private URL", request=request)
        if kind == "network":
            raise httpx.ConnectError("private URL", request=request)
        return httpx.Response(200, content=b"not JSON")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await GoogleGeocodingLocationResolver(
            client=client, api_key="secret", timeout_seconds=3
        ).resolve(address="household", supplied_location=None)
    assert calls == 1
    assert result.failure_code == code


async def test_forward_request_india_restriction_and_reverse_request() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=google_body())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = GoogleGeocodingLocationResolver(
            client=client, api_key="secret", timeout_seconds=3
        )
        await adapter.resolve(address="household", supplied_location=None)
        await adapter.resolve(address="household", supplied_location=GeoPoint(28.6, 77.2))
    assert requests[0].url.params["components"] == "country:IN"
    assert requests[0].url.params["region"] == "in"
    assert requests[1].url.params["latlng"] == "28.6,77.2"
    assert "address" not in requests[1].url.params


async def test_h3_constant_canonical_deterministic_and_separated() -> None:
    deriver = H3CellIdDeriver()
    first = await deriver.derive(GeoPoint(28.6139, 77.209))
    assert first == await deriver.derive(GeoPoint(28.6139, 77.209))
    assert h3.is_valid_cell(first)
    assert h3.get_resolution(first) == 7
    assert first == h3.latlng_to_cell(28.6139, 77.209, 7)
    assert first != await deriver.derive(GeoPoint(28.728, 77.12))


@pytest.mark.parametrize(
    "point",
    [GeoPoint(91, 0), GeoPoint(0, 181), GeoPoint(float("nan"), 0), GeoPoint(0, float("inf"))],
)
async def test_h3_invalid_coordinates_fail_closed(point: GeoPoint) -> None:
    with pytest.raises(InvalidServiceabilityInputError):
        await H3CellIdDeriver().derive(point)


async def test_injected_google_client_caller_owned_and_missing_config_fails_closed() -> None:
    settings = Settings(
        _env_file=None, google_maps_api_key=SecretStr("test"), google_maps_http_timeout_seconds=3
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=google_body()))
    ) as client:
        async with serviceability_runtime(settings, client=client) as runtime:
            assert isinstance(runtime.resolver, GoogleGeocodingLocationResolver)
        assert not client.is_closed
    async with serviceability_runtime(Settings(_env_file=None)) as runtime:
        with pytest.raises(ServiceabilityResolverNotConfiguredError):
            await runtime.resolver.resolve(address="household", supplied_location=None)


@pytest.mark.parametrize("timeout", [0, -1, 61, float("nan"), float("inf")])
async def test_timeout_configuration_bounded(timeout: float) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, google_maps_http_timeout_seconds=timeout)


async def test_owned_client_reused_closed_and_transport_has_no_retries(monkeypatch: Any) -> None:
    real_client = httpx.AsyncClient
    real_transport = httpx.AsyncHTTPTransport
    clients: list[httpx.AsyncClient] = []
    transport_retries: list[int] = []

    def make_transport(*, retries: int) -> httpx.AsyncHTTPTransport:
        transport_retries.append(retries)
        return real_transport(retries=retries)

    def make_client(**kwargs: Any) -> httpx.AsyncClient:
        # Inspect production transport configuration, replace only network access.
        client = real_client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=google_body()))
        )
        clients.append(client)
        return client

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", make_transport)
    monkeypatch.setattr(httpx, "AsyncClient", make_client)
    async with serviceability_runtime(
        Settings(_env_file=None, google_maps_api_key="test", google_maps_http_timeout_seconds=3)
    ) as runtime:
        await runtime.resolver.resolve(address="synthetic household", supplied_location=None)
        await runtime.resolver.resolve(address="synthetic household", supplied_location=None)
        assert len(clients) == 1
        assert not clients[0].is_closed
    assert clients[0].is_closed
    assert transport_retries == [0]


async def test_azure_envelope_identifier_only_and_settlement_adapter() -> None:
    sender = MagicMock()
    sender.__aenter__ = AsyncMock(return_value=sender)
    sender.__aexit__ = AsyncMock(return_value=None)
    sender.send_messages = AsyncMock()
    client = MagicMock()
    client.get_queue_sender.return_value = sender
    routed = RoutedMessage(
        "transport-id", "ServiceabilityRequested", b'{"serviceability_context_id":"internal-id"}'
    )
    await AzureServiceBusPublisher(client, timeout_seconds=3).send("serviceability", routed)
    client.get_queue_sender.assert_called_once_with(queue_name="serviceability")
    envelope = sender.send_messages.call_args.args[0]
    assert envelope.message_id == routed.message_id
    assert envelope.subject == routed.message_type
    assert b"".join(envelope.body) == routed.body
    assert not envelope.application_properties
    assert sender.send_messages.call_args.kwargs == {"timeout": 3}

    receiver = MagicMock()
    receiver.complete_message = AsyncMock()
    receiver.abandon_message = AsyncMock()
    receiver.dead_letter_message = AsyncMock()
    received = MagicMock(message_id=routed.message_id, subject=routed.message_type)
    received.body = [routed.body]
    delivery = AzureServiceabilityDelivery(receiver, received)
    assert delivery.body == routed.body
    await delivery.complete()
    await delivery.abandon()
    await delivery.dead_letter()
    receiver.complete_message.assert_awaited_once_with(received)
    receiver.abandon_message.assert_awaited_once_with(received)
    assert (
        receiver.dead_letter_message.call_args.kwargs["reason"] == "INVALID_SERVICEABILITY_MESSAGE"
    )
    received.body = [b"x" * 10000]
    assert len(AzureServiceabilityDelivery(receiver, received).body) == 513
