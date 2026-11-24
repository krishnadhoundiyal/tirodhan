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
WEB_DOMAIN_ADDRESSES = {
    app: f'azurerm_container_app_custom_domain.web["{app}"]' for app in WEB_APP_ADDRESSES
}
WEB_CERTIFICATE_ADDRESSES = {
    app: f'azurerm_container_app_environment_managed_certificate.web["{app}"]'
    for app in WEB_APP_ADDRESSES
}
WEB_DOMAIN_RESOURCES = frozenset(
    (*WEB_DOMAIN_ADDRESSES.values(), *WEB_CERTIFICATE_ADDRESSES.values())
)
WEB_WORKLOAD_RESOURCES = WEB_SUPPORT_ADDRESSES | frozenset(WEB_APP_ADDRESSES.values())
WEB_ADDRESSES = WEB_WORKLOAD_RESOURCES | WEB_DOMAIN_RESOURCES


def web_hostname(app: str) -> str:
    return f"{app}-nonprod.qtkservice.com"


def validate_web_plan(
    plan: dict[str, Any], state: BackendState, inputs: dict[str, Any]
) -> dict[str, list[str]]:
    images = inputs.get("nonprod_web_images")
    outputs = (state.document or {}).get("outputs", {})
    planned = plan.get("planned_values", {})
    custom_domains = inputs.get("nonprod_web_custom_domains", False)
    if (
        not isinstance(images, dict)
        or set(images) != set(WEB_APP_ADDRESSES)
        or state.kind != "INITIALIZED"
        or outputs.get("deployment_stage", {}).get("value") != 4
        or plan.get("variables", {}).get("nonprod_web_images", {}).get("value") != images
        or type(custom_domains) is not bool
        or (
            plan.get("variables", {}).get("nonprod_web_custom_domains", {}).get("value", False)
            != custom_domains
        )
        or populated_key_paths(plan)
    ):
        raise AdmissionDenied("WEB_PLAN_INPUTS_UNVERIFIED")
    for name in ("deployment_stage", "application_image", "migration_image", "fluent_bit_image"):
        if planned.get("outputs", {}).get(name, {}).get("value") != outputs[name]["value"]:
            raise AdmissionDenied("WEB_PLAN_CHANGED_BACKEND_OUTPUT")
    resources = {row["address"]: row for row in planned.get("root_module", {}).get("resources", [])}
    required = WEB_ADDRESSES if custom_domains else WEB_WORKLOAD_RESOURCES
    if not required.issubset(resources):
        raise AdmissionDenied("WEB_PLAN_WORKLOADS_MISSING")
    for app, address in WEB_APP_ADDRESSES.items():
        containers = resources[address]["values"]["template"][0]["container"]
        if len(containers) != 1 or containers[0].get("image") != images[app]:
            raise AdmissionDenied("WEB_PLAN_IMAGE_MISMATCH")
        if custom_domains:
            hostname = web_hostname(app)
            domain = resources[WEB_DOMAIN_ADDRESSES[app]]["values"]
            certificate = resources[WEB_CERTIFICATE_ADDRESSES[app]]["values"]
            if (
                domain.get("name") != hostname
                or domain.get("container_app_id") != state.resources.get(address)
                or certificate.get("subject_name") != hostname
                or certificate.get("domain_control_validation") != "CNAME"
                or certificate.get("container_app_environment_id")
                != state.resources.get("azurerm_container_app_environment.nonprod")
                or certificate.get("name") != f"tirodhan-np-{inputs['suffix']}-{app}-web-tls"
            ):
                raise AdmissionDenied("WEB_PLAN_DOMAIN_SCOPE_MISMATCH")
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
            address not in required
            or actions != allowed
            or (actions == ["create"] and address in state.resources)
        ):
            raise AdmissionDenied("WEB_PLAN_OUTSIDE_WORKLOADS")
        changes[address] = actions
    return changes


