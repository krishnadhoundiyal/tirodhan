"""Reproducible public OSM road vertices; requires pinned osmium==4.2.0 only to regenerate."""

import hashlib
import json
import sys
from pathlib import Path

import osmium

BOXES = {
    "West": (77.04, 28.61, 77.14, 28.69),
    "North": (77.17, 28.72, 77.23, 28.78),
    "South": (77.17, 28.48, 77.27, 28.56),
    "East": (77.28, 28.60, 77.33, 28.66),
}


def spread(points, count):
    selected = [points[0]]
    remaining = dict.fromkeys(points[1:])
    distances = dict.fromkeys(remaining, float("inf"))
    while len(selected) < count:
        x, y, _ = selected[-1]
        for p in remaining:
            distances[p] = min(distances[p], (p[0] - x) ** 2 + (p[1] - y) ** 2)
        choice = max(remaining, key=lambda p: (distances[p], p[2]))
        selected.append(choice)
        del remaining[choice]
    return selected


class Roads(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.points = {region: {} for region in BOXES}

    def way(self, way):
        if way.tags.get("highway") not in {"residential", "tertiary", "secondary", "primary"}:
            return
        if way.tags.get("access") in {"private", "no"}:
            return
        for node in way.nodes:
            if not node.location.valid():
                continue
            x, y = node.lon, node.lat
            for region, (a, b, c, d) in BOXES.items():
                if a < x < c and b < y < d:
                    self.points[region][node.ref] = (x, y, node.ref)


if __name__ == "__main__":
    source, output = map(Path, sys.argv[1:])
    reader = Roads()
    reader.apply_file(str(source), locations=True)
    regions = {}
    for region, nodes in reader.points.items():
        points = sorted(nodes.values(), key=lambda p: p[2])
        a, b, c, d = BOXES[region]
        centre = ((a + c) / 2, (b + d) / 2)
        if region in {"North", "East"}:
            points = sorted(
                points, key=lambda p: (p[0] - centre[0]) ** 2 + (p[1] - centre[1]) ** 2
            )[:1200]
        if region == "West":
            first = [p for p in points if abs(p[0] - 77.08) < 0.008 and abs(p[1] - 28.66) < 0.008]
            second = [p for p in points if abs(p[0] - 77.12) < 0.008 and abs(p[1] - 28.63) < 0.008]
            sparse = [p for p in points if p not in first and p not in second]
            selected = [
                p
                for group in zip(
                    spread(first, 94), spread(second, 94), spread(sparse, 94), strict=True
                )
                for p in group
            ]
        else:
            selected = spread(points, 300)
        regions[region] = {
            "bbox": BOXES[region],
            "distribution": "sparse"
            if region == "South"
            else "two clusters plus sparse"
            if region == "West"
            else "clustered",
            "nodes": [dict(longitude=x, latitude=y, osm_node_id=i) for x, y, i in selected[:280]],
        }
    output.write_text(
        json.dumps(
            {
                "source": "https://download.bbbike.org/osm/bbbike/NewDelhi/NewDelhi.osm.pbf",
                "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "osm_timestamp": "2026-10-16T23:00:00Z",
                "license": "OpenStreetMap contributors, ODbL 1.0",
                "selection": "public way vertices; deterministic farthest-point dispersion",
                "regions": regions,
            },
            indent=2,
        )
        + "\n"
    )
