"""An additive gateway cannot reconcile or migrate the deployed application."""

import hashlib
import json
import subprocess
import sys
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from lifecycle_fakes import CONFIG, SCOPE, MemoryObservations, auth, state, write_plan

from tirodhan.deployment import apim_lifecycle as apim
from tirodhan.deployment import foundation_lifecycle
from tirodhan.deployment.deployment_admission import AdmissionDenied
from tirodhan.deployment.lifecycle_checks import Checks
from tirodhan.deployment.lifecycle_evidence import ObservationError
from tirodhan.deployment.release_setup import expected_bindings

TF = CONFIG["terraform"] | {"apim_publisher_email": "publisher@example.com"}
GATEWAY = SCOPE + "/providers/Microsoft.ApiManagement/service/tirodhan-np-test01-apim"
URL = "https://tirodhan-np-test01-apim.azure-api.net"


@pytest.fixture
def baseline():
    existing = state(
        {
            "azurerm_container_app.api[0]": SCOPE + "/providers/Microsoft.App/containerApps/api",
            "azurerm_container_app_job.migration[0]": SCOPE
            + "/providers/Microsoft.App/jobs/migrate",
        },
        serial=56,
    )
    document = existing.document
    document["outputs"] = {
        "deployment_stage": {"value": 4},
        "application_image": {"value": "app"},
        "migration_image": {"value": "migration"},
        "fluent_bit_image": {"value": "sidecar"},
    }
    api, job = (row["instances"][0] for row in document["resources"])
    document["resources"][0]["name"] = "api"
    document["resources"][1]["name"] = "migration"
    api["index_key"] = job["index_key"] = 0
    api["attributes"]["secret"] = [{"name": name} for name in sorted(apim.MIGRATION_SECRET_NAMES)]
    job["attributes"]["template"] = [
        {
            "container": [
                {
                    "name": "application",
                    "env": [{"name": "TIRODHAN_MIGRATION_OPERATION_KEY", "value": "a" * 64}],
                },
            ]
        }
    ]
    return existing


@pytest.fixture
def plan(baseline):
    return {
        "complete": True,
        "errored": False,
        "applyable": True,
        "variables": {
            key: {"value": value} for key, value in (TF | apim.deployed_inputs(baseline)).items()
        },
        "resource_changes": [
            {
                "mode": "managed",
                "type": "azurerm_api_management",
                "address": apim.ADDRESS,
                "change": {
                    "actions": ["create"],
                    "before": None,
                    "after": {
                        "name": "tirodhan-np-test01-apim",
                        "resource_group_name": TF["resource_group_name"],
                        "location": TF["location"],
                        "publisher_email": TF["apim_publisher_email"],
                        "sku_name": "Consumption_0",
                    },
                },
            }
        ],
        "output_changes": {"apim_gateway_url": {"actions": ["create"]}},
    }


@pytest.fixture
def session_plan(baseline, plan):
    baseline.resources[apim.ADDRESS] = GATEWAY
    session = {
        "secret_name": "gateway-secret",
        "web_policy_xml": "<policies/>",
        "business_operations": {"current-user": {"method": "GET", "path": "/auth/me"}},
    }
    terraform = TF | {"apim_web_session": session}
    plan["variables"]["apim_web_session"] = {"value": session}
    plan["output_changes"] = {}
    gateway = plan["resource_changes"][0]["change"]
    gateway["before"] = gateway["after"] | {"id": GATEWAY, "identity": []}
    gateway["actions"] = ["update"]
    gateway["after"] = gateway["before"] | {"identity": [{"type": "SystemAssigned"}]}
    resource_values = {
        "azurerm_role_assignment.gateway_secret": {
            "scope": SCOPE
            + "/providers/Microsoft.KeyVault/vaults/td-np-test01-kv/secrets/gateway-secret",
            "role_definition_name": "Key Vault Secrets User",
        },
        "azurerm_api_management_named_value.gateway_secret": {
            "secret": True,
            "value_from_key_vault": [
                {"secret_id": "https://td-np-test01-kv.vault.azure.net/secrets/gateway-secret"}
            ],
        },
        "azurerm_api_management_api_policy.web": {"xml_content": session["web_policy_xml"]},
    }
    for address in sorted(apim.session_resource_addresses(session)):
        plan["resource_changes"].append(
            {
                "address": address,
                "mode": "managed",
                "change": {
                    "actions": ["create"],
                    "before": None,
                    "after": {
                        "api_management_name": "tirodhan-np-test01-apim",
                        "resource_group_name": TF["resource_group_name"],
                    }
                    | resource_values.get(address.removeprefix(apim.SESSION_MODULE), {}),
                },
            }
        )
    return plan, terraform


