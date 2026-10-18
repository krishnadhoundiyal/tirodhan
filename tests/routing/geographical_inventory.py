"""Fail-fast QA dependency inventory, graph hashes and finite OSRM startup timing."""

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
import platform
import resource
import subprocess
import time
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tirodhan.workers.routing_regional import LocalRoadProfile, road_processes


def version(binary):
    result = subprocess.run([binary, "--version"], capture_output=True, check=True, timeout=10)
    return (result.stdout or result.stderr).decode().strip()


async def database_version():
    assert os.getenv("TIRODHAN_TEST_DATABASE_URL"), "Mandatory disposable PostgreSQL missing"
    engine = create_async_engine(os.environ["TIRODHAN_TEST_DATABASE_URL"])
    try:
        async with engine.connect() as connection:
            return dict(
                (
                    await connection.execute(
                        text("SELECT version() AS postgres, PostGIS_full_version() AS postgis")
                    )
                )
                .mappings()
                .one()
            )
    finally:
        await engine.dispose()


def inventory():
    versions = dict(
        python=platform.python_version(),
        osrm=version("osrm-routed"),
        vroom=version("vroom"),
        ortools=importlib.metadata.version("ortools"),
    )
    assert versions["osrm"] == "v6.0.0"
    assert versions["vroom"] == "vroom 1.15.0"
    assert versions["ortools"] == "9.14.6206"
    hashes, graph_bytes = {}, {}
    for profile in ("car", "bicycle", "topology-car", "topology-bicycle"):
        files = sorted((Path("/graphs") / profile).glob("map.osrm*"))
        assert files, f"Mandatory {profile} graph missing"
        hashes[profile] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
        graph_bytes[profile] = sum(p.stat().st_size for p in files)
    startups = []
    for repetition in range(2):
        profiles = [
            LocalRoadProfile(
                profile_version=f"qa-{name}",
                graph_version=f"fixture-{name}",
                graph_path=Path(f"/graphs/{name}/map.osrm"),
                port=5033 + i,
                threads=1,
                max_snap_distance_m=300,
                block_nodes=512,
                max_pairs=262144,
            )
            for i, name in enumerate(("car", "bicycle"))
        ]
        before = resource.getrusage(resource.RUSAGE_CHILDREN)
        stamp = time.perf_counter()
        with road_processes(profiles):
            ready = time.perf_counter() - stamp
            children = []
            for status in Path("/proc").glob("[0-9]*/status"):
                try:
                    content = status.read_text()
                    parent = int(content.split("PPid:\t")[1].split()[0])
                    if parent == os.getpid():
                        children.append(
                            {
                                k: int(content.split(k + ":")[1].split()[0]) * 1024
                                for k in ("VmRSS", "VmHWM")
                            }
                        )
                except (FileNotFoundError, IndexError, ValueError):
                    continue
            assert len(children) == 2, "Mandatory OSRM child resource counters unavailable"
        after = resource.getrusage(resource.RUSAGE_CHILDREN)
        startups.append(
            dict(
                repetition=repetition,
                profiles=["car", "bicycle"],
                ready_seconds=ready,
                processes=children,
                child_cpu_seconds=after.ru_utime
                + after.ru_stime
                - before.ru_utime
                - before.ru_stime,
                stopped_seconds=time.perf_counter() - stamp,
                qualification="fresh processes; host filesystem cache not evicted",
            )
        )
    return dict(
        target_commit="d9b676d74fd84394789b1909c2afa0335f5b69a9",
        branch="codex/geographical-scalability-qa",
        versions=versions,
        packages={d.metadata["Name"]: d.version for d in importlib.metadata.distributions()},
        database=asyncio.run(database_version()),
        graph_hashes=hashes,
        graph_bytes=graph_bytes,
        osrm_startup=startups,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output.write_text(json.dumps(inventory(), indent=2, sort_keys=True) + "\n")
    print("Mandatory native/road/database inventory and restart measurements complete")
