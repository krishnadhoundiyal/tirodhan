"""Small staged-release CLI; Terraform remains the sole owner of revision configuration."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from typing import Any

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
    from tirodhan.deployment.prerequisites import require_provisioning, revalidate_database

    grant = require_provisioning(f"{mode}-plan")
    assert_no_list_keys()  # Before the provider can refresh storage state.
    state = output_values()
    available = enabled_secret_names(state)
    require_secrets(available, MIGRATION_SECRET_NAMES)
    image, sidecar = os.environ["APPLICATION_IMAGE"], os.environ["FLUENT_BIT_IMAGE"]
    values = migration_variables(state, image, sidecar)
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
    subprocess.run(arguments, env=environment, check=True)
    data = json.loads(
        subprocess.run(
            ["terraform", "show", "-json", path], check=True, capture_output=True, text=True
        ).stdout
    )
    if populated_key_paths(data):
        raise ReleaseBlocked("Deployment plan contains populated storage credential attributes")


def run_migration() -> None:
    from tirodhan.deployment.prerequisites import require_provisioning, revalidate_database

    grant = require_provisioning("migration-start")
    state = output_values()
    group, job = state["resource_group_name"], state["migration_job_name"]
    if not job:
        raise ReleaseBlocked("Migration job must be applied first")
    revalidate_database(grant)
    execution = cli_json(["az", "containerapp", "job", "start", "-g", group, "-n", job])
    name = execution["name"]
    deadline = time.monotonic() + 1200
    while time.monotonic() < deadline:
        executions = cli_json(
            ["az", "containerapp", "job", "execution", "list", "-g", group, "-n", job]
        )
        status = next(
            (item["properties"]["status"] for item in executions if item["name"] == name), None
        )
        if status == "Succeeded":
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
