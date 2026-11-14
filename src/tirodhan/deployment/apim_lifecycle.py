"""Plan a gateway addition without reconciling the deployed application."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tirodhan.deployment.check_state import populated_key_paths
from tirodhan.deployment.deployment_admission import AdmissionDenied
from tirodhan.deployment.lifecycle_evidence import BackendState, Observations, plan_prior_state
from tirodhan.deployment.release import MIGRATION_SECRET_NAMES, RUNTIME_SECRET_NAMES

if TYPE_CHECKING:
    from tirodhan.deployment.lifecycle_checks import Checks

ADDRESS = "azurerm_api_management.nonprod[0]"
API_VERSION = "2024-05-01"
WORKFLOW_REF = "krishnadhoundiyal/tirodhan/.github/workflows/nonprod-apim.yml@refs/heads/main"
# AzureRM 5.8.0 allows three hours for APIM creation. Leave time for Terraform
# to persist state and release the backend lease before the runner stops it.
APIM_APPLY_TIMEOUT_SECONDS = 190 * 60


def gateway_id(io: Observations) -> str:
    return (
        f"{io.scope}/providers/Microsoft.ApiManagement/service/{io.tf['resource_group_name']}-apim"
    )


def deployed_inputs(state: BackendState) -> dict[str, Any]:
    document = state.document or {}
    outputs = document.get("outputs", {})
    stage = outputs.get("deployment_stage", {}).get("value")
    if state.kind != "INITIALIZED" or type(stage) is not int or stage != 4:
        raise AdmissionDenied("APIM_REQUIRES_STAGE_FOUR")
    values: dict[str, Any] = {"deployment_stage": stage}
    for key in ("application_image", "migration_image", "fluent_bit_image"):
        image = outputs.get(key, {}).get("value")
        if not isinstance(image, str) or not image:
            raise AdmissionDenied("APIM_DEPLOYED_IMAGE_UNVERIFIED")
        values[key] = image

    def attributes(kind: str, name: str) -> dict[str, Any]:
        matching = [
            instance["attributes"]
            for row in document["resources"]
            if (row["mode"], row.get("module"), row["type"], row["name"])
            == ("managed", None, kind, name)
            for instance in row["instances"]
            if instance.get("index_key") == 0
        ]
        if len(matching) != 1:
            raise AdmissionDenied("APIM_DEPLOYED_WORKLOAD_UNVERIFIED")
        return dict(matching[0])

    api = attributes("azurerm_container_app", "api")
    secrets = [item["name"] for item in api["secret"]]
    names = set(secrets)
    if (
        len(names) != len(secrets)
        or not MIGRATION_SECRET_NAMES.issubset(names)
        or not names.issubset(MIGRATION_SECRET_NAMES | RUNTIME_SECRET_NAMES)
    ):
        raise AdmissionDenied("APIM_DEPLOYED_SECRET_REFERENCES_UNVERIFIED")
    values["enabled_runtime_secret_names"] = sorted(names - MIGRATION_SECRET_NAMES)

    job = attributes("azurerm_container_app_job", "migration")
    keys = [
        item["value"]
        for container in job["template"][0]["container"]
        if container["name"] == "application"
        for item in container["env"]
        if item["name"] == "TIRODHAN_MIGRATION_OPERATION_KEY"
    ]
    if len(keys) != 1 or not isinstance(keys[0], str) or not re.fullmatch("[a-f0-9]{64}", keys[0]):
        raise AdmissionDenied("APIM_DEPLOYED_MIGRATION_CONTEXT_UNVERIFIED")
    values["migration_operation_key"] = keys[0]
    return values


def check_baseline(checks: Checks) -> bool:
    if os.environ.get("GITHUB_WORKFLOW_REF") != WORKFLOW_REF or not checks.tf.get(
        "apim_publisher_email"
    ):
        return False
    state = checks.backend()
    deployed_inputs(state)
    return (
        ADDRESS not in state.resources and checks.io.arm(gateway_id(checks.io), API_VERSION) is None
    )


def validate_apim_plan(
    plan: dict[str, Any], state: BackendState, terraform: dict[str, Any]
) -> None:
    """Reject drift, imports, moves, replacements and every unrelated planned change."""
    inputs = deployed_inputs(state)
    variables = {name: item["value"] for name, item in plan["variables"].items()}
    if variables.get("deployment_stage") == "4":
        variables["deployment_stage"] = 4
    managed = [row for row in plan["resource_changes"] if row["mode"] == "managed"]
    changes = [row for row in managed if row["change"]["actions"] != ["no-op"]]
    if (
        any(variables.get(name) != value for name, value in (terraform | inputs).items())
        or plan.get("complete") is not True
        or plan.get("errored") is not False
        or plan.get("applyable") is not True
        or any(
            plan.get(field)
            for field in ("resource_drift", "deferred_changes", "action_invocations")
        )
        or populated_key_paths(plan)
        or any(
            "previous_address" in row or "deposed" in row or row["change"].get("importing")
            for row in managed
        )
        or len(changes) != 1
    ):
        raise AdmissionDenied("APIM_PLAN_NOT_ADDITIVE")
    gateway = changes[0]
    change = gateway["change"]
    outputs = plan.get("output_changes", {})
    expected = {
        "name": terraform["resource_group_name"] + "-apim",
        "resource_group_name": terraform["resource_group_name"],
        "location": terraform["location"],
        "publisher_email": terraform["apim_publisher_email"],
        "sku_name": "Consumption_0",
    }
    if (
        ADDRESS in state.resources
        or gateway["address"] != ADDRESS
        or gateway["type"] != "azurerm_api_management"
        or change["actions"] != ["create"]
        or change["before"] is not None
        or any(change["after"].get(key) != value for key, value in expected.items())
        or outputs.get("apim_gateway_url", {}).get("actions") != ["create"]
        or any(
            row["actions"] != ["no-op"]
            for name, row in outputs.items()
            if name != "apim_gateway_url"
        )
    ):
        raise AdmissionDenied("APIM_PLAN_NOT_ADDITIVE")


def plan_gateway(path: Path) -> None:
    from tirodhan.deployment.foundation_reconcile import admitted_with_grant
    from tirodhan.deployment.prerequisites import revalidate_database

    checks, io, grant = admitted_with_grant("apim-plan")
    before = checks.backend()
    values = deployed_inputs(before)
    arguments = [
        f"-var={key}={json.dumps(value) if isinstance(value, list) else value}"
        for key, value in values.items()
    ]
    revalidate_database(grant)
    try:
        subprocess.run(
            ["terraform", "plan", "-input=false", f"-out={path}", *arguments],
            check=True,
            capture_output=True,
            timeout=1800,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AdmissionDenied("APIM_TERRAFORM_PLAN_FAILED") from exc
    after = io.backend()
    prior = plan_prior_state(path)
    if before.fingerprint != after.fingerprint or any(
        prior.get(key) != (before.document or {}).get(key) for key in ("lineage", "serial")
    ):
        raise AdmissionDenied("APIM_BACKEND_CHANGED_DURING_PLAN")
    validate_apim_plan(io.runner.json("terraform", "show", "-json", str(path)), before, checks.tf)
    print("APIM_PLAN_VERIFIED: one Consumption gateway create")


def verify_gateway(io: Observations, before: BackendState) -> None:
    after = io.backend()
    expected_id = gateway_id(io)
    live = io.arm(expected_id, API_VERSION) or {}
    sku = live.get("sku", {})
    before_outputs = dict((before.document or {}).get("outputs", {}))
    after_outputs = dict((after.document or {}).get("outputs", {}))
    gateway_url = after_outputs.pop("apim_gateway_url", {}).get("value")
    before_outputs.pop("apim_gateway_url", None)
    before_state, after_state = before.document or {}, after.document or {}
    if (
        after.kind != "INITIALIZED"
        or after_state.get("lineage") != before_state.get("lineage")
        or after_state["serial"] <= before_state["serial"]
        or {key: value.casefold() for key, value in after.resources.items()}
        != {
            key: value.casefold()
            for key, value in (before.resources | {ADDRESS: expected_id}).items()
        }
        or before_outputs != after_outputs
        or not gateway_url
        or live.get("properties", {}).get("gatewayUrl") != gateway_url
        or str(live.get("id", "")).casefold() != expected_id.casefold()
        or sku.get("name") != "Consumption"
        or sku.get("capacity") != 0
        or live.get("properties", {}).get("provisioningState") != "Succeeded"
    ):
        raise AdmissionDenied("APIM_POSTCONDITIONS_FAILED")
