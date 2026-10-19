"""Authoritative relational snapshots, cumulative invariants and offline graphical evidence."""

import csv
import gzip
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select

from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.dispatch.models import RiderAssignment, RiderAssignmentItem
from tirodhan.modules.planning.models import PickupExecution
from tirodhan.modules.planning.routing.validation import validate_proposal
from tirodhan.modules.routing_operations.dispatch_models import (
    RouteActivation,
    RouteRevision,
    RouteRevisionStop,
    RouteTask,
    VehicleRoute,
)
from tirodhan.modules.routing_operations.models import CargoVehicleObservation, RoutingShadowRun
from tirodhan.modules.routing_operations.parallel_models import PlanningOutcome
from tirodhan.modules.routing_operations.service import PROBLEM_CODEC, PROPOSAL_CODEC


def serial(value):
    return json.loads(json.dumps(value, default=str))


def atomic_write(path, value):
    temporary = path.with_name(f".{path.name}.{uuid4()}.tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


async def snapshot(factory, region):
    routes, assignments, cargo, bookings, task_history = [], [], [], [], []
    async with factory() as session:
        for root, assignment in (
            await session.execute(
                select(VehicleRoute, RiderAssignment)
                .join(RiderAssignment, RiderAssignment.assignment_id == VehicleRoute.assignment_id)
                .where(VehicleRoute.vehicle_id.in_(region["vehicles"]))
            )
        ).all():
            revision = await session.scalar(
                select(RouteRevision).where(
                    RouteRevision.route_id == root.route_id, RouteRevision.version == root.version
                )
            )
            activation = await session.get(RouteActivation, revision.activation_id)
            run = await session.get(RoutingShadowRun, activation.run_id)
            problem = PROBLEM_CODEC.validate_python(run.problem)
            proposal = PROPOSAL_CODEC.validate_python(run.results["COMPOSITE"]["proposal"])
            assessment = validate_proposal(problem, proposal)
            assessed = next((r for r in assessment.routes if r.vehicle_id == root.vehicle_id), None)
            schedule = {s.stop_id: s for s in assessed.stops} if assessed else {}
            vehicle = next(v for v in problem.vehicles if v.vehicle_id == root.vehicle_id)
            stops = []
            for planned, task, pickup in (
                await session.execute(
                    select(RouteRevisionStop, RouteTask, PickupExecution)
                    .join(RouteTask, RouteTask.task_id == RouteRevisionStop.task_id)
                    .join(
                        PickupExecution,
                        PickupExecution.pickup_execution_id == RouteTask.pickup_execution_id,
                    )
                    .where(RouteRevisionStop.revision_id == revision.revision_id)
                    .order_by(RouteRevisionStop.ordinal)
                )
            ).all():
                load = schedule.get(task.stop_id)
                stops.append(
                    dict(
                        task_id=str(task.task_id),
                        stop_id=str(task.stop_id),
                        booking_id=str(pickup.request_id),
                        kind=task.kind,
                        ordinal=planned.ordinal,
                        state=task.state,
                        committed_at=task.committed_at,
                        started_at=task.started_at,
                        completed_at=task.completed_at,
                        planned_start=planned.planned_start,
                        planned_end=planned.planned_end,
                        assessment=asdict(load) if load else None,
                        remaining_capacity=[
                            c - q for c, q in zip(vehicle.capacity, load.load_after, strict=True)
                        ]
                        if load
                        else None,
                    )
                )
            routes.append(
                dict(
                    route_id=str(root.route_id),
                    rider_id=str(assignment.rider_id),
                    vehicle_id=str(root.vehicle_id),
                    revision_id=str(revision.revision_id),
                    version=root.version,
                    status=root.status,
                    assignment_id=str(root.assignment_id),
                    assignment_status=assignment.status,
                    capacity=vehicle.capacity,
                    initial_cargo=[asdict(c) for c in vehicle.initial_cargo],
                    travel_seconds=assessed.travel_seconds if assessed else 0,
                    distance_m=assessed.distance_m if assessed else 0,
                    epoch=problem.epoch,
                    stops=stops,
                )
            )
        for _item, pickup, assignment, root in (
            await session.execute(
                select(RiderAssignmentItem, PickupExecution, RiderAssignment, VehicleRoute)
                .join(
                    PickupExecution,
                    PickupExecution.pickup_execution_id == RiderAssignmentItem.pickup_execution_id,
                )
                .join(
                    RiderAssignment,
                    RiderAssignment.assignment_id == RiderAssignmentItem.assignment_id,
                )
                .join(VehicleRoute, VehicleRoute.assignment_id == RiderAssignment.assignment_id)
                .where(
                    VehicleRoute.vehicle_id.in_(region["vehicles"]),
                    RiderAssignmentItem.released_at.is_(None),
                    RiderAssignment.status == "ACTIVE",
                )
            )
        ).all():
            assignments.append(
                dict(
                    booking_id=str(pickup.request_id),
                    rider_id=str(assignment.rider_id),
                    vehicle_id=str(root.vehicle_id),
                    route_id=str(root.route_id),
                    revision=root.version,
                    assignment_id=str(assignment.assignment_id),
                    status=pickup.status,
                    region=region["name"],
                    planned_pickup=next(
                        (
                            s["planned_start"]
                            for r in routes
                            if r["route_id"] == str(root.route_id)
                            for s in r["stops"]
                            if s["booking_id"] == str(pickup.request_id) and s["kind"] == "PICKUP"
                        ),
                        None,
                    ),
                    planned_discharge=next(
                        (
                            s["planned_start"]
                            for r in routes
                            if r["route_id"] == str(root.route_id)
                            for s in r["stops"]
                            if s["booking_id"] == str(pickup.request_id)
                            and s["kind"] == "DISCHARGE"
                        ),
                        None,
                    ),
                )
            )
        for row in await session.scalars(
            select(CargoVehicleObservation).where(
                CargoVehicleObservation.vehicle_id.in_(region["vehicles"])
            )
        ):
            cargo.append({col.name: getattr(row, col.name) for col in row.__table__.columns})
        for task, assignment in (
            await session.execute(
                select(RouteTask, RiderAssignment)
                .join(VehicleRoute, VehicleRoute.route_id == RouteTask.route_id)
                .join(RiderAssignment, RiderAssignment.assignment_id == VehicleRoute.assignment_id)
                .where(VehicleRoute.vehicle_id.in_(region["vehicles"]))
            )
        ).all():
            task_history.append(
                dict(
                    rider_id=str(assignment.rider_id),
                    **{c.name: getattr(task, c.name) for c in task.__table__.columns},
                )
            )
        ids = [b["booking_id"] for b in region["bookings"]]
        if ids:
            from uuid import UUID

            for booking in await session.scalars(
                select(CollectionRequest).where(
                    CollectionRequest.request_id.in_(list(map(UUID, ids)))
                )
            ):
                outcomes = list(
                    await session.scalars(
                        select(PlanningOutcome)
                        .where(PlanningOutcome.request_id == booking.request_id)
                        .order_by(PlanningOutcome.evaluated_at)
                    )
                )
                bookings.append(
                    dict(
                        booking_id=str(booking.request_id),
                        status=booking.status,
                        slot_start=booking.slot_start,
                        slot_end=booking.slot_end,
                        outcomes=[
                            {c.name: getattr(o, c.name) for c in o.__table__.columns}
                            for o in outcomes
                        ],
                    )
                )
    return serial(
        dict(
            region=region["name"],
            region_id=region["region_id"],
            routes=routes,
            assignments=assignments,
            cargo=cargo,
            bookings=bookings,
            task_history=task_history,
        )
    )


class Ledger:
    """Retain all previous commitments, rather than just comparing adjacent snapshots."""

    def __init__(self):
        self.committed = set()
        self.protected = {}
        self.cargo = {}

    def check(self, before, after):
        self.committed.update(a["booking_id"] for a in before["assignments"])
        for task in before.get("task_history", []):
            if task["committed_at"] or task["started_at"] or task["state"] == "COMPLETED":
                self.protected[str(task["task_id"])] = (task["rider_id"], task)
        active = [a["booking_id"] for a in after["assignments"]]
        terminal = {
            b["booking_id"] for b in after["bookings"] if b["status"] in {"COMPLETED", "CANCELLED"}
        }
        assert len(active) == len(set(active)), "Duplicate active booking ownership"
        assert self.committed <= set(active) | terminal, "Historical commitment silently lost"
        vehicles, riders = [], []
        tasks = {str(t["task_id"]): (t["rider_id"], t) for t in after.get("task_history", [])}
        bookings = {b["booking_id"]: b for b in after["bookings"]}
        for route in after["routes"]:
            if route["status"] != "CLOSED":
                vehicles.append(route["vehicle_id"])
                riders.append(route["rider_id"])
            for stop in route["stops"]:
                tasks[stop["task_id"]] = (route["rider_id"], stop)
                if stop["kind"] == "PICKUP" and stop["state"] != "CANCELLED":
                    booking = bookings[stop["booking_id"]]
                    assert (
                        datetime.fromisoformat(booking["slot_start"])
                        <= datetime.fromisoformat(stop["planned_start"])
                        <= datetime.fromisoformat(booking["slot_end"])
                    ), "Pickup window violated"
        assert len(vehicles) == len(set(vehicles)), "Conflicting vehicle routes"
        assert len(riders) == len(set(riders)), "Conflicting rider routes"
        for identity, (rider, previous) in self.protected.items():
            assert identity in tasks, "Protected task disappeared"
            owner, current = tasks[identity]
            assert owner == rider, "Protected task changed custodian"
            assert current["kind"] == previous["kind"]
            if previous["state"] != "CANCELLED":
                assert current["state"] != "CANCELLED", "Protected task cancelled without recovery"
            if previous["state"] == "COMPLETED":
                assert current["state"] == "COMPLETED", "Completed task rolled back"
                assert current["completed_at"] == previous["completed_at"]
        for identity, row in tasks.items():
            if row[1]["committed_at"] or row[1]["started_at"] or row[1]["state"] == "COMPLETED":
                self.protected[identity] = row
        current_cargo = {str(c["request_id"]): c for c in after["cargo"]}
        for identity, previous in self.cargo.items():
            assert identity in current_cargo, "Cargo observation erased"
            assert current_cargo[identity] == previous, "Cargo observation rewritten"
        self.cargo.update(current_cargo)
        old = {a["booking_id"]: a for a in before["assignments"]}
        new = {a["booking_id"]: a for a in after["assignments"]}
        diff = {
            "added": sorted(set(new) - set(old)),
            "retained": [],
            "revised": [],
            "transferred": [],
            "historical_preserved": len(self.committed),
            "lost": sorted(self.committed - set(new) - terminal),
        }
        for identity in old.keys() & new.keys():
            kind = (
                "transferred"
                if old[identity]["rider_id"] != new[identity]["rider_id"]
                else "revised"
                if old[identity]["revision"] != new[identity]["revision"]
                else "retained"
            )
            diff[kind].append(identity)
        self.committed.update(active)
        return diff


def write_evidence(root, evidence):
    root.mkdir(parents=True, exist_ok=True)
    documents = Path(__file__).parents[2] / "docs/qa/geographical-rolling"
    for name in ("QA_EXECUTION_CONTRACT.md", "QA_SCENARIO_MATRIX.md"):
        if (documents / name).exists():
            (root / name).write_text((documents / name).read_text())
    for name, key in [
        ("QA_EXECUTIONS", "executions"),
        ("QA_PLAN_SNAPSHOTS", "snapshots"),
        ("QA_INVARIANTS", "invariants"),
        ("QA_METRICS", "metrics"),
        ("QA_EXECUTION_EVENTS", "physical_events"),
    ]:
        atomic_write(
            root / f"{name}.json", json.dumps(evidence.get(key, []), default=str, indent=2) + "\n"
        )
    trace = [json.loads(p.read_text()) for p in sorted((root / "trace").glob("*.json"))]
    (root / "QA_SOLVER_TRACE.json").write_text(json.dumps(trace, default=str, indent=2) + "\n")
    for name, rows in [
        ("QA_BOOKINGS", evidence["bookings"]),
        ("QA_ASSIGNMENTS", evidence["assignments"]),
    ]:
        with (root / f"{name}.csv").open("w", newline="") as output:
            if rows:
                writer = csv.DictWriter(output, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            else:
                output.write("No recorded rows\n")
    # Embed only data, no external map/JS services. Connectors are explicitly schematic;
    # their metrics come from real OSRM matrices, not line lengths on this display.
    template = (Path(__file__).with_name("geographical_report.html")).read_text()
    payload = json.dumps(serial(dict(evidence, solver_trace=trace))).replace("<", "\\u003c")
    atomic_write(root / "QA_TEAM_REPORT.html", template.replace("__QA_DATA__", payload))
    counts = {
        engine: sum(len(t.get("observations", [])) for t in trace if t.get("engine") == engine)
        for engine in ("VROOM", "OR_TOOLS")
    }
    counts["CP_SAT_COORDINATOR"] = sum(t["kind"] == "coordinator" for t in trace)
    counts["CP_SAT_STAGES"] = sum(len(t.get("stages", [])) for t in trace)
    (root / "QA_FINAL_REPORT.md").write_text(
        "# Recorded geographical QA run\n\n"
        + f"Executions: {len(evidence['executions'])}; authoritative ACTIVATED cycles: "
        + f"{sum(e.get('status') == 'ACTIVATED' for e in evidence['executions'])}.\n\n"
        + f"Actual engine calls: `{counts}`.\n\n"
        + "Failures are acceptance failures, not successful optimization. See QA_EXECUTIONS.json "
        + "and QA_INVARIANTS.json. Public OSM synthetic scenario; map edges are schematic only.\n"
    )


def archive_problem(root, run):
    with gzip.open(root / f"problem-{run.run_id}.json.gz", "wt") as output:
        json.dump(
            dict(configuration=run.configuration, problem=run.problem, results=run.results), output
        )
