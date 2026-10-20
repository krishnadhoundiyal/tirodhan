"""Render immutable, standalone geographic QA route reports from recorded evidence.

Stdlib only. No routing solvers, network calls, or database needed. Reports are
written beside (never in place of) QA_BOOKINGS/QA_PLAN_SNAPSHOTS/etc.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

REQUIRED = ("QA_BOOKINGS.csv", "QA_PLAN_SNAPSHOTS.json", "QA_EXECUTIONS.json", "QA_METRICS.json")
VERSION = "1.0"
PAGE_TEMPLATE = Path(__file__).with_name("geographical_route_report_template.html")


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def input_hashes(case_root: Path) -> dict[str, str]:
    names = [*REQUIRED, "QA_REGION_GEOGRAPHY.json"]
    return {
        n: hashlib.sha256((case_root / n).read_bytes()).hexdigest()
        for n in names if (case_root / n).is_file()
    }


def geography(case_root: Path) -> list[dict[str, Any]]:
    geo = case_root / "QA_REGION_GEOGRAPHY.json"
    if geo.exists():
        data = load_json(geo)
        if not isinstance(data, list):
            raise ValueError(f"{geo}: expected list of operating regions")
        return data
    # Prior QA runs embedded region geography only in the original offline HTML.
    legacy = case_root / "QA_TEAM_REPORT.html"
    if legacy.exists():
        content = legacy.read_text(encoding="utf-8")
        match = re.search(r"\bconst D\s*=\s*", content)
        if match:
            data, _ = json.JSONDecoder().raw_decode(content[match.end():])
            return data.get("operating_regions", [])
    raise ValueError(
        f"{case_root}: missing QA_REGION_GEOGRAPHY.json, and no region geography "
        "could be read from QA_TEAM_REPORT.html; cannot plot a verified discharge point"
    )


def evidence_cases(root: Path) -> list[Path]:
    if all((root / n).exists() for n in REQUIRED):
        return [root]
    candidates = sorted(
        p for p in root.iterdir() if p.is_dir() and all((p / n).exists() for n in REQUIRED)
    )
    if not candidates:
        raise ValueError(f"No complete geographical QA evidence found in {root}")
    return candidates


def slug(text: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_-]+", "-", text).strip("-")
    if not normalized:
        raise ValueError(f"Unsafe or empty report label: {text!r}")
    return normalized


def alias(region: str, index: int) -> str:
    """Region-prefixed stable ordinal (W01/N01/S01/E01 for primary QA)."""
    return f"{region[:1].upper()}{index + 1:02d}"


def build_case(case_root: Path, output_root: Path) -> list[dict[str, Any]]:
    with (case_root / "QA_BOOKINGS.csv").open(encoding="utf-8", newline="") as f:
        bookings = list(csv.DictReader(f))
    snapshots = load_json(case_root / "QA_PLAN_SNAPSHOTS.json")
    executions = load_json(case_root / "QA_EXECUTIONS.json")
    metrics = load_json(case_root / "QA_METRICS.json")
    geographical = geography(case_root)
    geo_by_region = {str(x["region_id"]): x for x in geographical}
    snap_by_label = {s["label"]: s for s in snapshots}
    met_by_label = {m["label"]: m for m in metrics}
    if len(snap_by_label) != len(snapshots) or len(met_by_label) != len(metrics):
        raise ValueError("Duplicate labels in QA snapshots or metrics")
    if len({b["booking_id"] for b in bookings}) != len(bookings):
        raise ValueError("Duplicate booking identifiers in QA_BOOKINGS.csv")
    for e in executions:
        if e["label"] not in snap_by_label or e["label"] not in met_by_label:
            raise ValueError(f"Execution {e['label']}: missing snapshot or metrics")
    hashes = input_hashes(case_root)
    by_region = defaultdict(list)
    for e in executions:
        by_region[e["region_id"]].append(e)
    booking_by_id = {b["booking_id"]: b for b in bookings}
    page = PAGE_TEMPLATE.read_text(encoding="utf-8")
    if page.count("__QA_ROUTE_REPORT_DATA__") != 1:
        raise ValueError("Route report HTML template must have one data placeholder")
    out_index = []
    for region_id, events in sorted(by_region.items()):
        events.sort(key=lambda e: (int(e["cycle"]), e["label"]))
        g = geo_by_region.get(str(region_id))
        if not g or not g.get("origins"):
            raise ValueError(f"Region {region_id}: no recorded receiving-point geography")
        origin = g["origins"][0]
        if "latitude" not in origin or "longitude" not in origin:
            raise ValueError(f"Region {region_id}: invalid discharge coordinate")
        # The QA fixture declares its synthetic discharge facility at origins[0].
        facility = {
            "latitude": float(origin["latitude"]),
            "longitude": float(origin["longitude"]),
            "label": f"Synthetic QA {events[0]['region']} discharge",
            "source": "QA operating-region origins[0] (declared synthetic receiving point)",
        }
        # Stable labels by first route appearance, without relying on the order of UUIDs.
        rider_ids: list[str] = []
        for e in events:
            for r in snap_by_label[e["label"]]["after"]["routes"]:
                if r["rider_id"] not in rider_ids:
                    rider_ids.append(r["rider_id"])
        rider_alias = {r: f"Rider {i+1:02d}" for i, r in enumerate(rider_ids)}

        for e in events:
            snap = snap_by_label[e["label"]]
            met = met_by_label[e["label"]]
            chosen = [b for b in bookings if b["region_id"] == region_id and int(b["cycle"]) <= int(e["cycle"])]
            before = [b for b in chosen if int(b["cycle"]) < int(e["cycle"])]
            after = chosen
            if len(after) != int(e["total_bookings"]):
                raise ValueError(f"{e['label']}: expected {e['total_bookings']} bookings, found {len(after)}")
            after_ids = {b["booking_id"] for b in after}
            for phase in ("before", "after"):
                for route in snap[phase]["routes"]:
                    for stop in route["stops"]:
                        if stop["booking_id"] not in booking_by_id:
                            raise ValueError(f"{e['label']}: route stop has unknown booking {stop['booking_id']}")
                        if phase == "after" and stop["booking_id"] not in after_ids:
                            raise ValueError(f"{e['label']}: route stop points to future booking")
            for b in chosen:
                if not (-90 <= float(b["latitude"]) <= 90 and -180 <= float(b["longitude"]) <= 180):
                    raise ValueError(f"{e['label']}: invalid booking coordinate")
            if len(snap["after"]["assignments"]) != met["assigned"]:
                raise ValueError(f"{e['label']}: assignment metrics mismatch")
            docs = {
                "case": case_root.name,
                "region": e["region"],
                "cycle": int(e["cycle"]),
                "execution": {
                    "label": e["label"], "invoked_at": e["invoked_at"],
                    "elapsed_seconds": e["elapsed_seconds"], "status": e["status"],
                    "horizon_seconds": e["horizon_seconds"], "fleet_size": e["fleet_size"],
                    "timings": (e.get("production_measurements") or {}).get("timings", {}),
                },
                "metrics": met,
                "facility": facility,
                "bookings": [
                    {
                        "id": b["booking_id"], "ref": alias(e["region"], int(b["ordinal"])),
                        "ordinal": int(b["ordinal"]), "cycle": int(b["cycle"]),
                        "latitude": float(b["latitude"]), "longitude": float(b["longitude"]),
                        "osm_node_id": b.get("osm_node_id", ""),
                        "slot_start": b["slot_start"], "slot_end": b["slot_end"],
                    }
                    for b in after
                ],
                "phases": {
                    phase: {
                        "routes": [
                            {
                                "vehicle_id": r["vehicle_id"],
                                "rider_id": r["rider_id"],
                                "rider": rider_alias[r["rider_id"]],
                                "version": r["version"],
                                "status": r["status"],
                                "distance_m": r["distance_m"],
                                "travel_seconds": r["travel_seconds"],
                                "stops": [
                                    {
                                        "kind": s["kind"], "booking_id": s["booking_id"],
                                        "time": s["planned_start"], "end": s["planned_end"],
                                        "state": s["state"],
                                    }
                                    for s in r["stops"]
                                ],
                            }
                            for r in snap[phase]["routes"]
                        ],
                        "assigned": len(snap[phase]["assignments"]),
                        "assignments": [
                            {
                                "booking_id": a["booking_id"],
                                "rider_id": a["rider_id"],
                                "rider": rider_alias[a["rider_id"]],
                                "status": a["status"],
                            }
                            for a in snap[phase]["assignments"]
                        ],
                        "accepted": len(before) if phase == "before" else len(after),
                    }
                    for phase in ("before", "after")
                },
                "source_hashes": hashes,
                "renderer_version": VERSION,
            }
            rel = f"{slug(e['region'])}-R{e['cycle']}.html"
            target = output_root / rel
            if target.exists():
                raise FileExistsError(f"Report already exists; preserving prior output: {target}")
            rendered = page.replace(
                "__QA_ROUTE_REPORT_DATA__", json.dumps(docs, ensure_ascii=True).replace("<", "\\u003c")
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            # Exclusive creation forbids accidental overwrites, including between processes.
            with target.open("x", encoding="utf-8") as f:
                f.write(rendered)
            out_index.append({"region": e["region"], "cycle": int(e["cycle"]), "report": rel,
                              "bookings": len(after), "assigned": met["assigned"], "status": e["status"]})
    return out_index


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-root", required=True, type=Path,
                        help="One case directory or its parent run directory")
    args = parser.parse_args(argv)
    root = args.evidence_root.resolve()
    cases = evidence_cases(root)
    # Case directories are owned by a unique QA run. Never touch evidence originals.
    for case in cases:
        reports = case / "route-reports"
        reports.mkdir(exist_ok=True)
        idx = build_case(case, reports)
        listing = reports / "index.html"
        if listing.exists():
            raise FileExistsError(f"Report index already exists: {listing}")
        links = "".join(
            f'<li><a href="{r["report"]}">{r["region"]} R{r["cycle"]}</a> — '
            f'{r["assigned"]}/{r["bookings"]} assigned — {r["status"]}</li>' for r in idx
        )
        with listing.open("x", encoding="utf-8") as out:
            out.write('<!doctype html><meta charset="utf-8"><title>Tirodhan geographic route reports</title>'
                      '<style>body{font:16px system-ui;margin:35px;line-height:1.8}a{color:#215a9e}</style>'
                      f'<h1>{case.name} — geographic QA route reports</h1><ul>{links}</ul>')
        print(f"Wrote {len(idx)} reports and index: {listing}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Geographical route report error: {exc}", file=sys.stderr)
        sys.exit(2)