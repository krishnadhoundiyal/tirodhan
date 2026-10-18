"""Finite directed road memoization; square queries retain strict OSRM safeguards.

Only requested cells are retained. A new node needs O(n b) provider cells for
fixed block width b, not a dense regional matrix. No coordinate quantization.
"""

from __future__ import annotations

import asyncio
import hashlib
import heapq
import struct
from collections import OrderedDict
from uuid import NAMESPACE_URL, UUID, uuid5

from tirodhan.modules.planning.routing.contracts import RoadArc, RoadMatrix
from tirodhan.modules.planning.routing.execution import RoutingAdapterError
from tirodhan.modules.planning.routing.osrm import RoadNode
from tirodhan.modules.planning.routing.validation import RoutingValidationError, _validate_matrix

from .snapshot import MatrixSource

ARC = struct.Struct(">qq")
Location = tuple[float, float]
ArcKey = tuple[Location, Location]


class DirectedRoadCache:
    def __init__(
        self,
        source: MatrixSource,
        *,
        max_bytes: int = 32_000_000,
        block_nodes: int = 32,
    ):
        if max_bytes < 1024 or block_nodes < 2:
            raise ValueError("INVALID_ROAD_CACHE_BUDGET")
        self.source, self.max_bytes = source, max_bytes
        self.block_nodes = min(block_nodes, int(getattr(source, "max_nodes", block_nodes)))
        self.entries: OrderedDict[ArcKey, bytes] = OrderedDict()
        self.snaps: OrderedDict[Location, tuple[str, str]] = OrderedDict()
        self.namespace: tuple[str, str] | None = None
        self.source_namespace = getattr(source, "cache_namespace", None)
        self.lock = asyncio.Lock()
        self.hits = self.queries = self.provider_cells = 0
        self.bytes = 0

    async def acquire(self, nodes: tuple[RoadNode, ...]) -> RoadMatrix:
        # Compatibility for native *local* dense projections. Refuse allocation,
        # not a booking, if the requested model cannot fit this process budget.
        if len(nodes) ** 2 * 80 > self.max_bytes:
            raise RoutingAdapterError("UNKNOWN_MODEL_MEMORY_BUDGET")
        sparse = await self.acquire_arcs(
            nodes, {(a.node_id, b.node_id) for a in nodes for b in nodes}
        )
        index = {n.node_id: i for i, n in enumerate(nodes)}
        values = [[sparse.arc(a.node_id, b.node_id, index) for b in nodes] for a in nodes]
        return RoadMatrix(
            sparse.matrix_id,
            sparse.graph_version,
            sparse.profile_version,
            sparse.node_ids,
            tuple(tuple(t for t, _ in row) for row in values),
            tuple(tuple(d for _, d in row) for row in values),
        )

    async def acquire_arcs(
        self, nodes: tuple[RoadNode, ...], pairs: set[tuple[UUID, UUID]]
    ) -> RoadMatrix:
        if len(pairs) * 512 + len(nodes) * 1024 > 128_000_000:
            raise RoutingAdapterError("UNKNOWN_MATRIX_MEMORY_BUDGET")
        by_id = {n.node_id: n for n in nodes}
        if len(by_id) != len(nodes) or any(a not in by_id or b not in by_id for a, b in pairs):
            raise RoutingAdapterError("INVALID_MATRIX_NODES")
        coords = {n.node_id: (n.longitude, n.latitude) for n in nodes}
        locations = {coords[n.node_id]: n for n in nodes}
        required = {(coords[a], coords[b]) for a, b in pairs}
        result: dict[ArcKey, tuple[int | None, int | None]] = {}
        # One acquisition at a time coalesces overlapping work. No business lock.
        async with self.lock:
            configured = getattr(self.source, "cache_namespace", None)
            if configured != self.source_namespace:
                self.entries.clear()
                self.snaps.clear()
                self.bytes = 0
                self.namespace = None
                self.source_namespace = configured
                raise RoutingAdapterError("MATRIX_PROVENANCE_CHANGED")
            missing: dict[Location, list[Location]] = {}
            for a, b in sorted(required):
                if (a, b) in self.entries and a in self.snaps and b in self.snaps:
                    value = ARC.unpack(self.entries[(a, b)])
                    result[(a, b)] = (
                        None if value[0] == -1 else value[0],
                        None if value[1] == -1 else value[1],
                    )
                    self.entries.move_to_end((a, b))
                    self.hits += 1
                else:
                    missing.setdefault(a, []).append(b)
            pending = {(a, b) for a, targets in missing.items() for b in targets}
            adjacent: dict[Location, set[Location]] = {}
            for a, b in pending:
                adjacent.setdefault(a, set()).add(b)
                adjacent.setdefault(b, set()).add(a)
            heap = [(-len(v), k) for k, v in adjacent.items()]
            neighbours = {key: list(values) for key, values in adjacent.items()}
            for values in neighbours.values():
                heapq.heapify(values)
            heapq.heapify(heap)
            while pending:
                degree, hub = heapq.heappop(heap)
                if -degree != len(adjacent[hub]) or not adjacent[hub]:
                    continue
                selected: list[Location] = []
                while neighbours[hub] and len(selected) < self.block_nodes - 1:
                    key = heapq.heappop(neighbours[hub])
                    if key in adjacent[hub]:
                        selected.append(key)
                keys = list(dict.fromkeys([hub] + selected))
                query = tuple(locations[k] for k in keys)
                if len(query) == 1:
                    node = query[0]
                    query += (
                        RoadNode(
                            uuid5(NAMESPACE_URL, f"{node.node_id}:probe"),
                            node.longitude,
                            node.latitude,
                        ),
                    )
                matrix = await self.source.acquire(query)
                if matrix.node_ids != tuple(n.node_id for n in query):
                    raise RoutingAdapterError("MATRIX_PROVENANCE_CHANGED")
                try:
                    _validate_matrix(matrix, len(query))
                except RoutingValidationError:
                    raise RoutingAdapterError("INVALID_ROAD_MATRIX") from None
                namespace = (matrix.graph_version, matrix.profile_version)
                if self.namespace is not None and namespace != self.namespace:
                    self.entries.clear()
                    self.snaps.clear()
                    self.bytes = 0
                    self.namespace = namespace
                    raise RoutingAdapterError("MATRIX_PROVENANCE_CHANGED")
                self.namespace = namespace
                if (
                    isinstance(configured, tuple)
                    and len(configured) >= 2
                    and tuple(configured[:2]) != namespace
                ):
                    raise RoutingAdapterError("MATRIX_PROVENANCE_CHANGED")
                reusable = self.source_namespace is not None and len(matrix.snap_tokens) == len(
                    query
                )
                if reusable:
                    for i, key in enumerate(keys):
                        if key in self.snaps and self.snaps[key] != matrix.snap_tokens[i]:
                            self.entries.clear()
                            self.snaps.clear()
                            self.bytes = 0
                            self.namespace = None
                            raise RoutingAdapterError("MATRIX_SNAPPING_CHANGED")
                    for i, key in enumerate(keys):
                        self.snaps[key] = matrix.snap_tokens[i]
                self.queries += 1
                self.provider_cells += len(query) ** 2
                for i, origin in enumerate(keys):
                    for j, destination in enumerate(keys):
                        arc_key = (origin, destination)
                        if arc_key not in pending:
                            continue
                        pending.remove(arc_key)
                        if (destination, origin) not in pending:
                            adjacent[origin].discard(destination)
                            adjacent[destination].discard(origin)
                            heapq.heappush(heap, (-len(adjacent[origin]), origin))
                            heapq.heappush(heap, (-len(adjacent[destination]), destination))
                        value = (matrix.travel_seconds[i][j], matrix.distance_m[i][j])
                        result[arc_key] = value
                        if reusable and all(v is None or v < 2**63 for v in value):
                            packed = ARC.pack(*(v if v is not None else -1 for v in value))
                            if arc_key in self.entries:
                                self.bytes -= len(self.entries.pop(arc_key)) + 384
                            self.entries[arc_key] = packed
                            self.bytes += len(packed) + 384
                            while self.entries and self.bytes > self.max_bytes // 2:
                                _, removed = self.entries.popitem(last=False)
                                self.bytes -= len(removed) + 384
                while len(self.snaps) * 512 > self.max_bytes // 2:
                    self.snaps.popitem(last=False)
            if self.namespace is None:
                raise RoutingAdapterError("EMPTY_ROAD_ACQUISITION")
            arcs = tuple(
                RoadArc(a, b, *result[(coords[a], coords[b])])
                for a, b in sorted(pairs, key=lambda pair: (pair[0].int, pair[1].int))
            )
            identity = hashlib.sha256(repr((self.namespace, arcs)).encode()).hexdigest()
            return RoadMatrix(
                uuid5(NAMESPACE_URL, identity),
                *self.namespace,
                tuple(n.node_id for n in nodes),
                (),
                (),
                arcs,
            )
