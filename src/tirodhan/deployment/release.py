"""Small staged-release CLI; Terraform remains the sole owner of revision configuration."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from tirodhan.deployment.azure_controls import assert_no_list_keys
from tirodhan.deployment.check_state import populated_key_paths
from tirodhan.deployment.seed_secrets import SECRET_NAMES


class ReleaseBlocked(RuntimeError):
    """An operator-actionable message containing only controlled deployment identifiers."""


MIGRATION_SECRET_NAMES = frozenset({"ghcr-pull-pat", "fluent-bit-sas"})
RUNTIME_SECRET_NAMES = SECRET_NAMES - MIGRATION_SECRET_NAMES


def output_values() -> dict[str, Any]:
    result = subprocess.run(
        ["terraform", "output", "-json"], check=True, capture_output=True, text=True
    )
    return {key: item["value"] for key, item in json.loads(result.stdout).items()}


def migration_variables(state: dict[str, Any], image: str, sidecar: str) -> dict[str, Any]:
    if "deployment_stage" not in state:
        raise ReleaseBlocked("Apply foundation and bootstrap PostgreSQL first")
    return {
        "deployment_stage": max(1, int(state["deployment_stage"])),
        "migration_image": image,
        # Never revert/destroy existing workloads while upgrading the schema.
        "application_image": state.get("application_image"),
        "fluent_bit_image": state.get("fluent_bit_image") or sidecar,
    }


def cli_json(command: list[str]) -> Any:
    return json.loads(
        subprocess.run(command + ["-o", "json"], check=True, capture_output=True, text=True).stdout
    )


def enabled_secret_names(state: dict[str, Any]) -> frozenset[str]:
    metadata = cli_json(
        ["az", "keyvault", "secret", "list", "--vault-name", state["key_vault_name"]]
    )
    return frozenset(item["name"] for item in metadata if item["attributes"].get("enabled", True))


def runtime_secret_names(available: frozenset[str]) -> frozenset[str]:
    return available & RUNTIME_SECRET_NAMES


def require_secrets(available: frozenset[str], required: frozenset[str]) -> None:
    missing = required - available
    if missing:
        names = ", ".join(sorted(missing))
        raise ReleaseBlocked(f"Required enabled Key Vault secrets are missing: {names}")


def plan(mode: str, path: str) -> None:
    from tirodhan.deployment.deployment_admission import configuration
    from tirodhan.deployment.migration_start import expected_job_id, operation_key
    from tirodhan.deployment.prerequisites import require_provisioning, revalidate_database

    grant = require_provisioning(f"{mode}-plan")
    assert_no_list_keys()  # Before the provider can refresh storage state.
    state = output_values()
    available = enabled_secret_names(state)
    require_secrets(available, MIGRATION_SECRET_NAMES)
    image, sidecar = os.environ["APPLICATION_IMAGE"], os.environ["FLUENT_BIT_IMAGE"]
    values = migration_variables(state, image, sidecar)
    values["migration_operation_key"] = operation_key(
        grant.authorization, expected_job_id(configuration()), image
    )
    if mode == "runtime":
        values.update(
            deployment_stage=4,
            application_image=image,
            fluent_bit_image=sidecar,
            enabled_runtime_secret_names=json.dumps(sorted(runtime_secret_names(available))),
        )
    environment = os.environ | {
        f"TF_VAR_{key}": json.dumps(value) if value is None else str(value)
        for key, value in values.items()
        if value is not None
    }
    # null default means no established application image at initial stage 0/1.
    revalidate_database(grant)
    arguments = ["terraform", "plan", "-input=false", f"-out={path}"]
    if mode == "migration":
        # An established stage-4 stack must not reconcile running applications with
        # new inputs (or the empty default secret set) before migrations succeed.
        arguments.append("-target=azurerm_container_app_job.migration[0]")
    try:
        subprocess.run(arguments, env=environment, check=True, timeout=1800)
    except subprocess.TimeoutExpired:
        raise ReleaseBlocked("Terraform planning timed out; rollout is blocked") from None
    data = json.loads(
        subprocess.run(
            ["terraform", "show", "-json", path], check=True, capture_output=True, text=True
        ).stdout
    )
    if populated_key_paths(data):
        raise ReleaseBlocked("Deployment plan contains populated storage credential attributes")


def run_migration() -> None:
    from tirodhan.deployment.foundation_reconcile import admitted_with_grant
    from tirodhan.deployment.lifecycle_evidence import ObservationError
    from tirodhan.deployment.migration_evidence import (
        API_VERSION,
        execution_name,
        successful_execution,
    )
    from tirodhan.deployment.migration_start import (
        ClaimStore,
        belongs_to_release,
        operation_key,
        reconcile,
    )
    from tirodhan.deployment.prerequisites import revalidate_database

    checks, io, grant = admitted_with_grant("migration-start")
    job = checks.job()
    job_id = job["id"]
    image = os.environ["APPLICATION_IMAGE"]
    key = operation_key(checks.auth, job_id, image)
    revalidate_database(grant)
    name = None
    store = ClaimStore(io)
    try:
        owns_start = store.acquire(key, uuid4().hex)
    finally:
        store.close()
    if owns_start:
        revalidate_database(grant)  # Claim acquisition cannot extend stale SQL/authority.
        try:
            # No execution-template override: preserve native volumes/mounts/sidecars.
            execution = io.runner.json(
                "az",
                "rest",
                "--method",
                "post",
                "--url",
                "https://management.azure.com" + job_id + "/start?api-version=" + API_VERSION,
                "-o",
                "json",
            )
            if execution_name(execution.get("name")):
                name = execution["name"]
        except ObservationError:
            pass  # A lost response can only be resolved by actual execution evidence.
    deadline = time.monotonic() + 1200
    while time.monotonic() < deadline:
        if name is None:
            name = reconcile(io, job_id, key)
        if name is None:
            time.sleep(10)
            continue
        value = io.arm(job_id + "/executions/" + name, API_VERSION)
        status = value.get("properties", {}).get("status") if value else None
        if status == "Succeeded":
            if (
                value is None
                or not successful_execution(value, job_id, name, image)
                or not belongs_to_release(value, key)
            ):
                raise ReleaseBlocked("Migration execution template does not match this release")
            if output := os.environ.get("GITHUB_OUTPUT"):
                with Path(output).open("a", encoding="utf-8") as stream:
                    stream.write(f"migration_execution={name}\n")
            print("Migration and bounded log-sidecar execution succeeded")
            return
        if status in {"Failed", "Stopped"}:
            raise ReleaseBlocked("Migration job did not succeed; runtime rollout is blocked")
        time.sleep(10)
    raise ReleaseBlocked("Migration completion timed out; runtime rollout is blocked")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["migration-plan", "runtime-plan", "migrate"])
    parser.add_argument("--plan", default="release.tfplan")
    args = parser.parse_args()
    if args.operation == "migrate":
        run_migration()
    else:
        plan(args.operation.removesuffix("-plan"), args.plan)


if __name__ == "__main__":
    import sys

    from tirodhan.deployment.deployment_admission import (
        AdmissionDenied,
        require_release_authorization,
    )

    try:
        operation = (
            "migration-start"
            if "migrate" in sys.argv
            else "runtime-plan"
            if "runtime-plan" in sys.argv
            else "migration-plan"
        )
        require_release_authorization(operation)
        main()
    except AdmissionDenied as exc:
        raise SystemExit(str(exc)) from None
    except ReleaseBlocked as exc:
        raise SystemExit(f"RELEASE_UNVERIFIED: {exc}") from None
    except Exception:
        raise SystemExit("RELEASE_UNVERIFIED: rollout is blocked") from None
