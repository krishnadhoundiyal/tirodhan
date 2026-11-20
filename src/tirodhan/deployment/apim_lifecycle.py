"""Plan a gateway addition without reconciling the deployed application."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import NAMESPACE_URL, uuid5

from tirodhan.deployment.check_destroy_plan import apim_child_state_ids, approved_apim_child_id
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
PILOT_ADDRESS = "azurerm_api_management.pilot[0]"
SESSION_MODULE = "module.apim_web_session[0]."
SESSION_ROLE = SESSION_MODULE + "azurerm_role_assignment.gateway_secret"
NATIVE_OPERATIONS = {"native-otp-start", "native-otp-verify", "native-refresh", "native-logout"}
WEB_OPERATIONS = ("start", "verify", "refresh", "logout")
API_VERSION = "2024-05-01"
WORKFLOW_REF = "krishnadhoundiyal/tirodhan/.github/workflows/nonprod-apim.yml@refs/heads/main"
# AzureRM 5.8.0 allows three hours for APIM creation. Leave time for Terraform
# to persist state and release the backend lease before the runner stops it.
APIM_APPLY_TIMEOUT_SECONDS = 190 * 60


class ApimPlanBlocked(AdmissionDenied):
    """A locally generated APIM diagnostic safe for the workflow log."""


def validate_dispatch(terraform: dict[str, Any], operation: str | None) -> None:
    session = terraform.get("apim_web_session")
    reconcile = terraform.get("apim_session_reconcile") is True
    if operation == "create" and session is None and not reconcile:
        return
    if (
        operation == ("reconcile" if reconcile else "configure")
        and isinstance(session, dict)
        and terraform.get("apim_pilot_enabled") is not True
    ):
        return
    raise AdmissionDenied("APIM_DISPATCH_CONFIGURATION_MISMATCH")


def gateway_name(terraform: dict[str, Any], *, pilot: bool = False) -> str:
    suffix = "apim-pilot" if pilot else "apim"
    return f"{terraform['resource_group_name']}-{suffix}"


def gateway_id(io: Observations, *, pilot: bool = False) -> str:
    return (
        f"{io.scope}/providers/Microsoft.ApiManagement/service/{gateway_name(io.tf, pilot=pilot)}"
    )


def gateway_target(terraform: dict[str, Any]) -> tuple[bool, str, str]:
    if terraform.get("apim_pilot_enabled") is True:
        return True, PILOT_ADDRESS, "apim_pilot_gateway_url"
    return False, ADDRESS, "apim_gateway_url"


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


def tracked_gateway_principal(state: BackendState) -> str | None:
    identities = [
        identity.get("principal_id")
        for resource in (state.document or {}).get("resources", [])
        if resource.get("mode") == "managed"
        and not resource.get("module")
        and resource.get("type") == "azurerm_api_management"
        and resource.get("name") == "nonprod"
        for instance in resource["instances"]
        if instance.get("index_key") == 0
        for identity in instance["attributes"].get("identity") or []
        if identity.get("type") == "SystemAssigned"
    ]
    if len(identities) == 1 and isinstance(identities[0], str) and identities[0]:
        return identities[0]
    return None


def recovery_children_match(
    io: Observations, state: BackendState, children: set[str], principal: str
) -> bool:
    gateway = gateway_id(io).casefold()
    secret_scope = f"{io.vault_id}/secrets/{io.tf['apim_web_session']['secret_name']}".casefold()
    role_name = uuid5(NAMESPACE_URL, f"{secret_scope}/{principal.casefold()}")
    for address in sorted(children):
        tracked_id = state.resources.get(address, "").casefold()
        resource_type = address.removeprefix(SESSION_MODULE).split(".", 1)[0]
        allowed_ids: tuple[str, ...]
        if address == SESSION_ROLE:
            allowed_ids = (
                f"{secret_scope}/providers/microsoft.authorization/roleassignments/{role_name}",
            )
            version = "2022-04-01"
        else:
            allowed_ids = apim_child_state_ids(address, resource_type, gateway)
            version = API_VERSION
        if not allowed_ids or (tracked_id and tracked_id not in allowed_ids):
            return False
        for resource_id in (tracked_id,) if tracked_id else allowed_ids:
            if resource_type == "azurerm_api_management_api_policy":
                resource_id += "/policies/policy"
            if (io.arm(resource_id, version) is not None) != bool(tracked_id):
                return False
    return True


def check_baseline(checks: Checks) -> bool:
    if os.environ.get("GITHUB_WORKFLOW_REF") != WORKFLOW_REF or not checks.tf.get(
        "apim_publisher_email"
    ):
        return False
    state = checks.backend()
    deployed_inputs(state)
    pilot, address, _ = gateway_target(checks.tf)
    session = checks.tf.get("apim_web_session")
    if session is not None:
        original_id = gateway_id(checks.io)
        original = checks.io.arm(original_id, API_VERSION) or {}
        sku = original.get("sku", {})
        identity = original.get("identity") or {}
        expected_children = session_resource_addresses(session)
        present = {address for address in state.resources if address.startswith(SESSION_MODULE)}
        if checks.tf.get("apim_session_reconcile") is True:
            principal = tracked_gateway_principal(state)
            known_identity = bool(
                principal
                and identity.get("type") == "SystemAssigned"
                and identity.get("principalId", "").casefold() == principal.casefold()
                and present < expected_children
                and recovery_children_match(checks.io, state, expected_children, principal)
            )
        else:
            known_identity = not present and not identity.get("principalId")
        return (
            not pilot
            and PILOT_ADDRESS not in state.resources
            and known_identity
            and state.resources.get(ADDRESS, "").casefold() == original_id.casefold()
            and str(original.get("id", "")).casefold() == original_id.casefold()
            and sku.get("name") == "Consumption"
            and sku.get("capacity") == 0
            and original.get("properties", {}).get("provisioningState") == "Succeeded"
            and checks.permits(
                original_id,
                {
                    "Microsoft.ApiManagement/service/apis/write",
                    "Microsoft.ApiManagement/service/apis/operations/write",
                    "Microsoft.ApiManagement/service/apis/policies/write",
                    "Microsoft.ApiManagement/service/namedValues/write",
                },
            )
            and checks.permits(
                f"{checks.io.vault_id}/secrets/{session['secret_name']}",
                {"Microsoft.Authorization/roleAssignments/write"},
            )
        )
    if pilot:
        original_id = gateway_id(checks.io)
        original = checks.io.arm(original_id, API_VERSION) or {}
        if (
            state.resources.get(ADDRESS, "").casefold() != original_id.casefold()
            or str(original.get("id", "")).casefold() != original_id.casefold()
            or original.get("sku", {}).get("name") != "Consumption"
            or original.get("properties", {}).get("provisioningState") != "Stopped"
        ):
            return False
    return (
        address not in state.resources
        and checks.io.arm(gateway_id(checks.io, pilot=pilot), API_VERSION) is None
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


def session_resource_addresses(session: dict[str, Any]) -> set[str]:
    operations = session["business_operations"]
    return {
        SESSION_MODULE + suffix
        for suffix in {
            "azurerm_role_assignment.gateway_secret",
            "azurerm_api_management_named_value.gateway_secret",
            "azurerm_api_management_api.web",
            "azurerm_api_management_api.backend",
            "azurerm_api_management_api_policy.web",
            "azurerm_api_management_api_policy.backend",
            *(
                f'azurerm_api_management_api_operation.web["{name}-{method}"]'
                for name in WEB_OPERATIONS
                for method in ("post", "options")
            ),
            *(
                f"azurerm_api_management_api_operation.backend[{json.dumps(name)}]"
                for name in NATIVE_OPERATIONS | operations.keys()
            ),
        }
    }


def validate_session_changes(
    managed: list[dict[str, Any]],
    outputs: dict[str, Any],
    state: BackendState,
    terraform: dict[str, Any],
) -> None:
    """Admit an initial configuration or the missing children of its signed retry."""
    session = terraform["apim_web_session"]
    expected_children = session_resource_addresses(session)
    reconcile = terraform.get("apim_session_reconcile") is True
    present = {address for address in state.resources if address.startswith(SESSION_MODULE)}
    by_address = {row["address"]: row["change"] for row in managed}
    changed = {address for address, change in by_address.items() if change["actions"] != ["no-op"]}
    expected_changes = expected_children - present if reconcile else expected_children | {ADDRESS}
    group = terraform["resource_group_name"]
    scope = f"/subscriptions/{terraform['subscription_id']}/resourceGroups/{group}"
    name = gateway_name(terraform)
    expected_id = f"{scope}/providers/Microsoft.ApiManagement/service/{name}"
    if (
        state.resources.get(ADDRESS, "").casefold() != expected_id.casefold()
        or terraform.get("apim_pilot_enabled") is True
        or PILOT_ADDRESS in state.resources
        or (not present < expected_children if reconcile else bool(present))
        or len(by_address) != len(managed)
        or not (expected_children | {ADDRESS}).issubset(by_address)
        or changed != expected_changes
        or any(row["actions"] != ["no-op"] for row in outputs.values())
    ):
        raise ApimPlanBlocked("APIM_SESSION_PLAN_SCOPE_MISMATCH")

    gateway = by_address[ADDRESS]
    before, after = gateway.get("before"), gateway.get("after")
    unknown = gateway.get("after_unknown", {})
    if (
        not isinstance(before, dict)
        or not isinstance(after, dict)
        or before.get("id", "").casefold() != expected_id.casefold()
        or [{"type": entry.get("type")} for entry in after.get("identity", [])]
        != [{"type": "SystemAssigned"}]
        or any(value for key, value in unknown.items() if key != "identity")
        or {key: value for key, value in before.items() if key != "identity"}
        != {key: value for key, value in after.items() if key != "identity"}
    ):
        raise ApimPlanBlocked("APIM_SESSION_GATEWAY_UPDATE_MISMATCH")
    principal = tracked_gateway_principal(state)
    if reconcile:
        if (
            not principal
            or after["identity"][0].get("principal_id") != principal
            or before != after
        ):
            raise ApimPlanBlocked("APIM_SESSION_GATEWAY_UPDATE_MISMATCH")
    elif before.get("identity") not in (None, []) or gateway["actions"] != ["update"]:
        raise ApimPlanBlocked("APIM_SESSION_GATEWAY_UPDATE_MISMATCH")

    for address in expected_children:
        change = by_address[address]
        after = change.get("after")
        if not isinstance(after, dict) or (
            address != SESSION_ROLE
            and (
                after.get("api_management_name") != name
                or after.get("resource_group_name") != group
            )
        ):
            raise ApimPlanBlocked("APIM_SESSION_CHILD_CREATE_MISMATCH")
        if address in present:
            if change.get("before") != after or after.get("id") != state.resources[address]:
                raise ApimPlanBlocked("APIM_SESSION_CHILD_IDENTITY_MISMATCH")
        elif change["actions"] != ["create"] or change.get("before") is not None:
            raise ApimPlanBlocked("APIM_SESSION_CHILD_CREATE_MISMATCH")

    vault_name = f"td-np-{terraform['suffix']}-kv"
    secret = session["secret_name"]
    assignment = by_address[SESSION_ROLE]
    named_value = by_address[SESSION_MODULE + "azurerm_api_management_named_value.gateway_secret"]
    policy = by_address[SESSION_MODULE + "azurerm_api_management_api_policy.web"]
    vault_refs = named_value["after"].get("value_from_key_vault") or []
    if (
        assignment["after"].get("scope", "").casefold()
        != f"{scope}/providers/Microsoft.KeyVault/vaults/{vault_name}/secrets/{secret}".casefold()
        or assignment["after"].get("role_definition_name") != "Key Vault Secrets User"
        or (reconcile and assignment["after"].get("principal_id") != principal)
        or named_value["after"].get("secret") is not True
        or named_value["after"].get("value") not in (None, "")
        or len(vault_refs) != 1
        or vault_refs[0].get("secret_id")
        != f"https://{vault_name}.vault.azure.net/secrets/{secret}"
        or vault_refs[0].get("identity_client_id") not in (None, "")
        or policy["after"].get("xml_content") != session["web_policy_xml"]
    ):
        raise ApimPlanBlocked("APIM_SESSION_POLICY_OR_SECRET_BINDING_MISMATCH")


def validate_apim_plan(
    plan: dict[str, Any], state: BackendState, terraform: dict[str, Any]
) -> None:
    """Reject material drift and limit changes to the selected APIM operation."""
    inputs = deployed_inputs(state)
    pilot, address, output_name = gateway_target(terraform)
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
    ):
        raise ApimPlanBlocked("APIM_PLAN_NOT_ADDITIVE")
    outputs = plan.get("output_changes", {})
    if terraform.get("apim_web_session") is not None:
        validate_session_changes(managed, outputs, state, terraform)
        return
    if len(changes) != 1:
        raise ApimPlanBlocked("APIM_PLAN_NOT_ADDITIVE")
    gateway = changes[0]
    change = gateway["change"]
    expected = {
        "name": gateway_name(terraform, pilot=pilot),
        "resource_group_name": terraform["resource_group_name"],
        "location": terraform["location"],
        "publisher_email": terraform["apim_publisher_email"],
        "sku_name": "Consumption_0",
    }
    if (
        (pilot and ADDRESS not in state.resources)
        or address in state.resources
        or gateway["address"] != address
        or gateway["type"] != "azurerm_api_management"
        or change["actions"] != ["create"]
        or change["before"] is not None
        or any(change["after"].get(key) != value for key, value in expected.items())
        or (
            pilot
            and [identity.get("type") for identity in (change["after"].get("identity") or [])]
            != ["SystemAssigned"]
        )
    ):
        raise ApimPlanBlocked("APIM_PLAN_GATEWAY_MISMATCH")
    if outputs.get(output_name, {}).get("actions") != ["create"] or any(
        row["actions"] != ["no-op"] for name, row in outputs.items() if name != output_name
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


_STATE_LOCK_CODES = {
    "AuthorizationFailure": "STATE_LOCK_AUTHORIZATION",
    "AuthorizationFailed": "STATE_LOCK_AUTHORIZATION",
    "AuthorizationPermissionMismatch": "STATE_LOCK_PERMISSION",
    "AuthenticationFailed": "STATE_LOCK_AUTHENTICATION",
    "KeyBasedAuthenticationNotPermitted": "STATE_LOCK_KEY_AUTH_DISABLED",
    "LeaseAlreadyPresent": "STATE_LOCK_LEASE_PRESENT",
    "LeaseIsBreakingAndCannotBeAcquired": "STATE_LOCK_LEASE_BREAKING",
    "LeaseIdMismatchWithBlobOperation": "STATE_LOCK_LEASE_MISMATCH",
    "BlobNotFound": "STATE_LOCK_BLOB_MISSING",
    "ContainerNotFound": "STATE_LOCK_CONTAINER_MISSING",
    "BlobImmutableDueToPolicy": "STATE_LOCK_IMMUTABLE",
    "AccountIsDisabled": "STATE_LOCK_ACCOUNT_DISABLED",
    "SubscriptionIsDisabled": "STATE_LOCK_SUBSCRIPTION_DISABLED",
    "ReadOnlyDisabledSubscription": "STATE_LOCK_SUBSCRIPTION_READ_ONLY",
    "ServerBusy": "STATE_LOCK_SERVER_BUSY",
    "OperationTimedOut": "STATE_LOCK_TIMEOUT",
}
_STATE_LOCK_HTTP_STATUS = re.compile(
    r"\b(?:status(?: code)?|HTTP(?:/\d(?:\.\d)?)?)(?:\s*[:=]\s*|\s+)"
    r"(401|403|404|409|412|429|500|502|503|504)\b",
    re.IGNORECASE,
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
            if isinstance(detail, str):
                for code, label in _STATE_LOCK_CODES.items():
                    if re.search(rf"\b{code}\b", detail):
                        labels.add(label)
                labels.update(
                    f"STATE_LOCK_HTTP_{status}"
                    for status in _STATE_LOCK_HTTP_STATUS.findall(detail)
                )
                if re.search(
                    r"\b(?:context deadline exceeded|i/o timeout|TLS handshake timeout)\b", detail
                ):
                    labels.add("STATE_LOCK_TIMEOUT")
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
    pilot, _, _ = gateway_target(checks.tf)
    arguments = [
        f"-var={key}={json.dumps(value) if isinstance(value, list) else value}"
        for key, value in values.items()
    ]
    # The recovery pilot uses the locked deployment baseline. Refreshing failed
    # workloads could otherwise plan repairs the owner excluded from this release.
    if pilot:
        arguments.append("-refresh=false")
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
    if checks.tf.get("apim_web_session") is not None:
        print("APIM_PLAN_VERIFIED: web session configuration of existing Consumption gateway")
    else:
        print("APIM_PLAN_VERIFIED: one Consumption gateway create")


def verify_gateway(io: Observations, before: BackendState) -> None:
    after = io.backend()
    pilot, address, output_name = gateway_target(io.tf)
    expected_id = gateway_id(io, pilot=pilot)
    live = io.arm(expected_id, API_VERSION) or {}
    sku = live.get("sku", {})
    before_outputs = dict((before.document or {}).get("outputs", {}))
    after_outputs = dict((after.document or {}).get("outputs", {}))
    gateway_url = after_outputs.pop(output_name, {}).get("value")
    before_outputs.pop(output_name, None)
    before_state, after_state = before.document or {}, after.document or {}
    if (
        after.kind != "INITIALIZED"
        or after_state.get("lineage") != before_state.get("lineage")
        or after_state["serial"] <= before_state["serial"]
        or {key: value.casefold() for key, value in after.resources.items()}
        != {
            key: value.casefold()
            for key, value in (before.resources | {address: expected_id}).items()
        }
        or before_outputs != after_outputs
        or not gateway_url
        or live.get("properties", {}).get("gatewayUrl") != gateway_url
        or str(live.get("id", "")).casefold() != expected_id.casefold()
        or sku.get("name") != "Consumption"
        or sku.get("capacity") != 0
        or (
            pilot
            and (
                (live.get("identity") or {}).get("type") != "SystemAssigned"
                or not (live.get("identity") or {}).get("principalId")
            )
        )
        or live.get("properties", {}).get("provisioningState") != "Succeeded"
    ):
        raise AdmissionDenied("APIM_POSTCONDITIONS_FAILED")


def verify_sessions(io: Observations, before: BackendState) -> None:
    session = io.tf["apim_web_session"]
    children = session_resource_addresses(session)
    after = io.backend()
    gateway = gateway_id(io)
    live = io.arm(gateway, API_VERSION) or {}
    identity = live.get("identity") or {}
    before_state, after_state = before.document or {}, after.document or {}
    if (
        after.kind != "INITIALIZED"
        or after_state.get("lineage") != before_state.get("lineage")
        or after_state["serial"] <= before_state["serial"]
        or before_state.get("outputs") != after_state.get("outputs")
        or set(after.resources) != set(before.resources) | children
        or any(after.resources.get(address) != value for address, value in before.resources.items())
        or str(live.get("id", "")).casefold() != gateway.casefold()
        or live.get("properties", {}).get("provisioningState") != "Succeeded"
        or identity.get("type") != "SystemAssigned"
        or not identity.get("principalId")
    ):
        raise AdmissionDenied("APIM_SESSION_POSTCONDITIONS_FAILED")

    role_address = SESSION_MODULE + "azurerm_role_assignment.gateway_secret"
    role_id = after.resources[role_address]
    role = io.arm(role_id, "2022-04-01") or {}
    scope = f"{io.vault_id}/secrets/{session['secret_name']}"
    assignment_prefix = f"{scope}/providers/Microsoft.Authorization/roleAssignments/"
    if (
        not role_id.casefold().startswith(assignment_prefix.casefold())
        or role.get("properties", {}).get("principalId") != identity["principalId"]
    ):
        raise AdmissionDenied("APIM_SESSION_CHILD_POSTCONDITIONS_FAILED")
    for address in sorted(children - {role_address}):
        resource_type = address.removeprefix(SESSION_MODULE).split(".", 1)[0]
        resource_id = after.resources[address]
        if not approved_apim_child_id(
            address, resource_type, resource_id.casefold(), gateway.casefold()
        ):
            raise AdmissionDenied("APIM_SESSION_CHILD_POSTCONDITIONS_FAILED")
        if resource_type == "azurerm_api_management_api_policy":
            resource_id += "/policies/policy"
        if not io.arm(resource_id, API_VERSION):
            raise AdmissionDenied("APIM_SESSION_CHILD_POSTCONDITIONS_FAILED")