def test_existing_gateway_session_plan_is_scoped(baseline, session_plan):
    plan, terraform = session_plan
    gateway = plan["resource_changes"][0]["change"]
    apim.validate_apim_plan(plan, baseline, terraform)

    gateway["after"]["sku_name"] = "Developer_1"
    with pytest.raises(AdmissionDenied, match="APIM_SESSION_GATEWAY_UPDATE_MISMATCH"):
        apim.validate_apim_plan(plan, baseline, terraform)
    gateway["after"]["sku_name"] = "Consumption_0"

    assignment = next(
        row["change"]["after"]
        for row in plan["resource_changes"]
        if row["address"] == apim.SESSION_MODULE + "azurerm_role_assignment.gateway_secret"
    )
    secret_scope = assignment["scope"]
    assignment["scope"] = secret_scope.split("/secrets/")[0]
    with pytest.raises(AdmissionDenied, match="APIM_SESSION_POLICY_OR_SECRET_BINDING_MISMATCH"):
        apim.validate_apim_plan(plan, baseline, terraform)
    assignment["scope"] = secret_scope

    plan["resource_changes"].append(
        {"address": apim.PILOT_ADDRESS, "mode": "managed", "change": {"actions": ["create"]}}
    )
    with pytest.raises(AdmissionDenied, match="APIM_SESSION_PLAN_SCOPE_MISMATCH"):
        apim.validate_apim_plan(plan, baseline, terraform)


def test_reconcile_creates_only_missing_children_and_preserves_the_identity(baseline, session_plan):
    plan, terraform = session_plan
    principal = "11111111-1111-1111-1111-111111111111"
    gateway = plan["resource_changes"][0]["change"]
    gateway["after"]["identity"][0]["principal_id"] = principal
    gateway["before"] = deepcopy(gateway["after"])
    gateway["actions"] = ["no-op"]
    baseline.document["resources"].append(
        {
            "mode": "managed",
            "type": "azurerm_api_management",
            "name": "nonprod",
            "instances": [{"index_key": 0, "attributes": gateway["before"]}],
        }
    )
    assignment = next(
        row["change"] for row in plan["resource_changes"] if row["address"] == apim.SESSION_ROLE
    )
    assignment["after"]["principal_id"] = principal
    api_address = apim.SESSION_MODULE + "azurerm_api_management_api.web"
    existing_api = next(
        row["change"] for row in plan["resource_changes"] if row["address"] == api_address
    )
    existing_api["after"]["id"] = GATEWAY + "/apis/tirodhan-web-session;rev=1"
    existing_api["before"] = deepcopy(existing_api["after"])
    existing_api["actions"] = ["no-op"]
    baseline.resources[api_address] = existing_api["after"]["id"]

    with pytest.raises(AdmissionDenied, match="APIM_SESSION_PLAN_SCOPE_MISMATCH"):
        apim.validate_apim_plan(plan, baseline, terraform)
    plan["variables"]["apim_session_reconcile"] = {"value": True}
    terraform = terraform | {"apim_session_reconcile": True}
    apim.validate_apim_plan(plan, baseline, terraform)

    existing_api["actions"] = ["delete", "create"]
    with pytest.raises(AdmissionDenied, match="APIM_SESSION_PLAN_SCOPE_MISMATCH"):
        apim.validate_apim_plan(plan, baseline, terraform)


