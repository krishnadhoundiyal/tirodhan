"""Package genuine recorded QA outputs without replaying or changing operational state.

Archives preserve the complete structured scenario directory, raw road responses,
submitted CP models, native traces and persisted problem witnesses. Review copies are
small indexes/primary report; SHA256 manifests verify archive contents after extraction.
"""

import argparse
import csv
import gzip
import hashlib
import html
import json
import shutil
import time
import zipfile
from collections import Counter
from pathlib import Path

from render_geographical import render


def load(path):
    return json.loads(path.read_text())


def summarize(case):
    executions = load(case / "QA_EXECUTIONS.json")
    invariants = load(case / "QA_INVARIANTS.json")
    traces = load(case / "QA_SOLVER_TRACE.json")
    scopes = {
        t["task_id"]: t["cycle"]
        for d in traces
        if d["kind"] == "worker_dispatch"
        for t in [dict(t, cycle=d["cycle"]) for t in d["tasks"]]
    }
    calls = Counter()
    failures = Counter()
    for t in traces:
        if t["kind"] == "native_worker":
            for observed in t["observations"]:
                calls[t["engine"]] += 1
                if observed["status"] != "SUCCEEDED":
                    failures[f"{t['engine']}:{observed.get('failure')}"] += 1
    calls["CP_SAT_COORDINATOR"] = sum(t["kind"] == "coordinator" for t in traces)
    calls["CP_SAT_STAGES"] = sum(len(t.get("stages", [])) for t in traces)
    matrix_sizes = {}
    for path in case.glob("problem-*.json.gz"):
        raw = json.loads(gzip.decompress(path.read_bytes()))
        matrix_sizes[raw["problem"]["problem_id"]] = [
            len(m["node_ids"]) for m in raw["problem"]["matrices"]
        ]
    metrics = []
    for e in executions:
        production = e.get("production_measurements") or {}
        actual = production.get("routing_metrics", {})
        pools = [t for t in traces if t["kind"] == "coordinator" and t["cycle"] == e["label"]]
        native = [
            o
            for t in traces
            if t["kind"] == "native_worker" and scopes.get(t["task_id"]) == e["label"]
            for o in t["observations"]
        ]
        metrics.append(
            dict(
                label=e["label"],
                actual_native_calls=Counter(o["engine"] for o in native),
                actual_coordinator_calls=len(pools),
                production=actual,
                native_failures=Counter(
                    o.get("failure") for o in native if o["status"] != "SUCCEEDED"
                ),
            )
        )
    final = {}
    snapshots = load(case / "QA_PLAN_SNAPSHOTS.json")
    for p in snapshots:
        final[p["after"]["region"]] = p["after"]
    with (case / "QA_BOOKINGS.csv").open(newline="") as source:
        bookings = list(csv.DictReader(source))
    return dict(
        scenario=case.name,
        cycles=len(executions),
        activated=sum(e["status"] == "ACTIVATED" for e in executions),
        bookings=len(bookings),
        calls=dict(calls),
        native_candidate_failures=dict(failures),
        violations=[i for i in invariants if i["violation"]],
        matrix_max_nodes=max((n for sizes in matrix_sizes.values() for n in sizes), default=0),
        matrix_shapes_by_persisted_problem=matrix_sizes,
        cycles_metrics=metrics,
        final_assignments={k: len(v["assignments"]) for k, v in final.items()},
        final_used_riders={
            k: len({a["rider_id"] for a in v["assignments"]}) for k, v in final.items()
        },
        executions=executions,
    )


def archive(case, destination):
    rendered, diffs, metrics, publications = (
        render(case) if (case / "QA_EXECUTIONS.json").exists() else (None, None, None, None)
    )
    with zipfile.ZipFile(
        destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
    ) as output:
        for path in sorted(case.rglob("*")):
            if path.is_file():
                member = (Path(case.name) / path.relative_to(case)).as_posix()
                if path.name == "QA_TEAM_REPORT.html" and rendered:
                    output.writestr(member, rendered)
                elif path.name == "QA_METRICS.json" and metrics is not None:
                    output.write(path, arcname=f"{case.name}/raw/QA_METRICS.json")
                    output.writestr(member, json.dumps(metrics, indent=2))
                else:
                    output.write(path, arcname=member)
        if diffs is not None:
            output.writestr(
                (Path(case.name) / "QA_DERIVED_DIFFS.json").as_posix(), json.dumps(diffs, indent=2)
            )
            output.writestr(
                f"{case.name}/QA_PUBLICATION_AUDIT.json", json.dumps(publications, indent=2)
            )
    # Verify every member CRC before publishing its SHA256.
    with zipfile.ZipFile(destination) as output:
        assert output.testzip() is None
    return dict(
        file=destination.name,
        sha256=hashlib.sha256(destination.read_bytes()).hexdigest(),
        bytes=destination.stat().st_size,
    )


