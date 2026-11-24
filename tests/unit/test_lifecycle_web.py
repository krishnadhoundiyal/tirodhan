from copy import deepcopy

import pytest
from lifecycle_fakes import MemoryObservations, state

from tirodhan.deployment.deployment_admission import AdmissionDenied, uses_reviewed_main_only
from tirodhan.deployment.lifecycle_evidence import BackendState
from tirodhan.deployment.prerequisites import REQUIRED
from tirodhan.deployment.release_setup import expected_bindings
from tirodhan.deployment.web_lifecycle import (
    WEB_APP_ADDRESSES,
    WEB_CERTIFICATE_ADDRESSES,
    WEB_DOMAIN_ADDRESSES,
    WEB_WORKLOAD_RESOURCES,
    bind_web_certificates,
    validate_web_plan,
    web_hostname,
)


def test_web_plan_cannot_change_backend_or_replace_pull_identity() -> None:
    images = dict(customer="customer-digest", manager="manager-digest")
    outputs = {
        "deployment_stage": {"value": 4},
        "application_image": {"value": "api"},
        "migration_image": {"value": "migration"},
        "fluent_bit_image": {"value": "logging"},
    }
    pull_address = "azurerm_user_assigned_identity.web_pull[0]"
    state = BackendState("INITIALIZED", {"outputs": outputs}, "etag", {pull_address: "identity"})
    resources = [
        {
            "address": address,
            "values": {"template": [{"container": [{"image": images[app]}]}]},
        }
        for app, address in WEB_APP_ADDRESSES.items()
    ]
    resources.extend(
        {"address": address} for address in WEB_WORKLOAD_RESOURCES - set(WEB_APP_ADDRESSES.values())
    )
    changes = dict.fromkeys(WEB_WORKLOAD_RESOURCES - {pull_address}, ["create"])
    plan = {
        "variables": {"nonprod_web_images": {"value": images}},
        "planned_values": {"outputs": outputs, "root_module": {"resources": resources}},
        "resource_changes": [
            {"address": address, "change": {"actions": actions}}
            for address, actions in changes.items()
        ],
    }
    inputs = {"nonprod_web_images": images}
    assert validate_web_plan(plan, state, inputs) == changes
    for address, actions in [
        ("azurerm_container_app.api[0]", ["update"]),
        (pull_address, ["delete", "create"]),
        (pull_address, ["create"]),
    ]:
        out_of_scope = deepcopy(plan)
        out_of_scope["resource_changes"].append(
            {"address": address, "change": {"actions": actions}}
        )
        with pytest.raises(AdmissionDenied, match="WEB_PLAN_OUTSIDE_WORKLOADS"):
            validate_web_plan(out_of_scope, state, inputs)


def test_web_authorizations_are_scoped_to_web_without_database_evidence() -> None:
    plan = expected_bindings("web-release")
    apply = expected_bindings("apply", "web-apply")
    assert plan == dict.fromkeys(
        ("terraform-setup", "web-plan"), {"execution_environment": "nonprod-plan"}
    )
    assert apply == dict.fromkeys(
        ("terraform-setup", "web-apply"), {"execution_environment": "nonprod"}
    )
    assert uses_reviewed_main_only(list(plan))
    assert uses_reviewed_main_only(list(apply))
    assert not uses_reviewed_main_only([*plan, "runtime-plan"])
    assert "database" not in REQUIRED["web-plan"] | REQUIRED["web-apply"]
    assert "saved_plan" in REQUIRED["web-apply"]


def test_certificate_binding_only_changes_an_unbound_approved_hostname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    io = MemoryObservations()
    environment = io.scope + "/providers/Microsoft.App/managedEnvironments/tirodhan-np-test01-aca"
    resources = {"azurerm_container_app_environment.nonprod": environment}
    live = {}
    for app, address in WEB_APP_ADDRESSES.items():
        hostname = web_hostname(app)
        app_id = io.scope + f"/providers/Microsoft.App/containerApps/tirodhan-np-test01-{app}-web"
        certificate_id = environment + f"/managedCertificates/tirodhan-np-test01-{app}-web-tls"
        resources[address] = app_id
        resources[WEB_DOMAIN_ADDRESSES[app]] = app_id + "/customDomainName/" + hostname
        resources[WEB_CERTIFICATE_ADDRESSES[app]] = certificate_id
        binding = {
            "name": hostname,
            "bindingType": "SniEnabled" if app == "customer" else "Disabled",
            "certificateId": certificate_id if app == "customer" else None,
        }
        live[app_id] = {"properties": {"configuration": {"ingress": {"customDomains": [binding]}}}}
        live[certificate_id] = {
            "properties": {"subjectName": hostname, "provisioningState": "Succeeded"}
        }
    io.current = state(resources)
    monkeypatch.setattr(io, "arm", lambda resource_id, version: live[resource_id])
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(io.runner, "run", lambda *args: commands.append(args))

    bind_web_certificates(io)

    assert len(commands) == 1
    assert commands[0][:4] == ("az", "containerapp", "hostname", "bind")
    assert "manager-nonprod.qtkservice.com" in commands[0]
    assert resources[WEB_CERTIFICATE_ADDRESSES["manager"]] in commands[0]
