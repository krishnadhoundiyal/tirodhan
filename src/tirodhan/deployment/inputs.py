"""Materialize nonsecret environment configuration for trusted deployment runners."""

from __future__ import annotations

import json
import os
from pathlib import Path

from tirodhan.core.config import Settings
from tirodhan.deployment.runtime_prerequisites import validate_optional_runtime_configuration


def main() -> None:
    values = json.loads(os.environ["NONPROD_TFVARS_JSON"])
    allowed = {
        "subscription_id",
        "tenant_id",
        "location",
        "resource_group_name",
        "suffix",
        "postgres_admin_object_id",
        "postgres_admin_name",
        "postgres_admin_type",
        "postgres_version",
        "postgres_bootstrap_ipv4",
        "ghcr_username",
        "runtime_env",
        "blob_soft_delete_days",
        "media_cool_after_days",
        "media_delete_after_days",
        "logs_delete_after_days",
        "job_schedules",
        "financial_inventory_schedule",
        "customer_notifications_schedule",
        "routing_availability_schedule",
    }
    if not isinstance(values, dict) or not set(values).issubset(allowed):
        raise RuntimeError(
            "Only approved nonsecret inputs are accepted; images/stage are release-owned"
        )
    names = set(Path("../../../deploy/runtime-env.names").read_text().splitlines())
    runtime = values.get("runtime_env", {})
    if (
        not isinstance(runtime, dict)
        or not set(runtime).issubset(names)
        or any(not isinstance(value, str) for value in runtime.values())
    ):
        raise RuntimeError("Runtime inputs must be approved nonsecret setting names")
    # Cleanup/recovery must be able to materialize the same authoritative inputs
    # even when an optional workload is misconfigured. This grants no operation:
    # signed live plan/apply admission independently validates workload policies.
    maintenance_workflows = {
        "krishnadhoundiyal/tirodhan/.github/workflows/nonprod-destroy.yml@refs/heads/main",
        "krishnadhoundiyal/tirodhan/.github/workflows/nonprod-reconcile.yml@refs/heads/main",
    }
    if os.environ.get("GITHUB_WORKFLOW_REF") not in maintenance_workflows:
        os.environ.update(runtime)
        Settings()  # CI has no .env; use the existing runtime validators.
        validate_optional_runtime_configuration(values)
    Path("nonprod.auto.tfvars.json").write_text(json.dumps(values), encoding="utf-8")


if __name__ == "__main__":
    main()
