import asyncio
import logging
from dataclasses import replace

import httpx
import pytest

from tirodhan.modules.planning.routing.execution import RoutingAdapterError
from tirodhan.modules.planning.routing.osrm import OSRMMatrixProvider, RoadNode


def response():
    return dict(
        code="Ok",
        durations=[[0, 1.1], [9.2, 0]],
        distances=[[0, 7.1], [15, 0]],
        sources=[dict(distance=1), dict(distance=2)],
        destinations=[dict(distance=1), dict(distance=2)],
    )


def nodes():
    from uuid import UUID

    return (RoadNode(UUID(int=1), 77.20, 28.62), RoadNode(UUID(int=2), 77.21, 28.63))


def provider(client, **changes):
    config = dict(
        client=client,
        endpoint="http://localhost:5000",
        graph_version="graph-sha",
        profile_version="car-sha",
        max_nodes=10,
        max_snap_distance_m=50,
    )
    return OSRMMatrixProvider(**dict(config, **changes))


@pytest.mark.asyncio
async def test_directed_road_matrix_rounds_up_and_replay_retains_identity():
    calls = []

    def handle(request):
        calls.append(request)
        assert request.url.params["annotations"] == "duration,distance"
        assert "fallback_speed" not in request.url.params
        return httpx.Response(200, json=response())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        matrix = await provider(client).acquire(nodes())
        assert matrix.travel_seconds == ((0, 2), (10, 0))
        assert matrix.distance_m == ((0, 8), (15, 0))
        assert await provider(client).acquire(nodes()) == matrix
        changed = await provider(client, graph_version="new-graph").acquire(nodes())
        assert changed.matrix_id != matrix.matrix_id
    assert len(calls) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["null", "negative", "negative_distance", "nan", "snap", "shape", "fallback", "mask"]
)
async def test_matrix_never_fabricates_missing_roads(case):
    data = response()
    if case == "null":
        data["durations"][0][1] = data["distances"][0][1] = None
    elif case == "negative":
        data["durations"][0][1] = -1
    elif case == "negative_distance":
        data["durations"][0][1] = 0
        data["distances"][0][1] = -0.1
    elif case == "nan":
        data["durations"][0][1] = "NaN"
    elif case == "snap":
        data["sources"][0]["distance"] = 500
    elif case == "shape":
        data["durations"] = [[0]]
    elif case == "fallback":
        data["fallback_speed_cells"] = [[0, 1]]
    else:
        data["durations"][0][1] = None
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=data))
    ) as client:
        if case == "null":
            matrix = await provider(client).acquire(nodes())
            assert matrix.travel_seconds[0][1] is None
        else:
            with pytest.raises(RoutingAdapterError):
                await provider(client).acquire(nodes())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["timeout", "status", "malformed", "large", "duplicate", "negative_distance"]
)
async def test_read_failure_is_bounded_private_and_retryable(case):
    count = 0

    async def handle(request):
        nonlocal count
        count += 1
        if count > 1:
            return httpx.Response(200, json=response())
        if case == "timeout":
            raise httpx.ReadTimeout("SECRET_COORDINATES", request=request)
        if case == "status":
            return httpx.Response(503, text="SECRET_COORDINATES")
        if case == "large":
            return httpx.Response(200, content=b"x" * 10001)
        if case == "duplicate":
            return httpx.Response(200, content=b'{"code":"Ok","code":"SECRET_COORDINATES"}')
        if case == "negative_distance":
            invalid = response()
            invalid["durations"][0][1] = 0
            invalid["distances"][0][1] = -0.1
            return httpx.Response(200, json=invalid)
        return httpx.Response(200, content=b"SECRET_COORDINATES")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        gateway = provider(client, max_response_bytes=10000)
        with pytest.raises(RoutingAdapterError) as error:
            await gateway.acquire(nodes())
        assert "SECRET" not in str(error.value)
        assert count == 1  # No implicit retry.
        assert (await gateway.acquire(nodes())).travel_seconds[0][1] == 2


@pytest.mark.asyncio
async def test_verbose_http_logging_fails_before_coordinates_leave(caplog, monkeypatch):
    # Alembic fileConfig can disable pre-existing loggers earlier in the full suite.
    monkeypatch.setattr(logging.getLogger("httpx"), "disabled", False)
    caplog.set_level(logging.DEBUG, logger="httpx")
    assert logging.getLogger("httpx").isEnabledFor(logging.INFO)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("must not send"))
    ) as client:
        with pytest.raises(RoutingAdapterError, match="UNSAFE_HTTP_LOGGING"):
            await provider(client).acquire(nodes())
    assert "77.20" not in caplog.text


@pytest.mark.asyncio
async def test_whole_matrix_deadline_covers_slow_stream():
    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(10)
            yield b"never"

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=Slow()))
    ) as client:
        with pytest.raises(RoutingAdapterError, match="MATRIX_TIMEOUT"):
            await provider(client, timeout_seconds=0.02).acquire(nodes())


@pytest.mark.asyncio
async def test_invalid_nodes_are_rejected_before_http():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("must not send"))
    ) as client:
        for values in (nodes() * 2, (replace(nodes()[0], longitude=float("inf")),)):
            with pytest.raises(RoutingAdapterError, match="INVALID_MATRIX_NODES"):
                await provider(client).acquire(values)


def test_nonlocal_plaintext_endpoint_and_credentials_are_rejected():
    for endpoint in (
        "http://routing.example",
        "https://secret@routing.example",
        "https://routing.example?key=secret",
    ):
        with pytest.raises(RoutingAdapterError, match="INVALID_MATRIX_CONFIGURATION"):
            provider(None, endpoint=endpoint)
