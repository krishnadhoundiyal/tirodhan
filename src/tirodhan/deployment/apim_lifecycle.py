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
from tirodhan.deployment.lifecycle_evidence import (
    BackendState,
    ObservationError,
    Observations,
    plan_prior_state,
)
from tirodhan.deployment.release import MIGRATION_SECRET_NAMES, RUNTIME_SECRET_NAMES

if TYPE_CHECKING:
    from tirodhan.deployment.lifecycle_checks import Checks

ADDRESS = "azurerm_api_management.nonprod[0]"
API_VERSION = "2024-05-01"
WORKFLOW_REF = "krishnadhoundiyal/tirodhan/.github/workflows/nonprod-apim.yml@refs/heads/main"
# AzureRM 5.8.0 allows three hours for APIM creation. Leave time for Terraform
# to persist state and release the backend lease before the runner stops it.
APIM_APPLY_TIMEOUT_SECONDS = 190 * 60


class ApimPlanBlocked(AdmissionDenied):
    """A locally generated APIM diagnostic safe for the workflow log."""


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


def empty_container_command_refresh(row: dict[str, Any], state: BackendState) -> bool:
    """Recognize AzureRM 5.8.0's serialization of omitted command lists."""
    address = row.get("address")
    if (
        not isinstance(address, str)
        or address not in state.resources
        or row.get("mode") != "managed"
        or row.get("provider_name") != "registry.terraform.io/hashicorp/azurerm"
        or "previous_address" in row
        or "deposed" in row
    ):
        return False

    block = address.split("[", 1)[0]
    if (
        block
        not in (
            "azurerm_container_app.api",
            "azurerm_container_app.worker",
            "azurerm_container_app_job.scheduled",
        )
        or row.get("type") != block.split(".", 1)[0]
    ):
        return False

    change = row.get("change")
    if (
        not isinstance(change, dict)
        or change.get("actions") != ["update"]
        or change.get("after_unknown") != {}
        or change.get("importing")
        or change.get("replace_paths")
    ):
        return False
    before, after = change.get("before"), change.get("after")
    if (
        not isinstance(before, dict)
        or not isinstance(after, dict)
        or before.get("id") != state.resources[address]
    ):
        return False

    template = before.get("template")
    if not isinstance(template, list) or len(template) != 1 or not isinstance(template[0], dict):
        return False
    containers = template[0].get("container")
    if (
        not isinstance(containers, list)
        or len(containers) != 2
        or any(not isinstance(container, dict) for container in containers)
        or [container.get("name") for container in containers] != ["application", "fluent-bit"]
    ):
        return False

    application, sidecar = (dict(container) for container in containers)
    if "command" in sidecar and sidecar["command"] is None:
        sidecar["command"] = []
    if address == "azurerm_container_app.api[0]":
        for field in ("args", "command"):
            if field in application and application[field] is None:
                application[field] = []
    expected = before | {"template": [template[0] | {"container": [application, sidecar]}]}
    return expected != before and json.dumps(expected, sort_keys=True) == json.dumps(
        after, sort_keys=True
    )


def validate_apim_plan(
    plan: dict[str, Any], state: BackendState, terraform: dict[str, Any]
) -> None:
    """Reject material drift and every planned action except the gateway create."""
    inputs = deployed_inputs(state)
    variables = {name: item["value"] for name, item in plan["variables"].items()}
    if variables.get("deployment_stage") == "4":
        variables["deployment_stage"] = 4
    managed = [row for row in plan["resource_changes"] if row["mode"] == "managed"]
    changes = [row for row in managed if row["change"]["actions"] != ["no-op"]]
    if any(variables.get(name) != value for name, value in (terraform | inputs).items()):
        raise ApimPlanBlocked("APIM_PLAN_INPUT_MISMATCH")
    if plan.get("complete") is not True or plan.get("errored") is not False:
        raise ApimPlanBlocked("APIM_PLAN_INCOMPLETE")
    drift = plan.get("resource_drift", [])
    if not isinstance(drift, list) or any(
        not isinstance(row, dict) or not empty_container_command_refresh(row, state)
        for row in drift
    ):
        raise ApimPlanBlocked("APIM_PLAN_RESOURCE_DRIFT")
    if populated_key_paths(plan):
        raise ApimPlanBlocked("APIM_PLAN_SECRET_HYGIENE_FAILED")
    if (
        plan.get("applyable") is not True
        or plan.get("deferred_changes")
        or plan.get("action_invocations")
        or any(
            "previous_address" in row or "deposed" in row or row["change"].get("importing")
            for row in managed
        )
        or len(changes) != 1
    ):
        raise ApimPlanBlocked("APIM_PLAN_NOT_ADDITIVE")
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
    ):
        raise ApimPlanBlocked("APIM_PLAN_GATEWAY_MISMATCH")
    if outputs.get("apim_gateway_url", {}).get("actions") != ["create"] or any(
        row["actions"] != ["no-op"] for name, row in outputs.items() if name != "apim_gateway_url"
    ):
        raise ApimPlanBlocked("APIM_PLAN_OUTPUT_MISMATCH")


