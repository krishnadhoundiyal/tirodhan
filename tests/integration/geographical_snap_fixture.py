"""Public fixture preparation only: exclude graph-inaccessible vertices by snap distance.

Never select inputs by solver outcome, negative matrix entries or optimization quality.
All excluded source vertices and actual snap distances remain in the versioned manifest.
"""

import json
import sys
from pathlib import Path

import httpx

if __name__ == "__main__":
    source, destination, endpoint = sys.argv[1:]
    document = json.loads(Path(source).read_text())
    for name, region in document["regions"].items():
        nodes = region["nodes"]
        coordinates = ";".join(f"{p['longitude']},{p['latitude']}" for p in nodes)
        response = httpx.get(
            f"{endpoint}/table/v1/driving/{coordinates}",
            params={"annotations": "duration,distance"},
            timeout=60,
        )
        response.raise_for_status()
        raw = response.json()
        assert raw["code"] == "Ok"
        selected, excluded = [], []
        for index, node in enumerate(nodes):
            snaps = [raw[k][index]["distance"] for k in ("sources", "destinations")]
            (selected if max(snaps) <= 100 else excluded).append(
                dict(node, table_snap_distances_m=snaps)
            )
        region["nodes"] = selected
        region["excluded_graph_snap_vertices"] = excluded
        print(name, "valid public vertices", len(selected), "excluded", len(excluded))
        assert len(selected) >= 240, "Insufficient independently snapping public fixture vertices"
    document["snap_preflight"] = "OSRM 6.0.0/car; table snap <=100m; no matrix/outcome filtering"
    Path(destination).write_text(json.dumps(document, indent=2) + "\n")