def refresh_archive(case, destination):
    if not (case / "QA_EXECUTIONS.json").exists():
        return dict(
            file=destination.name,
            sha256=hashlib.sha256(destination.read_bytes()).hexdigest(),
            bytes=destination.stat().st_size,
        )
    rendered, diffs, metrics, publications = render(case)
    temporary = destination.with_suffix(".new.zip")
    with (
        zipfile.ZipFile(destination) as source,
        zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
        ) as output,
    ):
        for member in source.infolist():
            if member.filename.endswith(
                ("/QA_TEAM_REPORT.html", "/QA_DERIVED_DIFFS.json", "/QA_PUBLICATION_AUDIT.json")
            ):
                continue
            if member.filename == f"{case.name}/QA_METRICS.json":
                if f"{case.name}/raw/QA_METRICS.json" not in source.namelist():
                    output.writestr(
                        f"{case.name}/raw/QA_METRICS.json", source.read(member.filename)
                    )
                continue
            output.writestr(member, source.read(member.filename))
        output.writestr(f"{case.name}/QA_TEAM_REPORT.html", rendered)
        output.writestr(f"{case.name}/QA_DERIVED_DIFFS.json", json.dumps(diffs, indent=2))
        output.writestr(f"{case.name}/QA_METRICS.json", json.dumps(metrics, indent=2))
        output.writestr(
            f"{case.name}/QA_PUBLICATION_AUDIT.json", json.dumps(publications, indent=2)
        )
    with zipfile.ZipFile(temporary) as output:
        assert output.testzip() is None
    for attempt in range(10):
        try:
            temporary.replace(destination)
            break
        except PermissionError:
            if attempt == 9:
                raise
            # Windows scanners can retain a just-closed ZIP briefly; never discard
            # the validated replacement or overwrite an open archive in place.
            time.sleep(0.2 * (attempt + 1))
    return dict(
        file=destination.name,
        sha256=hashlib.sha256(destination.read_bytes()).hexdigest(),
        bytes=destination.stat().st_size,
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--refresh-reports", action="store_true")
    args = parser.parse_args()
    args.destination.mkdir(parents=True, exist_ok=True)
    scenarios = []
    manifests = []
    for case in sorted(args.source.iterdir()):
        if not case.is_dir() or case.name == "instrumentation-setup-run":
            continue
        if (case / "QA_EXECUTIONS.json").exists():
            summary = summarize(case)
            scenarios.append(summary)
            (args.destination / f"{case.name}-summary.json").write_text(
                json.dumps(summary, indent=2) + "\n"
            )
        pack = refresh_archive if args.refresh_reports else archive
        manifests.append(pack(case, args.destination / f"{case.name}.zip"))
    (args.destination / "ARCHIVE_SHA256.json").write_text(json.dumps(manifests, indent=2) + "\n")
    (args.destination / "QA_ALL_SCENARIOS.json").write_text(json.dumps(scenarios, indent=2) + "\n")
    primary = args.source / "primary"
    review = args.destination / "primary"
    review.mkdir(exist_ok=True)
    for path in primary.glob("QA_*"):
        if path.is_file():
            shutil.copyfile(path, review / path.name)
    rendered, diffs, metrics, publications = render(primary)
    (review / "QA_TEAM_REPORT.html").write_text(rendered, encoding="utf-8")
    (review / "QA_DERIVED_DIFFS.json").write_text(json.dumps(diffs, indent=2) + "\n")
    shutil.copyfile(primary / "QA_METRICS.json", review / "QA_RAW_METRICS.json")
    (review / "QA_METRICS.json").write_text(json.dumps(metrics, indent=2) + "\n")
    (review / "QA_PUBLICATION_AUDIT.json").write_text(json.dumps(publications, indent=2) + "\n")
    rows = "".join(
        f"<tr><td>{html.escape(s['scenario'])}</td><td>{s['activated']}/{s['cycles']}</td>"
        f"<td>{s['bookings']}</td><td>{s['matrix_max_nodes']}</td>"
        f'<td><a href="{html.escape(s["scenario"])}.zip">Complete evidence ZIP</a></td></tr>'
        for s in scenarios
    )
    (args.destination / "index.html").write_text(
        '<!doctype html><meta charset="utf-8"><title>Geographical QA evidence</title>'
        "<style>body{font:15px system-ui;margin:32px}td,th{padding:8px;"
        "border-bottom:1px solid #ddd;text-align:left}table{border-collapse:collapse}</style>"
        '<h1>Recorded geographical QA</h1><p><a href="primary/QA_TEAM_REPORT.html">'
        "Open the self-contained primary graphical report</a>.</p><p>Every ZIP retains the "
        "complete structured directory, raw runtime observations, real road responses, "
        "submitted CP models and problem witnesses. Extract it to open its own graphical "
        "report. Archive hashes are in ARCHIVE_SHA256.json. Node counts below are measured "
        "from persisted matrix inputs, not configured limits.</p><table><tr><th>Scenario</th>"
        "<th>Activated cycles</th><th>Paid bookings</th><th>Max matrix nodes</th>"
        "<th>Evidence</th></tr>" + rows + "</table>"
    )
    print(
        json.dumps(
            dict(
                scenarios=len(scenarios),
                cycles=sum(s["cycles"] for s in scenarios),
                bookings=sum(s["bookings"] for s in scenarios),
                archive_bytes=sum(m["bytes"] for m in manifests),
            )
        )
    )


if __name__ == "__main__":
    main()
