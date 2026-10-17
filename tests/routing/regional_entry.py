"""Local native suite supervisor; not a production entry point."""

import os
import subprocess
import sys
from pathlib import Path

from tirodhan.workers.routing_regional import LocalRoadProfile, road_processes

if __name__ == "__main__":
    profile = LocalRoadProfile(
        profile_version="test-car",
        graph_version="fixture-car",
        graph_path=Path("/graphs/car/map.osrm"),
        port=5000,
        threads=1,
        max_snap_distance_m=300,
        block_nodes=512,
        max_pairs=262144,
    )
    os.environ["TIRODHAN_OSRM_TEST_URL"] = "http://127.0.0.1:5000"
    os.environ["TIRODHAN_OSRM_GRAPH_VERSION"] = "fixture-car"
    with road_processes([profile]):
        result = subprocess.call([sys.executable, "-m", "pytest", *sys.argv[1:]])
    raise SystemExit(result)
