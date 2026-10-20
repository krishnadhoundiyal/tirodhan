"""Offline smoke and archive-safety tests for the geographical QA route renderer."""

from __future__ import annotations

import csv
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

MODULE = Path(__file__).with_name("render_geographical_route_reports.py")
spec = importlib.util.spec_from_file_location("geographical_route_reports", MODULE)
renderer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(renderer)


def fixture(case: Path) -> None:
    case.mkdir(parents=True)
    headers = [
        "booking_id",
        "region",
        "region_id",
        "cycle",
        "ordinal",
        "arrival_at",
        "slot_start",
        "slot_end",
        "longitude",
        "latitude",
        "osm_node_id",
        "table_snap_distances_m",
    ]
    data = [
        [
            "b1",
            "West",
            "region-1",
            "1",
            "0",
            "2026-10-20T09:00:00+05:30",
            "2026-10-20T09:30:00+05:30",
            "2026-10-20T10:00:00+05:30",
            "77.08",
            "28.66",
            "101",
            "[0, 0]",
        ],
        [
            "b2",
            "West",
            "region-1",
            "2",
            "1",
            "2026-10-20T09:01:00+05:30",
            "2026-10-20T10:00:00+05:30",
            "2026-10-20T10:30:00+05:30",
            "77.12",
            "28.63",
            "102",
            "[0, 0]",
        ],
    ]
    with (case / "QA_BOOKINGS.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(headers)
        writer.writerows(data)

    def route(bids: list[str], version: int) -> dict:
        return {
            "rider_id": "rider-1",
            "vehicle_id": "vehicle-1",
            "status": "ACTIVE",
            "version": version,
            "distance_m": 3000 * len(bids),
            "travel_seconds": 350 * len(bids),
            "stops": [
                {
                    "booking_id": booking_id,
                    "kind": "PICKUP",
                    "planned_start": "2026-10-20 04:00:00+00:00",
                    "planned_end": "2026-10-20 04:03:00+00:00",
                    "state": "PENDING",
                }
                for booking_id in bids
            ]
            + [
                {
                    "booking_id": booking_id,
                    "kind": "DISCHARGE",
                    "planned_start": "2026-10-20 05:00:00+00:00",
                    "planned_end": "2026-10-20 05:01:00+00:00",
                    "state": "PENDING",
                }
                for booking_id in bids
            ],
        }

    snapshots = []
    executions = []
    metrics = []
    for cycle in (1, 2):
        before = [route(["b1"], 1)] if cycle == 2 else []
        after = [route(["b1"], 1)] if cycle == 1 else [route(["b1", "b2"], 2)]
        assignments_before = (
            [{"booking_id": "b1", "rider_id": "rider-1", "status": "PENDING"}] if cycle == 2 else []
        )
        assignments_after = [
            {"booking_id": booking_id, "rider_id": "rider-1", "status": "PENDING"}
            for booking_id in (["b1"] if cycle == 1 else ["b1", "b2"])
        ]
        label = f"primary:West:R{cycle}"
        snapshots.append(
            {
                "label": label,
                "before": {"routes": before, "assignments": assignments_before, "bookings": []},
                "after": {"routes": after, "assignments": assignments_after, "bookings": []},
            }
        )
        executions.append(
            {
                "label": label,
                "region_id": "region-1",
                "region": "West",
                "cycle": cycle,
                "total_bookings": cycle,
                "invoked_at": f"2026-10-20T09:0{cycle - 1}:00+05:30",
                "elapsed_seconds": 5.1,
                "status": "ACTIVATED",
                "horizon_seconds": 7200,
                "fleet_size": 10,
                "production_measurements": {"timings": {"worker_seconds": 4.0}},
            }
        )
        metrics.append({"label": label, "assigned": cycle, "unassigned_outside_horizon": 0})

    regions = [{"region_id": "region-1", "origins": [{"latitude": 28.67, "longitude": 77.07}]}]
    for name, value in [
        ("QA_PLAN_SNAPSHOTS.json", snapshots),
        ("QA_EXECUTIONS.json", executions),
        ("QA_METRICS.json", metrics),
        ("QA_REGION_GEOGRAPHY.json", regions),
    ]:
        (case / name).write_text(json.dumps(value), encoding="utf-8")


class RouteReportTests(unittest.TestCase):
    def test_reporting_and_overwrite_protection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture(root / "primary")
            self.assertEqual(renderer.main(["--evidence-root", str(root)]), 0)
            first = root / "primary" / "route-reports" / "West-R1.html"
            second = root / "primary" / "route-reports" / "West-R2.html"
            self.assertTrue(first.exists())
            self.assertTrue(second.exists())
            rendered = second.read_text(encoding="utf-8")
            self.assertIn("W01", rendered)
            self.assertIn("W02", rendered)
            self.assertIn("before-booking-map", rendered)
            self.assertIn("after-booking-map", rendered)
            self.assertIn("rider-route", rendered)
            self.assertIn("77.12", rendered)
            self.assertIn("28.63", rendered)
            self.assertIn("QA_BOOKINGS.csv", rendered)
            initial = first.read_bytes()
            with self.assertRaises(FileExistsError):
                renderer.main(["--evidence-root", str(root)])
            self.assertEqual(initial, first.read_bytes())
            self.assertIn("b2", (root / "primary" / "QA_BOOKINGS.csv").read_text())

    def test_missing_region_manifest_blocks_false_geometry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            case = Path(directory) / "primary"
            fixture(case)
            (case / "QA_REGION_GEOGRAPHY.json").unlink()
            with self.assertRaises(ValueError) as ctx:
                renderer.main(["--evidence-root", str(case)])
            self.assertIn("missing QA_REGION_GEOGRAPHY.json", str(ctx.exception))

    def test_bookings_must_match_recorded_count(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            case = Path(directory) / "primary"
            fixture(case)
            path = case / "QA_BOOKINGS.csv"
            rows = path.read_text(encoding="utf-8").splitlines()
            path.write_text("\n".join(rows[:-1]) + "\n", encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                renderer.main(["--evidence-root", str(case)])
            self.assertIn("expected 2 bookings", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