@pytest.mark.parametrize(
    ("component", "api_name"),
    [("web", "tirodhan-web-session"), ("backend", "tirodhan-backend-v1")],
)
def test_recovery_reads_policy_endpoint_and_rejects_unrecorded_write(baseline, component, api_name):
    address = apim.SESSION_MODULE + f"azurerm_api_management_api_policy.{component}"
    state_id = f"{GATEWAY}/apis/{api_name}".casefold()
    policy_id = state_id + "/policies/policy"
    live = {policy_id: {"id": policy_id}}
    io = SimpleNamespace(
        tf=TF | {"apim_web_session": {"secret_name": "gateway-secret"}},
        scope=SCOPE,
        vault_id=SCOPE + "/providers/Microsoft.KeyVault/vaults/td-np-test01-kv",
        arm=lambda resource_id, version: live.get(resource_id),
    )
    principal = "11111111-1111-1111-1111-111111111111"
    assert not apim.recovery_children_match(io, baseline, {address}, principal)
    baseline.resources[address] = state_id
    assert apim.recovery_children_match(io, baseline, {address}, principal)
    del live[policy_id]
    assert not apim.recovery_children_match(io, baseline, {address}, principal)


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("complete", False, "APIM_PLAN_INCOMPLETE"),
        ("resource_drift", [{}], "APIM_PLAN_RESOURCE_DRIFT"),
        ("deferred_changes", [{}], "APIM_PLAN_NOT_ADDITIVE"),
        ("action_invocations", [{}], "APIM_PLAN_NOT_ADDITIVE"),
        ("output_changes", {"api_url": {"actions": ["update"]}}, "APIM_PLAN_OUTPUT_MISMATCH"),
    ],
)
def test_rejects_incomplete_drifted_or_unrelated_plan(baseline, plan, field, value, code):
    plan[field] = value
    with pytest.raises(AdmissionDenied, match=code):
        apim.validate_apim_plan(plan, baseline, TF)


@pytest.fixture(
    params=[
        "azurerm_container_app.api[0]",
        'azurerm_container_app.worker["planning"]',
        'azurerm_container_app_job.scheduled["outbox"]',
    ]
)
def plan_with_command_refresh(request, baseline, plan):
    address = request.param
    kind = address.split(".", 1)[0]
    resource_id = baseline.resources.setdefault(address, SCOPE + "/providers/Microsoft.App/example")
    api = address == "azurerm_container_app.api[0]"
    before = {
        "id": resource_id,
        "template": [
            {
                "container": [
                    {
                        "name": "application",
                        "image": "app",
                        "command": None if api else ["python"],
                        "args": None if api else ["-m", "worker"],
                    },
                    {
                        "name": "fluent-bit",
                        "image": "sidecar",
                        "command": None,
                        "args": ["service"],
                    },
                ]
            }
        ],
    }
    after = deepcopy(before)
    application, sidecar = after["template"][0]["container"]
    sidecar["command"] = []
    if api:
        application.update(args=[], command=[])
    identity = {
        "address": address,
        "mode": "managed",
        "provider_name": "registry.terraform.io/hashicorp/azurerm",
        "type": kind,
    }
    drift = identity | {
        "change": {
            "actions": ["update"],
            "before": before,
            "after": after,
            "after_unknown": {},
        },
    }
    plan["resource_drift"] = [drift]
    planned = identity | {"change": {"actions": ["no-op"], "before": after, "after": after}}
    plan["resource_changes"].append(planned)
    return plan, drift, planned


def test_azure_command_refresh_allows_only_gateway_create(baseline, plan_with_command_refresh):
    plan, _, _ = plan_with_command_refresh
    expected = deepcopy(plan)
    apim.validate_apim_plan(plan, baseline, TF)
    assert plan == expected


@pytest.mark.parametrize(
    "deviation",
    ["command", "image", "volume", "reverse", "unknown", "resource", "move", "delete"],
)
def test_command_refresh_does_not_mask_other_drift(baseline, plan_with_command_refresh, deviation):
    plan, drift, _ = plan_with_command_refresh
    change = drift["change"]
    template = change["after"]["template"][0]
    sidecar = template["container"][1]
    if deviation == "command":
        sidecar["command"] = ["different"]
    elif deviation == "image":
        sidecar["image"] = "different"
    elif deviation == "volume":
        template["volume"] = [{"name": "other"}]
    elif deviation == "reverse":
        change["before"], change["after"] = change["after"], change["before"]
    elif deviation == "unknown":
        change["after_unknown"] = {"template": True}
    elif deviation == "resource":
        change["before"]["id"] = "unrelated"
    elif deviation == "move":
        drift["previous_address"] = "unrelated"
    else:
        change["actions"] = ["delete"]
    with pytest.raises(AdmissionDenied, match="^APIM_PLAN_RESOURCE_DRIFT$"):
        apim.validate_apim_plan(plan, baseline, TF)


def test_command_refresh_does_not_authorize_workload_update(baseline, plan_with_command_refresh):
    plan, _, planned = plan_with_command_refresh
    planned["change"]["actions"] = ["update"]
    with pytest.raises(AdmissionDenied, match="^APIM_PLAN_NOT_ADDITIVE$"):
        apim.validate_apim_plan(plan, baseline, TF)


