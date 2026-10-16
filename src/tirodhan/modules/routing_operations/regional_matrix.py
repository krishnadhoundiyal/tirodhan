"""Per-execution directional block cache; no service, disk cache or household merging."""

from __future__ import annotations

from collections import OrderedDict
from uuid import NAMESPACE_URL, uuid5

from tirodhan.modules.planning.routing.contracts import RoadMatrix
from tirodhan.modules.planning.routing.execution import RoutingAdapterError
from tirodhan.modules.planning.routing.osrm import RoadNode

from .snapshot import MatrixSource


class RegionalMatrixCache:
    def __init__(
        self,
        source: MatrixSource,
        *,
        profile: str,
        graph: str,
        block_nodes: int,
        max_pairs: int,
        strategy: str = "zonal",
    ):
        if (
            not 2 <= block_nodes <= 512
            or max_pairs < block_nodes**2
            or strategy not in ("global", "zonal", "on_demand")
        ):
            raise ValueError("MATRIX_CACHE_CONFIGURATION")
        self.source, self.profile, self.graph = source, profile, graph
        self.block_nodes, self.max_pairs, self.strategy = block_nodes, max_pairs, strategy
        self.entries: OrderedDict[
            tuple[tuple[float, float], tuple[float, float]], tuple[int | None, int | None]
        ] = OrderedDict()
        self.queries = self.hits = self.misses = 0

    async def acquire(self, nodes: tuple[RoadNode, ...]) -> RoadMatrix:
        locations = {(n.longitude, n.latitude): n for n in nodes}
        keys = list(locations)
        if len(keys) ** 2 > self.max_pairs:
            raise RoutingAdapterError("MATRIX_MEMORY_BUDGET")
        width = (
            1
            if self.strategy == "on_demand"
            else (
                len(keys)
                if self.strategy == "global" and len(keys) <= self.block_nodes
                else self.block_nodes // 2
            )
        )
        for i in range(0, len(keys), width):
            for j in range(0, len(keys), width):
                origins, destinations = keys[i : i + width], keys[j : j + width]
                missing = [
                    (a, b) for a in origins for b in destinations if (a, b) not in self.entries
                ]
                for a in origins:
                    for b in destinations:
                        if (a, b) in self.entries:
                            self.entries.move_to_end((a, b))
                self.hits += len(origins) * len(destinations) - len(missing)
                if not missing:
                    continue
                union = list(dict.fromkeys(origins + destinations))
                # A global request is used only if the provider supports its dimension.
                if len(union) > self.block_nodes:
                    union = list(dict.fromkeys([a for a, _ in missing] + [b for _, b in missing]))
                if len(union) > 512:
                    raise RoutingAdapterError("MATRIX_BLOCK_LIMIT")
                query_nodes = tuple(locations[k] for k in union)
                if len(query_nodes) == 1:
                    # OSRM requires two coordinates even for a one-location table.
                    # Use a distinct probe ID at the same coordinate; validate snapping
                    # and the returned diagonal rather than inventing road values.
                    node = query_nodes[0]
                    query_nodes += (
                        RoadNode(
                            uuid5(NAMESPACE_URL, f"{node.node_id}:diagonal"),
                            node.longitude,
                            node.latitude,
                        ),
                    )
                block = await self.source.acquire(query_nodes)
                if block.graph_version != self.graph or block.profile_version != self.profile:
                    raise RoutingAdapterError("MATRIX_PROVENANCE_CHANGED")
                self.queries += 1
                self.misses += len(missing)
                indices = {k: n for n, k in enumerate(union)}
                for a in union:
                    for b in union:
                        ia, ib = indices[a], indices[b]
                        self.entries[(a, b)] = (
                            block.travel_seconds[ia][ib],
                            block.distance_m[ia][ib],
                        )
                while len(self.entries) > self.max_pairs:
                    self.entries.popitem(last=False)
        location_identity = tuple((n.node_id, n.longitude, n.latitude) for n in nodes)
        identity = f"{self.graph}:{self.profile}:{location_identity}"
        coordinates = [(n.longitude, n.latitude) for n in nodes]
        return RoadMatrix(
            uuid5(NAMESPACE_URL, identity),
            self.graph,
            self.profile,
            tuple(n.node_id for n in nodes),
            tuple(tuple(self.entries[(a, b)][0] for b in coordinates) for a in coordinates),
            tuple(tuple(self.entries[(a, b)][1] for b in coordinates) for a in coordinates),
        )
