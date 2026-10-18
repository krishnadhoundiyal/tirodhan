"""Finite QA pytest runner: record solver resources and FAIL on missing/skipped dependencies."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path


def run(output, selection):
    assert os.getenv("TIRODHAN_GEO_QA") == "1"
    assert os.getenv("TIRODHAN_ROUTING_ENGINE_TESTS") == "1"
    assert os.getenv("TIRODHAN_TEST_DATABASE_URL"), "Mandatory PostgreSQL missing"
    assert os.getenv("TIRODHAN_OSRM_TEST_URL"), "Mandatory OSRM missing"
    assert os.getenv("TIRODHAN_OSRM_GRAPH_VERSION"), "Mandatory road graph provenance missing"
    assert shutil.which("vroom") and shutil.which("osrm-routed"), (
        "Mandatory native binaries missing"
    )
    import ortools

    assert ortools.__version__ == "9.14.6206"
    stamp = time.perf_counter()
    peaks, cpu_ticks, observed = defaultdict(int), {}, set()
    with tempfile.TemporaryDirectory(prefix="tirodhan-geographical-qa-") as tmp:
        junit, log = Path(tmp) / "junit.xml", Path(tmp) / "pytest.log"
        command = [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-s",
            "-p",
            "no:cacheprovider",
            f"--junitxml={junit}",
            *selection,
        ]
        with log.open("wb") as stream:
            child = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT)
            while child.poll() is None:
                totals = defaultdict(int)
                for status in Path("/proc").glob("[0-9]*/status"):
                    try:
                        pid = int(status.parent.name)
                        if pid == os.getpid():
                            continue
                        content = status.read_text()
                        cmdline = (status.parent / "cmdline").read_bytes().decode(errors="replace")
                        label = (
                            "ortools"
                            if "ortools_worker" in cmdline
                            else (
                                "vroom"
                                if cmdline.split("\0")[0].endswith("vroom")
                                else "application"
                            )
                        )
                        rss = int(content.split("VmRSS:")[1].split()[0]) * 1024
                        totals[label] += rss
                        ticks = (status.parent / "stat").read_text().split(") ", 1)[1].split()
                        cpu_ticks[(label, pid)] = int(ticks[11]) + int(ticks[12])
                        observed.add(label)
                    except (FileNotFoundError, ProcessLookupError, IndexError, PermissionError):
                        continue
                for label, total in totals.items():
                    peaks[label] = max(peaks[label], total)
                time.sleep(0.02)
        root = ET.parse(junit).getroot()
        counts = {
            k: sum(int(s.get(k, 0)) for s in root.iter("testsuite"))
            for k in ("tests", "failures", "errors", "skipped")
        }
        cpu = {
            label: sum(t for (kind, _), t in cpu_ticks.items() if kind == label)
            / os.sysconf("SC_CLK_TCK")
            for label in observed
        }
        result = dict(
            command=command,
            elapsed_seconds=time.perf_counter() - stamp,
            exit_code=child.returncode,
            counts=counts,
            sampled_peak_rss_bytes=dict(peaks),
            sampled_cpu_seconds=cpu,
            resource_qualification="20ms samples; short-lived processes may be missed",
            qualified=child.returncode == 0 and counts["skipped"] == 0,
        )
        output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
        print(log.read_text())
        print("QA_VERIFICATION_RESULT=" + json.dumps(result, sort_keys=True))
    return 0 if result["qualified"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("selection", nargs="+")
    args = parser.parse_args()
    raise SystemExit(run(args.output, args.selection))