def bind_web_certificates(io: Observations) -> None:
    """Complete the managed binding that AzureRM can read but cannot configure."""
    resources = io.backend().resources
    environment = resources["azurerm_container_app_environment.nonprod"]
    for app, app_address in WEB_APP_ADDRESSES.items():
        hostname = web_hostname(app)
        resource_id = resources[app_address]
        certificate_id = resources[WEB_CERTIFICATE_ADDRESSES[app]]
        name = f"tirodhan-np-{io.tf['suffix']}-{app}-web"
        if (
            resource_id.casefold()
            != f"{io.scope}/providers/Microsoft.App/containerApps/{name}".casefold()
            or resources[WEB_DOMAIN_ADDRESSES[app]].casefold()
            != f"{resource_id}/customDomainName/{hostname}".casefold()
            or certificate_id.casefold()
            != f"{environment}/managedCertificates/{name}-tls".casefold()
        ):
            raise AdmissionDenied(f"WEB_CERTIFICATE_SCOPE_MISMATCH:{app}")
        certificate = (io.arm(certificate_id, "2025-07-01") or {}).get("properties", {})
        if (
            certificate.get("subjectName") != hostname
            or certificate.get("provisioningState") != "Succeeded"
        ):
            raise AdmissionDenied(f"WEB_CERTIFICATE_NOT_READY:{app}")
        application = (io.arm(resource_id, "2025-07-01") or {}).get("properties", {})
        bindings = (
            application.get("configuration", {}).get("ingress", {}).get("customDomains") or []
        )
        matching = [binding for binding in bindings if binding.get("name") == hostname]
        if len(matching) != 1:
            raise AdmissionDenied(f"WEB_HOSTNAME_NOT_REGISTERED:{app}")
        (domain,) = matching
        existing_certificate = str(domain.get("certificateId") or "")
        if (
            existing_certificate.casefold() == certificate_id.casefold()
            and domain.get("bindingType") == "SniEnabled"
        ):
            continue
        if existing_certificate and existing_certificate.casefold() != certificate_id.casefold():
            raise AdmissionDenied(f"WEB_CERTIFICATE_BINDING_CONFLICT:{app}")
        io.runner.run(
            "az",
            "containerapp",
            "hostname",
            "bind",
            "--resource-group",
            io.tf["resource_group_name"],
            "--name",
            resource_id.rsplit("/", 1)[-1],
            "--hostname",
            hostname,
            "--certificate",
            certificate_id,
            "--output",
            "none",
        )


def detach_web_certificate(io: Observations, hostname: str, certificate_id: str) -> None:
    """Release the matching certificate under Terraform's protected destroy lock."""
    if (
        os.environ.get("GITHUB_WORKFLOW_REF")
        != "krishnadhoundiyal/tirodhan/.github/workflows/nonprod-destroy.yml@refs/heads/main"
        or os.environ.get("TIRODHAN_EXECUTION_ENVIRONMENT") != "nonprod"
    ):
        raise AdmissionDenied("WEB_CERTIFICATE_DETACH_REQUIRES_PROTECTED_DESTROY")
    apps = [app for app in WEB_APP_ADDRESSES if web_hostname(app) == hostname]
    if not apps:
        raise AdmissionDenied("WEB_CERTIFICATE_DETACH_HOSTNAME_UNVERIFIED")
    (app,) = apps
    prefix = f"tirodhan-np-{io.tf['suffix']}"
    name = f"{prefix}-{app}-web"
    expected_cert = (
        f"{io.scope}/providers/Microsoft.App/managedEnvironments/{prefix}-aca"
        f"/managedCertificates/{name}-tls"
    )
    if certificate_id.casefold() != expected_cert.casefold():
        raise AdmissionDenied("WEB_CERTIFICATE_DETACH_TARGET_UNVERIFIED")
    target = io.arm(f"{io.scope}/providers/Microsoft.App/containerApps/{name}", "2025-07-01") or {}
    ingress = target.get("properties", {}).get("configuration", {}).get("ingress", {})
    if any(
        domain.get("name") == hostname
        and str(domain.get("certificateId") or "").casefold() == certificate_id.casefold()
        for domain in (ingress.get("customDomains") or [])
    ):
        io.runner.run(
            "az",
            "containerapp",
            "hostname",
            "delete",
            "--resource-group",
            io.tf["resource_group_name"],
            "--name",
            name,
            "--hostname",
            hostname,
            "--output",
            "none",
        )


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
                    hosts = [fqdn]
                    if io.tf.get("nonprod_web_custom_domains"):
                        hosts.append(web_hostname(app))
                    responses = [httpx.get(f"https://{host}/health", timeout=10) for host in hosts]
                    if all(
                        response.status_code == 200 and response.text.strip() == "ok"
                        for response in responses
                    ):
                        print(f"WEB_APP_READY {app} https://{fqdn}")
                        if len(hosts) == 2:
                            print(f"WEB_CUSTOM_DOMAIN_READY {app} https://{hosts[-1]}")
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
    from tirodhan.deployment.deployment_admission import configuration
    from tirodhan.deployment.foundation_lifecycle import apply

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("plan", "apply", "detach-certificate"))
    parser.add_argument("--plan", type=Path)
    args = parser.parse_args()
    try:
        if args.operation == "detach-certificate":
            detach_web_certificate(
                Observations(configuration()),
                os.environ["WEB_DESTROY_HOSTNAME"],
                os.environ["WEB_DESTROY_CERTIFICATE_ID"],
            )
        elif args.plan is None:
            parser.error("--plan is required for plan or apply")
        elif args.operation == "plan":
            plan_web(args.plan)
        else:
            apply("web-apply", args.plan)
    except AdmissionDenied as exc:
        raise SystemExit(str(exc)) from None
    except Exception:
        raise SystemExit("WEB_RELEASE_UNVERIFIED") from None


if __name__ == "__main__":
    main()
