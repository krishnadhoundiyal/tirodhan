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


def output_values() -> dict[str, Any]:
    result = subprocess.run(
        ["terraform", "output", "-json"], check=True, capture_output=True, text=True
    )
    return {key: item["value"] for key, item in json.loads(result.stdout).items()}


def migration_variables(state: dict[str, Any], image: str, sidecar: str) -> dict[str, Any]:
    if "deployment_stage" not in state:
        raise RuntimeError("Apply foundation, bootstrap PostgreSQL and seed secrets first")
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


def check_secret_metadata(state: dict[str, Any]) -> None:
    metadata = cli_json(
        ["az", "keyvault", "secret", "list", "--vault-name", state["key_vault_name"]]
    )
    available = {item["name"] for item in metadata if item["attributes"].get("enabled", True)}
    if not (SECRET_NAMES | {"fluent-bit-sas"}).issubset(available):
        raise RuntimeError("Required enabled Key Vault secrets are missing; run controlled seeding")


def plan(mode: str, path: str) -> None:
    assert_no_list_keys()  # Before the provider can refresh storage state.
    state = output_values()
    check_secret_metadata(state)
    image, sidecar = os.environ["APPLICATION_IMAGE"], os.environ["FLUENT_BIT_IMAGE"]
    values = migration_variables(state, image, sidecar)
    if mode == "runtime":
        values.update(deployment_stage=4, application_image=image, fluent_bit_image=sidecar)
    environment = os.environ | {
        f"TF_VAR_{key}": json.dumps(value) if value is None else str(value)
        for key, value in values.items()
        if value is not None
    }
    # null default means no established application image at initial stage 0/1.
    subprocess.run(
        ["terraform", "plan", "-input=false", f"-out={path}"], env=environment, check=True
    )
    data = json.loads(
        subprocess.run(
            ["terraform", "show", "-json", path], check=True, capture_output=True, text=True
        ).stdout
    )
    if populated_key_paths(data):
        raise RuntimeError("Deployment plan contains populated storage credential attributes")


def run_migration() -> None:
    state = output_values()
    group, job = state["resource_group_name"], state["migration_job_name"]
    if not job:
        raise RuntimeError("Migration job must be applied first")
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
            raise RuntimeError("Migration job did not succeed; runtime rollout is blocked")
        time.sleep(10)
    raise RuntimeError("Migration completion timed out; runtime rollout is blocked")


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
    main()
