"""Derived presentation of immutable recorded outputs; never calls routing or mutates authority."""

import json
from datetime import datetime, timedelta
from pathlib import Path

from audit_geographical import audit


def read_recorded_report(case):
    text = (case / "QA_TEAM_REPORT.html").read_text(encoding="utf-8")
    return json.JSONDecoder().raw_decode(text.split("const D=", 1)[1].lstrip())[0]


def arrival(route, stop):
    assessment = stop.get("assessment")
    return (
        (
            datetime.fromisoformat(route["epoch"]) + timedelta(seconds=assessment["arrival_at"])
        ).isoformat()
        if assessment
        else None
    )


def derive(data):
    diffs = []
    for execution, view in zip(data["executions"], data["snapshots"], strict=True):
        before = {r["vehicle_id"]: r for r in view["before"]["routes"] if r["status"] != "CLOSED"}
        after = {r["vehicle_id"]: r for r in view["after"]["routes"] if r["status"] != "CLOSED"}
        changes = []
        for vehicle in before.keys() | after.keys():
            old = before.get(vehicle, {"stops": [], "distance_m": 0, "travel_seconds": 0})
            new = after.get(vehicle, {"stops": [], "distance_m": 0, "travel_seconds": 0})
            old_stops = {s["task_id"]: s for s in old["stops"]}
            new_stops = {s["task_id"]: s for s in new["stops"]}
            schedules = [
                dict(
                    task_id=identity,
                    before_start=old_stops[identity]["planned_start"],
                    after_start=new_stops[identity]["planned_start"],
                    before_arrival=arrival(old, old_stops[identity]),
                    after_arrival=arrival(new, new_stops[identity]),
                )
                for identity in old_stops.keys() & new_stops.keys()
            ]
            changes.append(
                dict(
                    vehicle_id=vehicle,
                    before_order=[s["task_id"] for s in old["stops"]],
                    after_order=[s["task_id"] for s in new["stops"]],
                    existing_task_schedules=schedules,
                    distance_delta_m=new["distance_m"] - old["distance_m"],
                    travel_delta_seconds=new["travel_seconds"] - old["travel_seconds"],
                )
            )
        owned = {a["booking_id"] for a in view["after"]["assignments"]}
        reasons = []
        horizon = datetime.fromisoformat(execution["invoked_at"]) + timedelta(
            seconds=execution["horizon_seconds"]
        )
        for booking in view["after"]["bookings"]:
            if booking["booking_id"] in owned:
                continue
            actual = [o for o in booking["outcomes"] if o["cycle_id"] == execution["cycle_id"]]
            reason = (
                actual[-1]["reason"]
                if actual
                else "OUTSIDE_RECORDED_HORIZON"
                if datetime.fromisoformat(booking["slot_start"]) >= horizon
                else "NOT_IN_RECORDED_CYCLE_INPUTS"
            )
            reasons.append(
                dict(
                    booking_id=booking["booking_id"],
                    reason=reason,
                    basis="RECORDED_PLANNING_OUTCOME" if actual else "DERIVED_FROM_RECORDED_INPUTS",
                )
            )
        checks = []
        bookings = {b["booking_id"]: b for b in view["after"]["bookings"]}
        for route in after.values():
            for stop in route["stops"]:
                if stop["kind"] == "PICKUP" and stop["state"] != "CANCELLED":
                    booking = bookings[stop["booking_id"]]
                    checks.append(
                        datetime.fromisoformat(booking["slot_start"])
                        <= datetime.fromisoformat(stop["planned_start"])
                        <= datetime.fromisoformat(booking["slot_end"])
                    )
        derived = dict(
            label=execution["label"],
            basis="DERIVED_FROM_RECORDED_SNAPSHOTS",
            vehicles=changes,
            unassigned_reasons=reasons,
            planned_window_checks=len(checks),
            planned_window_violations=checks.count(False),
        )
        assert derived["planned_window_violations"] == 0, execution["label"]
        view["derived_diff"] = derived
        diffs.append(derived)
    return diffs


def render(case):
    data = read_recorded_report(case)
    diffs = derive(data)
    publications = audit(case, data)
    previous = {}
    for metric, view, execution, diff, publication in zip(
        data["metrics"], data["snapshots"], data["executions"], diffs, publications, strict=True
    ):
        active = [r for r in view["after"]["routes"] if r["status"] != "CLOSED"]
        metric["retained_revision_model_distance_m"] = sum(r["distance_m"] for r in active)
        metric["retained_revision_model_travel_seconds"] = sum(r["travel_seconds"] for r in active)
        metric["distance_m"] = publication["distance_m"]
        metric["travel_seconds"] = publication["travel_seconds"]
        metric["waiting_seconds"] = publication["waiting_seconds"]
        metric["unassigned_total"] = metric["deferred"]
        metric["scope"] = "VALIDATED_CURRENT_PUBLICATION; retained revision models include history"
        metric["publication_audit"] = publication
        view["publication_before"] = previous.get(view["after"]["region_id"])
        view["publication_after"] = publication
        previous[view["after"]["region_id"]] = publication
        inputs = (execution.get("production_measurements") or {}).get("inputs", [])
        current_ids = {
            b["booking_id"]
            for b in data["bookings"]
            if b["region_id"] == execution["region_id"] and b["cycle"] == execution["cycle"]
        }
        metric["captured_input_bookings"] = len(inputs)
        metric["new_captured_input_bookings"] = sum(i["request_id"] in current_ids for i in inputs)
        metric["previously_owned_input_bookings"] = sum(i["existing"] for i in inputs)
        metric["unassigned_outside_horizon"] = sum(
            r["reason"] == "OUTSIDE_RECORDED_HORIZON" for r in diff["unassigned_reasons"]
        )
        metric["planned_pickup_window_checks"] = diff["planned_window_checks"]
        metric["planned_pickup_window_violations"] = diff["planned_window_violations"]
    template = (Path(__file__).parents[1] / "integration/geographical_report.html").read_text(
        encoding="utf-8"
    )
    html = template.replace("__QA_DATA__", json.dumps(data).replace("<", "\\u003c"))
    return html, diffs, data["metrics"], publications
