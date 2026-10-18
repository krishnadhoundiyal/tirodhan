"""Mandatory real OSRM preflight and public-fixture manifest (QA only)."""

import argparse
import asyncio
import hashlib
import json
import math
import time
from pathlib import Path

import httpx
from geographical_fixture import DATASET_SHA256, SEED, candidates, sample


def percentiles(values):
    ordered = sorted(values)
    return (
        {f"p{p}": ordered[max(0, math.ceil(p / 100 * len(ordered)) - 1)] for p in (50, 95, 99)}
        if ordered
        else None
    )


async def generate(output):
    rows, ways = candidates(Path(__file__).with_name("data") / "delhi-road.osm.pbf")
    # Spread first, retain unique *actual snapped positions*, not unique input pins.
    ordered = sample(rows, min(1600, len(rows)))
    unique, seen, latencies = [], set(), []
    async with httpx.AsyncClient(trust_env=False, timeout=30) as client:
        for row in ordered:
            started = time.perf_counter()
            response = await client.get(
                f"http://127.0.0.1:5000/nearest/v1/driving/{row['longitude']},{row['latitude']}",
                params={"number": 1},
            )
            response.raise_for_status()
            data = response.json()
            assert data["code"] == "Ok", data["code"]
            waypoint = data["waypoints"][0]
            latencies.append(time.perf_counter() - started)
            snapped = tuple(waypoint["location"])
            if waypoint["distance"] > 300 or snapped in seen:
                continue
            seen.add(snapped)
            unique.append(dict(row, snapped=snapped, snap_distance_m=waypoint["distance"]))
            if len(unique) == 768:
                break
        assert len(unique) >= 512, "Mandatory distinct snapped-site qualification failed"
        scenarios = {
            name: sample(unique, n) for name, n in (("A", 64), ("B", 128), ("C", 256), ("D", 512))
        }
        mixed = sample(unique, 448, "mixed")
        scenarios["E"] = mixed + mixed[:64]  # Explicit shared physical access, 448 sites.
        scenarios["Control"] = json.loads(
            (Path(__file__).with_name("data") / "inc8-road-nodes.json").read_text()
        )
        reports = {}
        for name, sites in scenarios.items():
            # Table may choose different phantoms from Nearest at intersections.
            # Certify uniqueness using the actual production API, without hints.
            if name == "D":
                unused = [r for r in unique if r not in sites]
                for _ in range(20):
                    coordinates = ";".join(f"{r['longitude']},{r['latitude']}" for r in sites)
                    check = (
                        await client.get(
                            f"http://127.0.0.1:5000/table/v1/driving/{coordinates}",
                            params={"annotations": "distance,duration"},
                        )
                    ).json()
                    seen = set()
                    duplicates = []
                    for i, point in enumerate(check["sources"]):
                        key = tuple(point["location"])
                        if key in seen:
                            duplicates.append(i)
                        seen.add(key)
                    if not duplicates:
                        break
                    for i in duplicates:
                        sites[i] = unused.pop(0)
                else:
                    raise AssertionError("Could not establish 512 distinct Table snaps")
            distinct = list({(r["longitude"], r["latitude"]): r for r in sites}.values())
            coordinates = ";".join(f"{r['longitude']},{r['latitude']}" for r in distinct)
            stamp = time.perf_counter()
            raw = await client.get(
                f"http://127.0.0.1:5000/table/v1/driving/{coordinates}",
                params={"annotations": "distance,duration"},
            )
            raw.raise_for_status()
            data = raw.json()
            assert data["code"] == "Ok", data["code"]
            distances = data["distances"]
            durations = data["durations"]
            invalid, unreachable, asymmetric = [], 0, 0
            valid = []
            for i, values in enumerate(distances):
                for j, value in enumerate(values):
                    if value is None or durations[i][j] is None:
                        unreachable += 1
                    elif value < 0 or durations[i][j] < 0:
                        if len(invalid) < 5:
                            invalid.append(
                                dict(
                                    source=distinct[i],
                                    destination=distinct[j],
                                    distance=value,
                                    duration=durations[i][j],
                                )
                            )
                    elif i != j:
                        valid.append(value)
                        asymmetric += durations[i][j] != durations[j][i]
            reports[name] = dict(
                distinct_coordinates=len(distinct),
                distinct_snapped=len({tuple(p["location"]) for p in data["sources"]}),
                distinct_source_snaps=len({tuple(p["location"]) for p in data["sources"]}),
                distinct_destination_snaps=len(
                    {tuple(p["location"]) for p in data["destinations"]}
                ),
                same_source_destination_snap=sum(
                    a["location"] == b["location"]
                    for a, b in zip(data["sources"], data["destinations"], strict=True)
                ),
                bounds=[
                    min(r["longitude"] for r in distinct),
                    min(r["latitude"] for r in distinct),
                    max(r["longitude"] for r in distinct),
                    max(r["latitude"] for r in distinct),
                ],
                one_way_sites=sum(r.get("one_way", False) for r in distinct),
                bridge_sites=sum(r.get("bridge", False) for r in distinct),
                road_distance_m=percentiles(valid),
                min_distance_m=min(valid),
                max_distance_m=max(valid),
                confirmed_unreachable_cells=unreachable,
                asymmetric_duration_cells=asymmetric,
                invalid_examples=invalid,
                invalid_cells=sum(v is not None and v < 0 for row in distances for v in row),
                table_seconds=time.perf_counter() - stamp,
                table_sha256=hashlib.sha256(raw.content).hexdigest(),
            )
            # Independently construct an explicit feasible schedule: every pickup
            # is discharged before this vehicle's next pickup. No solver/certificate
            # or application insertion code is used. Times relative to 09:00.
            depot_sites = distinct + [
                dict(longitude=77.207, latitude=28.632),
                dict(longitude=77.211, latitude=28.630),
            ]
            coords = ";".join(f"{r['longitude']},{r['latitude']}" for r in depot_sites)
            witness_matrix = (
                await client.get(
                    f"http://127.0.0.1:5000/table/v1/driving/{coords}",
                    params={"annotations": "distance,duration"},
                )
            ).json()
            index = {(r["longitude"], r["latitude"]): i for i, r in enumerate(distinct)}
            n = {"A": 64, "B": 128, "C": 256}.get(name, 512)
            orders = [(i, i % 2 if i % 10 < 7 else 2 + i % 6) for i in range(n)]
            vehicles = [(-900, len(distinct)) for _ in range(80)]
            witness, failures = [], []
            for i, slot in sorted(orders, key=lambda p: (p[1], p[0])):
                site = sites[(i * 17) % len(sites)]
                node = index[(site["longitude"], site["latitude"])]
                choices = []
                for v, (at, origin) in enumerate(vehicles):
                    t = witness_matrix["durations"][origin][node]
                    d = witness_matrix["distances"][origin][node]
                    home = witness_matrix["durations"][node][-1]
                    home_d = witness_matrix["distances"][node][-1]
                    if None in (t, d, home, home_d) or min(t, d, home, home_d) < 0:
                        continue
                    start = max(at + math.ceil(t), slot * 1800)
                    finish = start + 180 + math.ceil(home) + 60
                    if start <= (slot + 1) * 1800 and finish <= 8 * 3600:
                        choices.append((finish, start, v))
                if not choices:
                    failures.append(i)
                    continue
                finish, start, v = min(choices)
                vehicles[v] = (finish, len(distinct) + 1)
                witness.append(
                    dict(booking_index=i, vehicle=v, pickup_start=start, discharge_complete=finish)
                )
            reports[name]["independent_witness"] = dict(
                method="earliest-finish pickup-then-discharge schedule; exact raw directed arcs",
                vehicles=80,
                valid=len(witness) == n,
                unplaced_indices=failures,
                schedule=witness,
            )
        output.write_text(
            json.dumps(
                dict(
                    seed=SEED,
                    dataset_sha256=DATASET_SHA256,
                    qualification="clipped-fixture-only",
                    source_candidates=len(rows),
                    unique_snapped_candidates=len(unique),
                    tagged_bridges=sum(t.get("bridge") not in (None, "no") for _, t, _ in ways),
                    tagged_restricted_ways=sum(
                        t.get("access") in ("private", "no") for _, t, _ in ways
                    ),
                    nearest_seconds=percentiles(latencies),
                    campaigns=reports,
                    sites=scenarios,
                ),
                sort_keys=True,
                indent=2,
            )
            + "\n"
        )
        print(
            json.dumps(
                {
                    name: {
                        k: v
                        for k, v in r.items()
                        if k not in ("invalid_examples", "bounds", "independent_witness")
                    }
                    for name, r in reports.items()
                }
            )
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(generate(parser.parse_args().output))
