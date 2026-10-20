"""Finite acceptance supervisor using the production local OSRM supervisor."""

import json
import os
import subprocess
import sys
from pathlib import Path

from tirodhan.workers.routing_regional import LocalRoadProfile, road_processes

if __name__ == "__main__":
    fixture = json.loads(Path("tests/routing/data/geographical-delhi-nodes.json").read_text())
    profile = LocalRoadProfile(
        profile_version="qa-car",
        graph_version=fixture["source_sha256"],
        graph_path=Path("/repo/.tools/geographical-qa/NewDelhi.osrm"),
        port=5000,
        threads=2,
        max_snap_distance_m=100,
        block_nodes=512,
        max_pairs=1_048_576,
    )
    os.environ["TIRODHAN_OSRM_TEST_URL"] = "http://127.0.0.1:5000"
    with road_processes([profile]):
        result = subprocess.call([sys.executable, "-m", "pytest", *sys.argv[1:]])
    raise SystemExit(result)