@pytest.mark.parametrize(
    ("cause", "expected"),
    [
        (
            apim.ApimPlanBlocked("APIM_SECRET_GRANT_PERMISSIONS_MISSING"),
            "APIM_PREREQUISITES_BLOCKED:apim_baseline:APIM_SECRET_GRANT_PERMISSIONS_MISSING",
        ),
        (ValueError("private resource details"), "APIM_PREREQUISITES_BLOCKED:apim_baseline"),
        (
            apim.ApimPlanBlocked("APIM_PRIVATE: sensitive runtime value"),
            "APIM_PREREQUISITES_BLOCKED:apim_baseline",
        ),
    ],
)
def test_apim_cli_identifies_failed_prerequisite_without_printing_provider_data(
    tmp_path, monkeypatch, cause, expected
):
    from tirodhan.deployment import foundation_reconcile

    def blocked(_operation):
        raise AdmissionDenied("PREREQUISITES_BLOCKED:apim_baseline") from cause

    monkeypatch.setattr(foundation_reconcile, "admitted_with_grant", blocked)
    monkeypatch.setattr(
        sys, "argv", ["foundation_lifecycle", "apim-plan", "--plan", str(tmp_path / "apim.tfplan")]
    )
    with pytest.raises(SystemExit) as error:
        foundation_lifecycle.main()
    assert str(error.value) == f"LIFECYCLE_BLOCKED: {expected}"


@pytest.mark.parametrize("field", ["summary", "detail"])
@pytest.mark.parametrize("provider_code", ["AuthorizationFailed", "ForbiddenByRbac"])
def test_terraform_diagnostics_are_classified_without_exposing_payload(
    baseline, tmp_path, monkeypatch, capsys, field, provider_code
):
    from tirodhan.deployment import foundation_reconcile, prerequisites

    checks = SimpleNamespace(backend=lambda: baseline, tf=TF)
    monkeypatch.setattr(foundation_reconcile, "admitted_with_grant", lambda _: (checks, None, None))
    monkeypatch.setattr(prerequisites, "revalidate_database", lambda _: None)
    monkeypatch.setattr(
        sys, "argv", ["foundation_lifecycle", "apim-plan", "--plan", str(tmp_path / "apim.tfplan")]
    )
    diagnostic = json.dumps(
        {
            "type": "diagnostic",
            "diagnostic": {
                "severity": "error",
                "summary": f"{provider_code}: private-value"
                if field == "summary"
                else "Read failed",
                "detail": f"{provider_code}: private-value" if field == "detail" else "",
            },
        }
    )

    def terraform(args, **options):
        assert "-json" in args
        assert options["capture_output"] is True
        raise subprocess.CalledProcessError(1, args, output=diagnostic, stderr="private-stderr")

    monkeypatch.setattr(apim.subprocess, "run", terraform)
    with pytest.raises(SystemExit) as error:
        foundation_lifecycle.main()
    assert str(error.value) == "LIFECYCLE_BLOCKED: APIM_TERRAFORM_PLAN_FAILED:AZURE_PERMISSION"
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("actions", [["update"], ["delete"], ["delete", "create"], ["create"]])
def test_only_exact_gateway_may_change(baseline, plan, actions):
    plan["resource_changes"].append(
        {
            "mode": "managed",
            "address": "azurerm_container_app.api[0]",
            "change": {"actions": actions},
        }
    )
    with pytest.raises(AdmissionDenied, match="APIM_PLAN_NOT_ADDITIVE"):
        apim.validate_apim_plan(plan, baseline, TF)


@pytest.mark.parametrize(
    ("detail", "labels"),
    [
        (
            "unexpected status 403 with error: AuthorizationPermissionMismatch",
            "HTTP_403,PERMISSION",
        ),
        ("unexpected status 409 with error: LeaseAlreadyPresent", "HTTP_409,LEASE_PRESENT"),
        ("PUT failed: context deadline exceeded", "TIMEOUT"),
        ("arbitrary response customer-private-code 599", ""),
    ],
)
def test_state_lock_diagnostic_preserves_only_known_categories(detail, labels):
    output = json.dumps(
        {
            "type": "diagnostic",
            "diagnostic": {
                "severity": "error",
                "summary": "Error acquiring the state lock",
                "detail": detail + "\nLock Info: private lease ID, path, and identity",
            },
        }
    )
    expected = "APIM_TERRAFORM_PLAN_FAILED:STATE_LOCK"
    if labels:
        expected += "".join(f",STATE_LOCK_{label}" for label in labels.split(","))
    assert apim.terraform_plan_failure(output) == expected


