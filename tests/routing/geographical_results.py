"""Aggregate executed QA evidence; missing artifacts fail rather than become zeroes."""

import hashlib
import json
from pathlib import Path


def main():
    root = Path(__file__).parents[2]
    directory = root / "docs/benchmarks"

    def read(name):
        return json.loads((directory / name).read_text(encoding="utf-8-sig"))

    results = []
    for campaign in ("Control", "A", "B", "C", "D", "E"):
        result = read(f"GEOGRAPHICAL_QA_{campaign}.json")
        runtime = read(f"GEOGRAPHICAL_QA_{campaign}.runtime.json")
        assert result["campaign"] == campaign
        result.pop("geography")
        result.pop("queue", None)
        # Logical ID-pair probes and physical-coordinate cache hits have
        # different denominators. Never manufacture a ratio between them.
        result["logical_id_pair_entry_hits_at_call"] = (
            result["metrics"]["requested_arcs"] - result["metrics"]["cache_misses_at_call"]
        )
        result["runtime"] = runtime
        results.append(result)
    verification = read("GEOGRAPHICAL_QA_VERIFICATION.json")
    quality = read("GEOGRAPHICAL_QA_QUALITY.json")
    manifest = read("GEOGRAPHICAL_QA_MANIFEST.json")
    fixture = read("GEOGRAPHICAL_QA_FIXTURE.json")
    original = read("COMMERCIAL_ADMISSION_SCALABILITY_RESULTS.json")
    manifest["pinned_inputs"] = {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in ("tests/routing/data/delhi-road.osm.pbf", "tests/routing/data/qa-topology.osm")
    }
    manifest["profile_source_image"] = (
        "ghcr.io/project-osrm/osrm-backend@sha256:"
        "729461bcc9ae9e6aafa92c0f93db9b060a32e85d5e72092c01ae4a4a9f1eb564"
    )
    manifest["profile_script_sha256"] = dict(
        car="9809a111a8fc8c465c2d6ef3208938a2a7c35fab0d3cda407ffa7ff187a96477",
        bicycle="6ff0bb0735c3651c437e9fc730bc42fde5b138faa357616575bdbb11a408626a",
    )
    manifest["images"] = dict(
        native="sha256:c0422f71d51d3a41363086fc6ecc5957ef67921266b9907d698872d587cc6604",
        postgres="sha256:2ed748fc602dd3031c6724db8cb289e1578c2deb552a4f6e291f6f7e5e6e4f69",
        quality="sha256:615e24c1638ab8d491ae007921dd19fa6e43454a409c630c58c4bbf7760d4971",
    )
    manifest["postgres_image_reference"] = (
        "postgis/postgis@sha256:01a6a70e41e6c4467c8f55f6063555ed72db2d6662cd0d571040d42eadaeb6f6"
    )
    # Preserve observed exact commands without reconstructing shell-quoted strings.
    manifest["campaign_execution_arguments"] = {
        r["campaign"]: r["runtime"]["docker_arguments"] for r in results
    }
    manifest["verification_command"] = verification["command"]
    manifest["verification_docker_arguments"] = read("GEOGRAPHICAL_QA_VERIFICATION.runtime.json")[
        "docker_arguments"
    ]
    manifest["dataset_attribution"] = (
        "Public road inputs © OpenStreetMap contributors, ODbL 1.0; "
        "provenance: tests/routing/data/README.md. qa-topology.osm is synthetic CC0."
    )
    manifest["scenario_configuration"] = dict(
        seed=fixture["seed"],
        vehicles=80,
        mass_capacity_g=10000,
        volume_capacity_ml=10000,
        pickup_seconds=180,
        discharge_seconds=60,
        concurrency=4,
        worker_quantum=1,
        worker_budget_seconds=600,
        mixed_worker_budget_seconds=180,
        application_cpus=2,
        application_memory_bytes=4294967296,
        database_cpus=2,
        database_memory_bytes=1073741824,
        osrm_cpus=2,
        osrm_memory_bytes=536870912,
        osrm_threads=1,
        paid_road_services=False,
        production_dispatch_enabled=False,
    )
    manifest["reproduction"] = [
        "./tests/routing/run-geographical-qa.ps1 -Setup -Budget 600 -MixedBudget 180",
        "See docs/GEOGRAPHICAL_SCALABILITY_QA.md for explicit native verification "
        "and history capture",
    ]
    (directory / "GEOGRAPHICAL_QA_MANIFEST.json").write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\n"
    )
    passed = (
        all(r["accepted"] == r["count"] for r in results)
        and verification["qualified"]
        and all(
            quality[k] == 0 for k in ("ruff", "full_repository_format", "python_format", "mypy")
        )
    )
    summary = dict(
        target_commit=manifest["target_commit"],
        qa_branch=manifest["branch"],
        qualification="PASS_WITHIN_CLIPPED_FIXTURE_ONLY" if passed else "FAILED",
        campaigns=results,
        verification=verification,
        initial_verification=read("GEOGRAPHICAL_QA_VERIFICATION_INITIAL.json"),
        quality=quality,
        future_window_comparison=[read(f"GEOGRAPHICAL_QA_WINDOW_{n}.json") for n in (0, 1)],
        custody=read("GEOGRAPHICAL_QA_CUSTODY.json"),
        durable_negative=read("GEOGRAPHICAL_QA_DURABLE_NEGATIVE.json"),
        original_benchmark=original["final_campaign"],
        metric_definitions=dict(
            cache_misses_at_call="logical requested node-ID pairs with absent coordinate entry",
            logical_id_pair_entry_hits_at_call="same logical ID-pair denominator as misses",
            road_cache_hits="production counter after physical-coordinate deduplication",
            pending="right-censored at budget, includes unknown; not proven infeasible",
        ),
    )
    (directory / "GEOGRAPHICAL_QA_RESULTS.json").write_text(
        json.dumps(summary, sort_keys=True, indent=2) + "\n"
    )
    print("Compiled six executed campaigns and explicit qualification failures")


if __name__ == "__main__":
    main()