def drift_summary(drift: Any, state: BackendState) -> str:
    """Report existing Terraform block labels and static schema fields only."""
    if not isinstance(drift, list):
        return "APIM_PLAN_DRIFT_DETAIL_UNAVAILABLE"

    blocks: dict[str, set[str]] = {}
    unmatched = 0
    for row in drift:
        address = row.get("address") if isinstance(row, dict) else None
        if not isinstance(address, str) or address not in state.resources:
            unmatched += 1
            continue
        # for_each keys are values. Never copy them into a public workflow log.
        label = address.split("[", 1)[0]
        if not re.fullmatch(r"azurerm_[a-z0-9_]+\.[a-zA-Z0-9_]+", label):
            unmatched += 1
            continue
        fields = blocks.setdefault(label, set())
        change = row.get("change")
        if isinstance(change, dict):
            before, after = change.get("before"), change.get("after")
            if isinstance(before, dict) and isinstance(after, dict):
                # Terraform show encodes resource objects using the provider schema.
                # Only root field names are static. Nested keys can be user input.
                fields.update(
                    key
                    for key in before.keys() | after.keys()
                    if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key)
                    and before.get(key) != after.get(key)
                )

    details = []
    for label, fields in sorted(blocks.items())[:12]:
        shown = ",".join(sorted(fields)[:12])
        if len(fields) > 12:
            shown += ",more"
        details.append(f"{label}({shown})" if shown else label)
    return (
        f"APIM_PLAN_DRIFT_DETAIL entries={len(drift)} unlisted={unmatched} "
        f"omitted_blocks={max(0, len(blocks) - 12)} blocks={';'.join(details)}"
    )


def terraform_plan_failure(output: str) -> str:
    """Return a fixed status label without forwarding provider diagnostics."""
    labels: set[str] = set()
    for line in output.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict) or record.get("type") != "diagnostic":
            continue
        diagnostic = record.get("diagnostic")
        if not isinstance(diagnostic, dict) or diagnostic.get("severity") != "error":
            continue
        summary = diagnostic.get("summary")
        detail = diagnostic.get("detail")
        if summary == "Error acquiring the state lock":
            labels.add("STATE_LOCK")
        elif summary in ("Invalid value for input variable", "Invalid function argument"):
            labels.add("INVALID_INPUT")
        elif any(
            isinstance(message, str)
            and re.search(r"\b(?:AuthorizationFailed|ForbiddenByRbac)\b", message)
            for message in (summary, detail)
        ):
            labels.add("AZURE_PERMISSION")
        else:
            labels.add("OTHER")
    return "APIM_TERRAFORM_PLAN_FAILED" + (":" + ",".join(sorted(labels)) if labels else "")


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
            ["terraform", "plan", "-input=false", f"-out={path}", "-json", *arguments],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=1800,
        )
    except subprocess.TimeoutExpired:
        raise ApimPlanBlocked("APIM_TERRAFORM_PLAN_TIMED_OUT") from None
    except subprocess.CalledProcessError as exc:
        raise ApimPlanBlocked(terraform_plan_failure(exc.stdout or "")) from None
    except OSError:
        raise ApimPlanBlocked("APIM_TERRAFORM_PLAN_UNAVAILABLE") from None
    try:
        after = io.backend()
    except ObservationError:
        raise ApimPlanBlocked("APIM_POST_PLAN_BACKEND_UNAVAILABLE") from None
    try:
        prior = plan_prior_state(path)
        shown = io.runner.json("terraform", "show", "-json", str(path))
    except ObservationError:
        raise ApimPlanBlocked("APIM_SAVED_PLAN_UNAVAILABLE") from None
    # Native Azure backend lock/unlock writes blob metadata, changing its ETag
    # even when the state contents are untouched. Keep the lock and compare
    # state contents here. Apply binds the post-plan ETag separately.
    if (
        after.kind != before.kind
        or after.document != before.document
        or any(prior.get(key) != (before.document or {}).get(key) for key in ("lineage", "serial"))
    ):
        raise ApimPlanBlocked("APIM_BACKEND_CHANGED_DURING_PLAN")
    try:
        validate_apim_plan(shown, before, checks.tf)
    except ApimPlanBlocked as exc:
        if str(exc) == "APIM_PLAN_RESOURCE_DRIFT":
            print(drift_summary(shown.get("resource_drift"), before), flush=True)
        raise
    except (KeyError, TypeError, ValueError):
        raise ApimPlanBlocked("APIM_SAVED_PLAN_FORMAT_INVALID") from None
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
