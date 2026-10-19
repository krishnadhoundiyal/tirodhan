"""OSRM Table stars: two directed rectangles, without synthetic road values."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
from typing import TYPE_CHECKING
from uuid import NAMESPACE_URL, uuid5

import httpx

from .contracts import RoadArc, RoadMatrix
from .execution import RoutingAdapterError, decode_object

if TYPE_CHECKING:
    from .osrm import OSRMMatrixProvider, RoadNode


async def acquire_star(provider: OSRMMatrixProvider, nodes: tuple[RoadNode, ...]) -> RoadMatrix:
    if (
        type(nodes) is not tuple
        or not 1 < len(nodes) <= provider.max_nodes
        or len({n.node_id for n in nodes}) != len(nodes)
        or any(
            not math.isfinite(n.longitude)
            or not math.isfinite(n.latitude)
            or not -180 <= n.longitude <= 180
            or not -90 <= n.latitude <= 90
            for n in nodes
        )
    ):
        raise RoutingAdapterError("INVALID_MATRIX_NODES")
    if logging.getLogger("httpx").isEnabledFor(logging.INFO) or logging.getLogger(
        "httpcore"
    ).isEnabledFor(logging.DEBUG):
        raise RoutingAdapterError("UNSAFE_HTTP_LOGGING")
    all_indices = tuple(range(len(nodes)))
    values: dict[tuple[int, int], tuple[int | None, int | None]] = {}
    snaps: dict[tuple[str, int], str] = {}
    complete_snaps = True
    for sources, destinations in (((0,), all_indices), (all_indices, (0,))):
        try:
            raw = await asyncio.wait_for(
                provider._fetch(nodes, sources=sources, destinations=destinations), provider.timeout
            )
        except (asyncio.TimeoutError, httpx.TimeoutException):
            raise RoutingAdapterError("MATRIX_TIMEOUT") from None
        except httpx.RequestError:
            raise RoutingAdapterError("MATRIX_UNAVAILABLE") from None
        data = decode_object(raw)
        if data.get("code") != "Ok":
            raise RoutingAdapterError("NO_ROAD_MATRIX")
        if data.get("fallback_speed_cells"):
            raise RoutingAdapterError("ROAD_FALLBACK_REJECTED")
        try:
            for name, indices in (("sources", sources), ("destinations", destinations)):
                points = data[name]
                if not isinstance(points, list) or len(points) != len(indices):
                    raise ValueError()
                for index, point in zip(indices, points, strict=True):
                    distance = point["distance"]
                    if (
                        type(distance) not in (int, float)
                        or not math.isfinite(distance)
                        or not 0 <= distance <= provider.max_snap
                    ):
                        raise ValueError()
                    location, hint = point.get("location"), point.get("hint")
                    if not (
                        isinstance(hint, str)
                        and hint
                        and isinstance(location, list)
                        and len(location) == 2
                        and all(type(v) in (int, float) and math.isfinite(v) for v in location)
                    ):
                        complete_snaps = False
                    token = hashlib.sha256(
                        json.dumps([hint, location], separators=(",", ":")).encode()
                    ).hexdigest()
                    key = (name, index)
                    if key in snaps and snaps[key] != token:
                        raise RoutingAdapterError("MATRIX_SNAPPING_CHANGED")
                    snaps[key] = token
            tables = [data[name] for name in ("durations", "distances")]
            if any(not isinstance(t, list) or len(t) != len(sources) for t in tables):
                raise ValueError()
            for i, origin in enumerate(sources):
                if any(
                    not isinstance(t[i], list) or len(t[i]) != len(destinations) for t in tables
                ):
                    raise ValueError()
                for j, destination in enumerate(destinations):
                    pair = tuple(t[i][j] for t in tables)
                    if any(
                        v is not None
                        and (
                            type(v) not in (int, float)
                            or not math.isfinite(v)
                            or not 0 <= v <= 2**31 - 1
                        )
                        for v in pair
                    ):
                        raise ValueError()
                    if (pair[0] is None) != (pair[1] is None):
                        raise ValueError()
                    if origin == destination and pair != (0, 0):
                        raise ValueError()
                    rounded = (
                        None if pair[0] is None else math.ceil(pair[0]),
                        None if pair[1] is None else math.ceil(pair[1]),
                    )
                    key_pair = (origin, destination)
                    if key_pair in values and values[key_pair] != rounded:
                        raise RoutingAdapterError("MATRIX_PROVENANCE_CHANGED")
                    values[key_pair] = rounded
        except (KeyError, TypeError, ValueError, IndexError):
            raise RoutingAdapterError("INVALID_ROAD_MATRIX") from None
    arcs = tuple(
        sorted(
            (RoadArc(nodes[a].node_id, nodes[b].node_id, *v) for (a, b), v in values.items()),
            key=lambda arc: (arc.origin.int, arc.destination.int),
        )
    )
    identity = repr((provider.cache_namespace, nodes, arcs, snaps))
    return RoadMatrix(
        uuid5(NAMESPACE_URL, identity),
        provider.graph_version,
        provider.profile_version,
        tuple(n.node_id for n in nodes),
        (),
        (),
        arcs,
        tuple((snaps[("sources", i)], snaps[("destinations", i)]) for i in all_indices)
        if complete_snaps
        else (),
    )
