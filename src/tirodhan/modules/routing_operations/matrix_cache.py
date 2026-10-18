"""Bounded exact-input reuse within a finite job; never a shared/production cache service."""

from __future__ import annotations

import time
from collections import OrderedDict

from tirodhan.modules.planning.routing.contracts import RoadMatrix
from tirodhan.modules.planning.routing.osrm import RoadNode

from .snapshot import MatrixSource


class JobMatrixCache:
    def __init__(
        self, source: MatrixSource, *, max_entries: int = 2, ttl_seconds: int = 30
    ) -> None:
        if not 1 <= max_entries <= 4 or not 1 <= ttl_seconds <= 60:
            raise ValueError("invalid finite matrix cache bounds")
        self.source, self.max_entries, self.ttl = source, max_entries, ttl_seconds
        self.entries: OrderedDict[tuple[RoadNode, ...], tuple[float, RoadMatrix]] = OrderedDict()
        self.hits = self.misses = 0

    async def acquire(self, nodes: tuple[RoadNode, ...]) -> RoadMatrix:
        cached = self.entries.get(nodes)
        if cached is not None and time.monotonic() < cached[0]:
            self.hits += 1
            self.entries.move_to_end(nodes)
            return cached[1]
        self.misses += 1
        matrix = await self.source.acquire(nodes)
        self.entries[nodes] = (time.monotonic() + self.ttl, matrix)
        self.entries.move_to_end(nodes)
        while len(self.entries) > self.max_entries:
            self.entries.popitem(last=False)
        return matrix

    @property
    def cache_namespace(self) -> object:
        return getattr(self.source, "cache_namespace", None)

    @property
    def max_nodes(self) -> int:
        return int(getattr(self.source, "max_nodes", 32))
