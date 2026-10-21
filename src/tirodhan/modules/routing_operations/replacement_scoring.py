"""Advisory ordering only; road feasibility and assignment authority remain separate."""

from __future__ import annotations

from datetime import datetime
from math import asin, cos, radians, sin, sqrt
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from .economics import FleetCost


def impact_rank(cost: FleetCost, baseline: FleetCost | None) -> tuple[int, int, int, int]:
    """Compare marginal work, never different riders' pre-existing route totals.

    Missing baselines form a separate, unpriced tier, ordered by identity by the caller.
    Negative deltas are valid improvements; an unavailable baseline is never zero cost.
    """
    if baseline is None:
        return (1, 0, 0, 0)
    return (
        0,
        cost.generalized_seconds - baseline.generalized_seconds,
        cost.distance_m - baseline.distance_m,
        cost.disruptions - baseline.disruptions,
    )


def ordered_vehicles(
    sources: dict[str, Any], rescued: dict[UUID, dict[str, Any]], now: datetime
) -> list[dict[str, Any]]:
    """Prioritize temporal compatibility and geographic insertion detour, without roads.

    These are broad time envelopes and great-circle detours, not road ETA estimates or
    feasibility proofs. No candidate is excluded. Existing stops, protected destinations,
    donor rendezvous and shift end inform ordering; native/independent checks still decide.
    """
    points = {p["receiving_point_id"]: p for p in sources["points"]}
    vehicles = {v["vehicle_id"]: v for v in sources["vehicles"]}
    visits: dict[UUID, dict[str, Any]] = {}
    for row in sources["requests"]:
        visits[uuid5(NAMESPACE_URL, f"tirodhan:pickup:{row['request_id']}")] = dict(
            row, earliest=row["slot_start"], latest=row["slot_end"]
        )
        point = points.get(row["receiving_point_id"])
        if point:
            visits[uuid5(NAMESPACE_URL, f"tirodhan:discharge:{row['request_id']}")] = dict(
                point, earliest=now, latest=row["discharge_deadline"]
            )
    tasks: dict[UUID, list[dict[str, Any]]] = {}
    for task in sources["tasks"]:
        tasks.setdefault(task["route_id"], []).append(task)
    routes = {r["vehicle_id"]: r["route_id"] for r in sources["routes"]}
    pickups = []
    for row in rescued.values():
        if row["pickup_status"] == "COLLECTED":
            donor = sources.get("recovery_donor_positions", {}).get(str(row["cargo_vehicle_id"]))
            if donor is None:
                donor = vehicles.get(row["cargo_vehicle_id"])
            if donor is None or donor.get("longitude") is None:
                continue  # Existing evaluation reports missing/freshness-invalid donor evidence.
            pickups.append(dict(donor, earliest=now, latest=row["discharge_deadline"]))
        else:
            pickups.append(dict(row, earliest=row["slot_start"], latest=row["slot_end"]))

    def distance(a: dict[str, Any], b: dict[str, Any]) -> float:
        lat1, lat2 = radians(a["latitude"]), radians(b["latitude"])
        h = (
            sin((lat2 - lat1) / 2) ** 2
            + cos(lat1) * cos(lat2) * sin(radians(b["longitude"] - a["longitude"]) / 2) ** 2
        )
        return 12_742_000 * asin(sqrt(min(1.0, max(0.0, h))))

    def rank(vehicle: dict[str, Any]) -> tuple[int, float, float, str]:
        path = [dict(vehicle, earliest=now, latest=now)]
        protected = 0
        route_id = routes.get(vehicle["vehicle_id"])
        for task in tasks.get(route_id, []) if route_id is not None else []:
            visit = visits.get(task["stop_id"])
            if visit is None or visit.get("request_id") in rescued:
                continue
            path.append(visit)
            if task["state"] == "STARTED" or task.get("committed_at") is not None:
                protected = len(path) - 1
        end = points.get(vehicle["end_receiving_point_id"])
        if end:
            path.append(dict(end, earliest=now, latest=vehicle["ends_at"]))
        gap = detour = 0.0
        for pickup in pickups:
            alternatives = [
                (
                    max(
                        0.0,
                        (a["earliest"] - pickup["latest"]).total_seconds(),
                        (pickup["earliest"] - b["latest"]).total_seconds(),
                    ),
                    distance(a, pickup)
                    + distance(pickup, b)
                    # An idle vehicle has no pre-existing journey to subtract.
                    - (distance(a, b) if len(path) > 2 else 0),
                )
                for a, b in zip(path[protected:], path[protected + 1 :], strict=False)
            ]
            if alternatives:
                time_gap, extra = min(alternatives)
                gap += time_gap
                detour += extra
            else:
                gap = detour = float("inf")
        return (int(vehicle["resource_kind"] != "COMMITTED"), gap, detour, str(vehicle["shift_id"]))

    return sorted(sources["vehicles"], key=rank)