def test_exact_saved_plan_and_live_fingerprint_are_rechecked(baseline, plan, tmp_path):
    io = MemoryObservations()
    io.current = baseline
    io.runner.plan = plan
    path = tmp_path / "apim.tfplan"
    write_plan(path, baseline)
    authorization = auth(
        "apim-apply",
        plan_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        state_fingerprint=baseline.fingerprint,
    )
    checks = Checks(authorization, CONFIG | {"terraform": TF}, {"plan": path}, io)
    assert checks.check_saved_plan()
    io.current.document["serial"] += 1
    assert not checks.check_saved_plan()


@pytest.mark.parametrize("changed", ["state", "gateway"])
def test_apply_stops_before_terraform_if_live_context_changes(baseline, monkeypatch, changed):
    io = MemoryObservations()
    io.tf = TF
    io.current = baseline
    checks = Checks(
        auth("apim-apply", state_fingerprint=baseline.fingerprint),
        CONFIG | {"terraform": TF},
        {},
        io,
    )
    monkeypatch.setattr(
        foundation_lifecycle, "admitted_with_grant", lambda *_, **__: (checks, io, None)
    )
    monkeypatch.setattr(foundation_lifecycle, "revalidate_database", lambda _: None)
    if changed == "state":
        io.current.document["serial"] += 1
    else:
        monkeypatch.setattr(io, "arm", lambda *_: {"id": GATEWAY})
    with pytest.raises(AdmissionDenied, match="BEFORE_APPLY"):
        foundation_lifecycle.apply("apim-apply", Path("apim.tfplan"))
    assert not io.runner.commands


def test_interrupted_apply_allows_provider_timeout_and_never_compensates(baseline, monkeypatch):
    io = MemoryObservations()
    io.tf = TF
    io.current = baseline
    checks = Checks(
        auth("apim-apply", state_fingerprint=baseline.fingerprint),
        CONFIG | {"terraform": TF},
        {},
        io,
    )
    monkeypatch.setattr(
        foundation_lifecycle, "admitted_with_grant", lambda *_, **__: (checks, io, None)
    )
    monkeypatch.setattr(foundation_lifecycle, "revalidate_database", lambda _: None)
    commands = []

    def terraform(*args, timeout):
        commands.append((args, timeout))
        raise ObservationError("OBSERVATION_UNAVAILABLE")

    monkeypatch.setattr(io.runner, "run", terraform)
    with pytest.raises(ObservationError, match="OBSERVATION_UNAVAILABLE"):
        foundation_lifecycle.apply("apim-apply", Path("apim.tfplan"))
    assert commands == [
        (("terraform", "apply", "-input=false", "apim.tfplan"), apim.APIM_APPLY_TIMEOUT_SECONDS)
    ]
    assert commands[0][1] > 3 * 60 * 60


@pytest.mark.parametrize("state_change", ["lease_metadata", "state_content", "saved_plan_serial"])
def test_plan_tolerates_only_the_backend_lease_metadata_change(
    baseline, plan, tmp_path, monkeypatch, state_change
):
    from tirodhan.deployment import foundation_reconcile, prerequisites

    io = MemoryObservations()
    io.current = baseline
    io.runner.plan = plan
    checks = SimpleNamespace(backend=io.backend, tf=TF)
    monkeypatch.setattr(foundation_reconcile, "admitted_with_grant", lambda _: (checks, io, None))
    monkeypatch.setattr(prerequisites, "revalidate_database", lambda _: None)
    path = tmp_path / "apim.tfplan"

    def terraform(args, **_):
        assert args[:4] == ["terraform", "plan", "-input=false", f"-out={path}"]
        assert "-var=application_image=app" in args
        assert "-var=deployment_stage=4" in args
        assert all(not arg.startswith("-target=") for arg in args)
        assert "-lock=false" not in args
        after = deepcopy(baseline.document)
        if state_change == "state_content":
            after["outputs"]["application_image"]["value"] = "changed"
        io.current = replace(baseline, document=after, etag="etag-after-native-unlock")
        saved = baseline
        if state_change == "saved_plan_serial":
            prior = deepcopy(baseline.document)
            prior["serial"] += 1
            saved = replace(baseline, document=prior)
        write_plan(path, saved)

    monkeypatch.setattr(apim.subprocess, "run", terraform)
    if state_change == "lease_metadata":
        apim.plan_gateway(path)
        assert path.is_file()
    else:
        with pytest.raises(AdmissionDenied, match="APIM_BACKEND_CHANGED_DURING_PLAN"):
            apim.plan_gateway(path)


