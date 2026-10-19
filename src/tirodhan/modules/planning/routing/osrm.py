from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
from dataclasses import dataclass
from typing import cast
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx

from .contracts import RoadMatrix
from .execution import RoutingAdapterError, decode_object


@dataclass(frozen=True, slots=True)
class RoadNode:
    node_id: UUID
    longitude: float
    latitude: float


class OSRMMatrixProvider:
    """Explicit graph/profile provenance is operator-attested, not inferred from OSRM."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        endpoint: str,
        graph_version: str,
        profile_version: str,
        max_nodes: int,
        max_snap_distance_m: float,
        timeout_seconds: float = 10,
        max_response_bytes: int = 4_000_000,
        routing_profile: str = "driving",
    ) -> None:
        url = urlsplit(endpoint)
        if (
            url.scheme not in ("http", "https")
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or (url.scheme == "http" and url.hostname not in ("localhost", "127.0.0.1", "::1"))
            or not re.fullmatch(r"[a-zA-Z0-9_-]+", routing_profile)
            or not graph_version
            or not profile_version
            or type(max_nodes) is not int
            or max_nodes < 1
            or not math.isfinite(max_snap_distance_m)
            or max_snap_distance_m < 0
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
            or type(max_response_bytes) is not int
            or max_response_bytes < 1
        ):
            raise RoutingAdapterError("INVALID_MATRIX_CONFIGURATION")
        self.client = client
        self.endpoint = endpoint.rstrip("/")
        self.graph_version, self.profile_version = graph_version, profile_version
        self.max_nodes, self.max_snap = max_nodes, max_snap_distance_m
        self.timeout, self.max_bytes, self.profile = (
            timeout_seconds,
            max_response_bytes,
            routing_profile,
        )

    @property
    def cache_namespace(self) -> tuple[str, str, str, str, float]:
        return (
            self.graph_version,
            self.profile_version,
            self.profile,
            self.endpoint,
            self.max_snap,
        )

    async def acquire(self, nodes: tuple[RoadNode, ...]) -> RoadMatrix:
        if (
            type(nodes) is not tuple
            or not 0 < len(nodes) <= self.max_nodes
            or len({node.node_id for node in nodes}) != len(nodes)
            or any(
                not math.isfinite(node.longitude)
                or not math.isfinite(node.latitude)
                or not -180 <= node.longitude <= 180
                or not -90 <= node.latitude <= 90
                for node in nodes
            )
        ):
            raise RoutingAdapterError("INVALID_MATRIX_NODES")
        # httpx/httpcore diagnostic logging includes full coordinate-bearing URLs.
        # Normal application configure_logging already disables these loggers.
        if logging.getLogger("httpx").isEnabledFor(logging.INFO) or logging.getLogger(
            "httpcore"
        ).isEnabledFor(logging.DEBUG):
            raise RoutingAdapterError("UNSAFE_HTTP_LOGGING")
        try:
            raw = await asyncio.wait_for(self._fetch(nodes), self.timeout)
        except (asyncio.TimeoutError, httpx.TimeoutException):
            raise RoutingAdapterError("MATRIX_TIMEOUT") from None
        except httpx.RequestError:
            raise RoutingAdapterError("MATRIX_UNAVAILABLE") from None
        data = decode_object(raw)
        if data.get("code") != "Ok":
            raise RoutingAdapterError("NO_ROAD_MATRIX")
        if data.get("fallback_speed_cells"):
            raise RoutingAdapterError("ROAD_FALLBACK_REJECTED")
        n = len(nodes)
        try:
            for name in ("sources", "destinations"):
                points = data[name]
                if not isinstance(points, list) or len(points) != n:
                    raise ValueError("waypoints")
                for point in points:
                    distance = point["distance"]
                    if (
                        type(distance) not in (int, float)
                        or not math.isfinite(distance)
                        or not 0 <= distance <= self.max_snap
                    ):
                        raise ValueError("snap")
            times: list[tuple[int | None, ...]] = []
            distances: list[tuple[int | None, ...]] = []
            for name, target in (("durations", times), ("distances", distances)):
                values = data[name]
                if not isinstance(values, list) or len(values) != n:
                    raise ValueError("matrix")
                for i, row in enumerate(values):
                    if not isinstance(row, list) or len(row) != n:
                        raise ValueError("row")
                    rounded = []
                    for value in row:
                        if value is not None and (
                            type(value) not in (int, float)
                            or not math.isfinite(value)
                            or value < 0
                            or value > 2**31 - 1
                        ):
                            raise ValueError("value")
                        rounded.append(None if value is None else math.ceil(value))
                    if rounded[i] != 0:
                        raise ValueError("diagonal")
                    target.append(tuple(rounded))
            if any(
                (times[i][j] is None) != (distances[i][j] is None)
                for i in range(n)
                for j in range(n)
            ):
                raise ValueError("reachability")
        except (KeyError, TypeError, ValueError, IndexError):
            raise RoutingAdapterError("INVALID_ROAD_MATRIX") from None
        identity = json.dumps(
            [
                self.graph_version,
                self.profile_version,
                self.profile,
                [(str(node.node_id), node.longitude, node.latitude) for node in nodes],
                times,
                distances,
            ],
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        return RoadMatrix(
            uuid5(NAMESPACE_URL, "tirodhan:osrm:" + hashlib.sha256(identity).hexdigest()),
            self.graph_version,
            self.profile_version,
            tuple(node.node_id for node in nodes),
            tuple(times),
            tuple(distances),
            snap_tokens=tuple(
                cast(
                    tuple[str, str],
                    tuple(
                        hashlib.sha256(
                            json.dumps(
                                [data[name][i].get("hint"), data[name][i].get("location")],
                                separators=(",", ":"),
                            ).encode()
                        ).hexdigest()
                        for name in ("sources", "destinations")
                    ),
                )
                for i in range(n)
            )
            if all(
                isinstance(p.get("hint"), str)
                and bool(p["hint"])
                and isinstance(p.get("location"), list)
                and len(p["location"]) == 2
                and all(type(v) in (int, float) and math.isfinite(v) for v in p["location"])
                for name in ("sources", "destinations")
                for p in data[name]
            )
            else (),
        )

    async def acquire_star(self, nodes: tuple[RoadNode, ...]) -> RoadMatrix:
        from .osrm_rectangular import acquire_star

        return await acquire_star(self, nodes)

    async def _fetch(
        self,
        nodes: tuple[RoadNode, ...],
        *,
        sources: tuple[int, ...] | None = None,
        destinations: tuple[int, ...] | None = None,
    ) -> bytes:
        coordinates = ";".join(f"{node.longitude},{node.latitude}" for node in nodes)
        url = f"{self.endpoint}/table/v1/{self.profile}/{coordinates}"
        params = {"annotations": "duration,distance"}
        if sources is not None:
            params["sources"] = ";".join(map(str, sources))
        if destinations is not None:
            params["destinations"] = ";".join(map(str, destinations))
        async with self.client.stream(
            "GET",
            url,
            params=params,
            timeout=self.timeout,
            follow_redirects=False,
        ) as response:
            if response.status_code != 200:
                raise RoutingAdapterError("MATRIX_UNAVAILABLE")
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > self.max_bytes:
                    raise RoutingAdapterError("RESPONSE_TOO_LARGE")
                chunks.append(chunk)
        return b"".join(chunks)
