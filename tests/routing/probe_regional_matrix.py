"""Public-fixture OSRM response diagnostic; no customer data or database access.

Run inside the pinned local image with a preprocessed fixture graph argument.
It reports raw invalid-cell counts and the strict provider result; it never repairs cells.
"""

import argparse
import asyncio
import base64
import json
import struct
from pathlib import Path
from uuid import UUID

import httpx

from tirodhan.core.logging import configure_logging
from tirodhan.modules.planning.routing.execution import RoutingAdapterError
from tirodhan.modules.planning.routing.osrm import OSRMMatrixProvider, RoadNode
from tirodhan.workers.routing_regional import LocalRoadProfile, road_processes


def diagnostic_nodes() -> tuple[RoadNode, ...]:
    anchors = [(77.207, 28.632), (77.209, 28.632), (77.2105, 28.6305), (77.211, 28.630)]
    nodes = []
    for i in range(64):
        x, y = anchors[i % 4]
        offset = (i // 4) % 16
        nodes.append(
            RoadNode(
                UUID(int=i + 1),
                x + ((offset % 4) - 1.5) * 0.00002,
                y + ((offset // 4) - 1.5) * 0.00002,
            )
        )
    return tuple(nodes)


def phantom(hint: str) -> tuple:
    # Diagnostic only: pinned OSRM 6.0 x86_64 PhantomNode layout, NOT a provider API.
    # Segment IDs, integer weights/offsets, float distances/offsets, durations/offsets.
    return struct.unpack("<IIiiiiffffiiii", base64.urlsafe_b64decode(hint)[:56])


async def probe(graph: Path) -> dict:
    nodes = diagnostic_nodes()
    profile = LocalRoadProfile(
        profile_version="probe-car",
        graph_version="fixture-car",
        graph_path=graph,
        port=5027,
        threads=1,
        max_snap_distance_m=300,
        block_nodes=128,
        max_pairs=65536,
    )
    with road_processes([profile]):
        async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
            endpoint = "http://127.0.0.1:5027"
            provider = OSRMMatrixProvider(
                client=client,
                endpoint=endpoint,
                graph_version=profile.graph_version,
                profile_version=profile.profile_version,
                max_nodes=128,
                max_snap_distance_m=300,
            )
            try:
                await provider.acquire(tuple(nodes))
                result = "VALIDATED"
            except RoutingAdapterError as error:
                result = error.code
            response = await client.get(
                endpoint
                + "/table/v1/driving/"
                + ";".join(f"{n.longitude},{n.latitude}" for n in nodes),
                params={"annotations": "duration,distance"},
            )
            response.raise_for_status()
            data = response.json()
            explained = 0
            for i, row in enumerate(data["distances"]):
                for j, value in enumerate(row):
                    if value is None or value >= 0:
                        continue
                    a = phantom(data["sources"][i]["hint"])
                    b = phantom(data["destinations"][j]["hint"])
                    # Zero integer weight lets a downstream -> upstream direct path
                    # survive the >= 0 weight test, despite negative float distance.
                    explained += any(
                        a[d] == b[d]
                        and a[d] & (1 << 31)
                        and a[2 + d] + a[4 + d] == b[2 + d] + b[4 + d]
                        and b[6 + d] + b[8 + d] < a[6 + d] + a[8 + d]
                        for d in (0, 1)
                    )
            # Public fixture pair on a one-way edge. Route's positive geometry
            # distance alone is not a verified correction for the zero-weight path.
            route = await client.get(
                endpoint
                + "/route/v1/driving/"
                + ";".join(f"{nodes[k].longitude},{nodes[k].latitude}" for k in (4, 20)),
                params={"overview": "false"},
            )
            route.raise_for_status()
            route_data = route.json()["routes"][0]
    return dict(
        strict_provider=result,
        code=data["code"],
        nodes=len(nodes),
        max_snap_m=max(point["distance"] for point in data["sources"]),
        negative_cells={
            key: sum(value is not None and value < 0 for row in data[key] for value in row)
            for key in ("durations", "distances")
        },
        invalid_diagonals={
            key: sum(row[i] != 0 for i, row in enumerate(data[key]))
            for key in ("durations", "distances")
        },
        quantized_same_edge_negative_cells=explained,
        one_way_example=dict(
            source_index=4,
            target_index=20,
            table_distance=data["distances"][4][20],
            table_duration=data["durations"][4][20],
            route={key: route_data[key] for key in ("distance", "duration", "weight")},
        ),
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("graph", type=Path)
    args = parser.parse_args()
    configure_logging("ERROR")
    print(json.dumps(asyncio.run(probe(args.graph))))
