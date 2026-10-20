"""Independent replay of recorded publication proofs against recorded relational task authority."""

import gzip
import json

from tirodhan.modules.planning.routing.validation import validate_proposal
from tirodhan.modules.routing_operations.service import PROBLEM_CODEC, PROPOSAL_CODEC


def audit(case, data):
    publications = {}
    for path in case.glob("problem-*.json.gz"):
        value = json.loads(gzip.decompress(path.read_bytes()))
        publications[value["results"]["COMPOSITE"]["selection"]["pool_digest"]] = value
    reports = []
    for execution, view in zip(data["executions"], data["snapshots"], strict=True):
        matching = [
            t
            for t in data["solver_trace"]
            if t["kind"] == "coordinator"
            and t["cycle"] == execution["label"]
            and t.get("selection", {}).get("pool_digest") in publications
        ]
        assert matching, execution["label"]
        digests = {t["selection"]["pool_digest"] for t in matching}
        assert len(digests) == 1, "Ambiguous recorded publication lineage"
        value = publications[next(iter(digests))]
        problem = PROBLEM_CODEC.validate_python(value["problem"])
        if execution.get("run_id"):
            assert str(problem.problem_id) == execution["run_id"]
        proposal = PROPOSAL_CODEC.validate_python(value["results"]["COMPOSITE"]["proposal"])
        assessment = validate_proposal(problem, proposal)
        actual = {
            r["vehicle_id"]: tuple(
                s["stop_id"] for s in r["stops"] if s["state"] in {"PENDING", "STARTED"}
            )
            for r in view["after"]["routes"]
            if r["status"] != "CLOSED"
        }
        expected = {
            str(r.vehicle_id): tuple(map(str, r.stop_ids)) for r in proposal.routes if r.stop_ids
        }
        assert actual == expected, (
            execution["label"],
            "Committed pending order differs from recorded publication",
        )
        distance = sum(r.distance_m for r in assessment.routes if r.stops)
        travel = sum(r.travel_seconds for r in assessment.routes if r.stops)
        waiting = sum(s.service_start_at - s.arrival_at for r in assessment.routes for s in r.stops)
        recorded = execution["production_measurements"]["routing_metrics"]
        assert (distance, travel, waiting) == (
            recorded["distance_m"],
            recorded["travel_seconds"],
            recorded["waiting_seconds"],
        )
        reports.append(
            dict(
                label=execution["label"],
                join_basis="EXACT_RECORDED_SELECTION_POOL_DIGEST",
                pool_digest=next(iter(digests)),
                source_problem_id=str(problem.problem_id),
                source_snapshot_id=str(problem.snapshot_id),
                epoch=problem.epoch.isoformat(),
                independent_gate_valid=True,
                pending_sequence_equals_committed_authority=True,
                distance_m=distance,
                travel_seconds=travel,
                waiting_seconds=waiting,
                matrix_nodes=[len(m.node_ids) for m in problem.matrices],
            )
        )
    return reports