def test_plan_reports_only_static_drift_details_when_blocked(
    baseline, plan, tmp_path, monkeypatch, capsys
):
    from tirodhan.deployment import foundation_reconcile, prerequisites

    io = MemoryObservations()
    private_address = 'azurerm_container_app.api["private-index"]'
    io.current = replace(
        baseline,
        resources=baseline.resources
        | {private_address: baseline.resources["azurerm_container_app.api[0]"]},
    )
    plan["resource_drift"] = [
        {
            "address": private_address,
            "change": {
                "actions": ["update"],
                "before": {"tags": {"private-map-key": "private-value"}, "private-field": "a"},
                "after": {"tags": {}, "private-field": "b"},
            },
        },
        {"address": "private-unknown-address"},
    ]
    io.runner.plan = plan
    checks = SimpleNamespace(backend=io.backend, tf=TF)
    monkeypatch.setattr(foundation_reconcile, "admitted_with_grant", lambda _: (checks, io, None))
    monkeypatch.setattr(prerequisites, "revalidate_database", lambda _: None)
    path = tmp_path / "apim.tfplan"
    monkeypatch.setattr(
        apim.subprocess, "run", lambda *_args, **_kwargs: write_plan(path, baseline)
    )
    with pytest.raises(AdmissionDenied, match="^APIM_PLAN_RESOURCE_DRIFT$"):
        apim.plan_gateway(path)
    assert capsys.readouterr().out == (
        "APIM_PLAN_DRIFT_DETAIL entries=2 unlisted=1 omitted_blocks=0 "
        "blocks=azurerm_container_app.api(tags)\n"
    )


def test_gate_requires_exact_workflow_and_absent_gateway(baseline, monkeypatch):
    io = MemoryObservations()
    io.current = baseline
    io.tf = TF
    checks = Checks(auth("apim-plan"), CONFIG | {"terraform": TF}, {}, io)
    assert not checks.check_apim_baseline()
    monkeypatch.setenv("GITHUB_WORKFLOW_REF", apim.WORKFLOW_REF)
    assert checks.check_apim_baseline()
    monkeypatch.setattr(io, "arm", lambda *_: {"id": GATEWAY})
    assert not checks.check_apim_baseline()


def test_postcondition_requires_exact_gateway_and_preserves_state(baseline, monkeypatch):
    io = MemoryObservations()
    io.tf = TF
    io.current = state(baseline.resources | {apim.ADDRESS: GATEWAY}, serial=57)
    io.current.document["outputs"] = baseline.document["outputs"] | {
        "apim_gateway_url": {"value": URL}
    }
    monkeypatch.setattr(
        io,
        "arm",
        lambda *_: {
            "id": GATEWAY,
            "sku": {"name": "Consumption", "capacity": 0},
            "properties": {"gatewayUrl": URL, "provisioningState": "Succeeded"},
        },
    )
    apim.verify_gateway(io, baseline)
    io.current.document["outputs"]["application_image"] = {"value": "changed"}
    with pytest.raises(AdmissionDenied, match="APIM_POSTCONDITIONS_FAILED"):
        apim.verify_gateway(io, baseline)


def test_pilot_admission_requires_stopped_original_and_absent_pilot(baseline, monkeypatch):
    io = MemoryObservations()
    io.tf = TF | {"apim_pilot_enabled": True}
    io.current = replace(baseline, resources=baseline.resources | {apim.ADDRESS: GATEWAY})
    checks = Checks(auth("apim-plan"), CONFIG | {"terraform": io.tf}, {}, io)
    original = {
        "id": GATEWAY,
        "sku": {"name": "Consumption"},
        "properties": {"provisioningState": "Stopped"},
    }
    resources = {GATEWAY: original}
    monkeypatch.setenv("GITHUB_WORKFLOW_REF", apim.WORKFLOW_REF)
    monkeypatch.setattr(io, "arm", lambda identity, _: resources.get(identity))
    assert checks.check_apim_baseline()
    resources[GATEWAY + "-pilot"] = {"id": GATEWAY + "-pilot"}
    assert not checks.check_apim_baseline()
    resources.pop(GATEWAY + "-pilot")
    original["properties"]["provisioningState"] = "Succeeded"
    assert not checks.check_apim_baseline()


