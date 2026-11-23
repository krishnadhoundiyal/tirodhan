"""Plan and apply only the published NONPROD web workloads."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx

from tirodhan.deployment.check_state import populated_key_paths
from tirodhan.deployment.deployment_admission import AdmissionDenied
from tirodhan.deployment.lifecycle_evidence import BackendState, Observations, plan_prior_state

WEB_APP_ADDRESSES = {
    "customer": 'azurerm_container_app.web["customer"]',
    "manager": 'azurerm_container_app.web["manager"]',
}
WEB_SUPPORT_ADDRESSES = frozenset(
    {"azurerm_user_assigned_identity.web_pull[0]", "azurerm_role_assignment.web_pull[0]"}
)
WEB_ADDRESSES = WEB_SUPPORT_ADDRESSES | frozenset(WEB_APP_ADDRESSES.values())


def validate_web_plan(
    plan: dict[str, Any], state: BackendState, inputs: dict[str, Any]
) -> dict[str, list[str]]:
    images = inputs.get("nonprod_web_images")
    outputs = (state.document or {}).get("outputs", {})
    planned = plan.get("planned_values", {})
    if (
        not isinstance(images, dict)
        or set(images) != set(WEB_APP_ADDRESSES)
        or state.kind != "INITIALIZED"
        or outputs.get("deployment_stage", {}).get("value") != 4
        or plan.get("variables", {}).get("nonprod_web_images", {}).get("value") != images
        or populated_key_paths(plan)
    ):
        raise AdmissionDenied("WEB_PLAN_INPUTS_UNVERIFIED")
    for name in ("deployment_stage", "application_image", "migration_image", "fluent_bit_image"):
        if planned.get("outputs", {}).get(name, {}).get("value") != outputs[name]["value"]:
            raise AdmissionDenied("WEB_PLAN_CHANGED_BACKEND_OUTPUT")
    resources = {row["address"]: row for row in planned.get("root_module", {}).get("resources", [])}
    if not WEB_ADDRESSES.issubset(resources):
        raise AdmissionDenied("WEB_PLAN_WORKLOADS_MISSING")
    for app, address in WEB_APP_ADDRESSES.items():
        containers = resources[address]["values"]["template"][0]["container"]
        if len(containers) != 1 or containers[0].get("image") != images[app]:
            raise AdmissionDenied("WEB_PLAN_IMAGE_MISMATCH")
    changes: dict[str, list[str]] = {}
    for row in plan.get("resource_changes", []):
        if row.get("mode", "managed") != "managed" or row["change"]["actions"] == ["no-op"]:
            continue
        address, actions = row["address"], row["change"]["actions"]
        allowed = (
            ["update"]
            if address in state.resources and address in WEB_APP_ADDRESSES.values()
            else ["create"]
        )
        if (
            address not in WEB_ADDRESSES
            or actions != allowed
            or (address in WEB_SUPPORT_ADDRESSES and address in state.resources)
        ):
            raise AdmissionDenied("WEB_PLAN_OUTSIDE_WORKLOADS")
        changes[address] = actions
    return changes


def verify_web_apps(io: Observations) -> None:
    resources = io.backend().resources
    for app, address in WEB_APP_ADDRESSES.items():
        resource_id = resources.get(address, "")
        name = f"tirodhan-np-{io.tf['suffix']}-{app}-web"
        if (
            resource_id.casefold()
            != f"{io.scope}/providers/Microsoft.App/containerApps/{name}".casefold()
        ):
            raise AdmissionDenied(f"WEB_APP_TARGET_MISMATCH:{app}")
        for attempt in range(18):
            status = (io.arm(resource_id, "2025-07-01") or {}).get("properties", {})
            ingress = status.get("configuration", {}).get("ingress", {})
            fqdn = ingress.get("fqdn") or ""
            ready = status.get("latestReadyRevisionName")
            if (
                status.get("provisioningState") == "Succeeded"
                and ready
                and ready == status.get("latestRevisionName")
                and re.fullmatch(rf"{name}\.[a-z0-9.-]+\.azurecontainerapps\.io", fqdn)
            ):
                containers = status.get("template", {}).get("containers", [])
                if not any(
                    row.get("name") == "web"
                    and row.get("image") == io.tf["nonprod_web_images"][app]
                    for row in containers
                ):
                    raise AdmissionDenied(f"WEB_APP_IMAGE_MISMATCH:{app}")
                try:
                    response = httpx.get(f"https://{fqdn}/health", timeout=10)
                    if response.status_code == 200 and response.text.strip() == "ok":
                        print(f"WEB_APP_READY {app} https://{fqdn}")
                        break
                except httpx.RequestError:
                    pass  # A cold revision can take several probes to answer.
            if attempt < 17:
                time.sleep(10)
        else:
            raise AdmissionDenied(f"WEB_APP_READINESS_UNVERIFIED:{app}")


def plan_web(plan: Path) -> None:
    from tirodhan.deployment.foundation_reconcile import admitted_with_grant
    from tirodhan.deployment.release import runtime_only_variables, runtime_secret_names

    checks, io, _ = admitted_with_grant("web-plan")
    state = checks.backend()
    outputs = {
        name: value["value"] for name, value in (state.document or {}).get("outputs", {}).items()
    }
    if outputs.get("deployment_stage") != 4:
        raise AdmissionDenied("WEB_PLAN_REQUIRES_STAGE_FOUR")
    values = runtime_only_variables(outputs, checks.job(), outputs["application_image"])
    (api,) = [
        row
        for row in (state.document or {}).get("resources", [])
        if row.get("mode") == "managed"
        and not row.get("module")
        and row["type"] == "azurerm_container_app"
        and row["name"] == "api"
    ]
    refs = frozenset(row["name"] for row in api["instances"][0]["attributes"]["secret"])
    values["enabled_runtime_secret_names"] = json.dumps(sorted(runtime_secret_names(refs)))
    environment = os.environ | {f"TF_VAR_{name}": str(value) for name, value in values.items()}
    subprocess.run(
        ["terraform", "plan", "-input=false", f"-out={plan}"],
        env=environment,
        check=True,
        timeout=600,
    )
    shown = io.runner.json("terraform", "show", "-json", str(plan))
    after = io.backend()
    prior = plan_prior_state(plan)
    if (
        after.kind != state.kind
        or after.document != state.document
        or any(prior.get(key) != (state.document or {}).get(key) for key in ("lineage", "serial"))
    ):
        raise AdmissionDenied("WEB_PLAN_BACKEND_CHANGED")
    validate_web_plan(shown, state, checks.tf)
    print("WEB_PLAN_VERIFIED")


def main() -> None:
    from tirodhan.deployment.foundation_lifecycle import apply

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("plan", "apply"))
    parser.add_argument("--plan", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.operation == "plan":
            plan_web(args.plan)
        else:
            apply("web-apply", args.plan)
    except AdmissionDenied as exc:
        raise SystemExit(str(exc)) from None
    except Exception:
        raise SystemExit("WEB_RELEASE_UNVERIFIED") from None


if __name__ == "__main__":
    main()