def test_pilot_plan_allows_only_new_pilot_and_preserves_original(baseline, plan):
    baseline = replace(baseline, resources=baseline.resources | {apim.ADDRESS: GATEWAY})
    terraform = TF | {"apim_pilot_enabled": True}
    plan["variables"]["apim_pilot_enabled"] = {"value": True}
    plan["resource_changes"][0]["address"] = apim.PILOT_ADDRESS
    plan["resource_changes"][0]["change"]["after"]["name"] += "-pilot"
    plan["output_changes"] = {"apim_pilot_gateway_url": {"actions": ["create"]}}
    with pytest.raises(AdmissionDenied, match="APIM_PLAN_GATEWAY_MISMATCH"):
        apim.validate_apim_plan(plan, baseline, terraform)
    plan["resource_changes"][0]["change"]["after"]["identity"] = [{"type": "SystemAssigned"}]
    apim.validate_apim_plan(plan, baseline, terraform)
    plan["resource_changes"].append(
        {"mode": "managed", "address": apim.ADDRESS, "change": {"actions": ["delete"]}}
    )
    with pytest.raises(AdmissionDenied, match="APIM_PLAN_NOT_ADDITIVE"):
        apim.validate_apim_plan(plan, baseline, terraform)


def test_gateway_workflow_and_signing_are_isolated():
    workflow = yaml.safe_load(Path(".github/workflows/nonprod-apim.yml").read_text())
    assert set(workflow[True]) == {"workflow_dispatch"}
    dispatch = workflow[True]["workflow_dispatch"]["inputs"]["operation"]
    assert dispatch["options"] == ["create", "configure", "reconcile"]
    assert dispatch["default"] == "create"
    assert workflow["env"]["TIRODHAN_APIM_OPERATION"] == "${{ inputs.operation }}"
    assert workflow["concurrency"] == {"group": "nonprod-release", "cancel-in-progress": False}
    jobs = workflow["jobs"]
    assert set(jobs) == {"release-admission", "apim-plan", "apim-apply"}
    assert all(job["if"] == "github.ref == 'refs/heads/main'" for job in jobs.values())
    token = "TIRODHAN_NONPROD_RELEASE_AUTHORIZATION"
    admission = jobs["release-admission"]
    assert token not in workflow["env"]
    assert token not in admission["env"]
    assert [step["run"] for step in admission["steps"] if token in step.get("env", {})] == [
        "python -m tirodhan.deployment.deployment_admission"
    ]
    assert jobs["apim-plan"]["needs"] == "release-admission"
    assert jobs["apim-apply"]["needs"] == "apim-plan"
    assert jobs["apim-apply"]["environment"] == "nonprod"
    assert jobs["apim-apply"]["timeout-minutes"] * 60 > apim.APIM_APPLY_TIMEOUT_SECONDS
    assert set(expected_bindings("apim-release")) == {"terraform-setup", "apim-plan"}
    assert set(expected_bindings("apply", "apim-apply")) == {"terraform-setup", "apim-apply"}
    assert "apim-plan" not in expected_bindings("release")


@pytest.mark.parametrize(
    ("operation", "signed_configuration", "allowed"),
    [
        ("create", {}, True),
        ("create", {"apim_pilot_enabled": True}, True),
        ("configure", {"apim_web_session": {}}, True),
        ("configure", {}, False),
        ("create", {"apim_web_session": {}}, False),
        ("configure", {"apim_web_session": {}, "apim_pilot_enabled": True}, False),
        ("reconcile", {"apim_web_session": {}, "apim_session_reconcile": True}, True),
        ("reconcile", {"apim_web_session": {}}, False),
        ("configure", {"apim_web_session": {}, "apim_session_reconcile": True}, False),
        (None, {}, False),
    ],
)
def test_apim_dispatch_must_match_signed_configuration(operation, signed_configuration, allowed):
    if allowed:
        apim.validate_dispatch(TF | signed_configuration, operation)
    else:
        with pytest.raises(AdmissionDenied, match="APIM_DISPATCH_CONFIGURATION_MISMATCH"):
            apim.validate_dispatch(TF | signed_configuration, operation)
