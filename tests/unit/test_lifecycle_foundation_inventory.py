"""The prerequisite inventory must cover Terraform, including every repeated binding."""

import json
from pathlib import Path

import hcl2
import pytest
from lifecycle_fakes import MemoryObservations, auth, state

from tirodhan.deployment.apim_lifecycle import ADDRESS, gateway_id
from tirodhan.deployment.deployment_admission import AdmissionDenied
from tirodhan.deployment.foundation_inventory import required_addresses
from tirodhan.deployment.lifecycle_checks import Checks
from tirodhan.deployment.lifecycle_evidence import ObservationError, Observations


def test_reviewed_inventory_covers_all_nonworkload_terraform_resources() -> None:
    manifest = json.loads(Path("deploy/nonprod-foundation-inventory.json").read_text())
    actual = {}
    for path in Path("infra/terraform/nonprod").glob("*.tf"):
        for resource in hcl2.load(path.open()).get("resource", []):
            for kind, names in resource.items():
                if kind in {"azurerm_container_app", "azurerm_container_app_job"}:
                    continue
                for name, block in names.items():
                    address = kind + "." + name
                    if address == "azurerm_api_management.nonprod":
                        assert block["count"] == (
                            "${var.deployment_stage >= 4 && "
                            "var.apim_publisher_email != null ? 1 : 0}"
                        )
                        continue
                    if address == "azurerm_api_management.pilot":
                        assert block["count"].replace(" != None ", " != null ") == (
                            "${var.deployment_stage >= 4 && "
                            "var.apim_publisher_email != null && var.apim_pilot_enabled ? 1 : 0}"
                        )
                        continue
                    if address in {
                        "azurerm_role_assignment.web_pull",
                        "azurerm_user_assigned_identity.web_pull",
                    }:
                        assert block["count"] == "${length(local.web_images) == 0 ? 0 : 1}"
                        continue
                    actual[address] = block
    assert set(actual) == set(manifest["resources"])
    queues = {"serviceability", "rider", "refund", "planning", "financial_webhook"}
    locals_ = hcl2.load(Path("infra/terraform/nonprod/locals.tf").open())["locals"][0]
    assert set(locals_["queues"]) == queues
    for address, entry in manifest["resources"].items():
        block = actual[address]
        assert entry["expression"] == (block.get("count") or block.get("for_each"))
        if "count" in block:
            assert entry["expression"] == "${var.postgres_bootstrap_ipv4 == null ? 0 : 1}"
            assert entry["keys"] == [0] and entry["when"] == "postgres_bootstrap_ipv4"
        elif "for_each" in block:
            if address.startswith("azurerm_role_assignment."):
                assert set(entry["keys"]) == queues - {"financial_webhook"}
            elif address.startswith("azurerm_servicebus_queue."):
                assert set(entry["keys"]) == queues
            else:
                assert address == "azurerm_storage_container.private"
                assert set(entry["keys"]) == {"media", "app-logs"}
        else:
            assert entry["keys"] is None


def test_every_foundation_binding_is_mandatory_before_database_bootstrap() -> None:
    io = MemoryObservations()
    required = required_addresses(io.config["terraform"])
    for missing in required:
        io.current = state(
            {key: io.scope + "/providers/Synthetic/" + key for key in required - {missing}}
        )
        assert not Checks(
            auth("database-bootstrap", kind="database"), io.config, {}, io
        ).check_foundation()
        assert io.runner.commands == []


@pytest.mark.parametrize("assignment_exists", [True, False])
def test_foundation_reads_inventory_through_versioned_arm(
    monkeypatch, capsys, assignment_exists: bool
) -> None:
    io = MemoryObservations()
    resources = {}
    roles = set()
    for address in required_addresses(io.config["terraform"]):
        if address.startswith("azurerm_role_assignment."):
            resource_id = (
                io.scope + "/providers/Microsoft.ServiceBus/namespaces/test/queues/planning"
                "/providers/Microsoft.Authorization/roleAssignments/" + address
            )
            roles.add(resource_id)
        else:
            resource_id = io.scope + "/providers/Synthetic/" + address
        resources[address] = resource_id
    io.current = state(resources)
    reads = {}

    def arm(resource_id, version):
        assert resource_id not in reads
        reads[resource_id] = version
        if resource_id in roles:
            assert version == "2022-04-01"
            return {"id": resource_id} if assignment_exists else None
        return {
            "id": resource_id,
            "properties": {
                "principalId": "principal",
                "clientId": "client",
                "state": "Ready",
                "fullyQualifiedDomainName": "test.postgres.database.azure.com",
                "authConfig": {"passwordAuth": "Disabled"},
                "status": "Active",
                "provisioningState": "Succeeded",
            },
        }

    monkeypatch.setattr(io, "arm", arm)
    checks = Checks(auth("database-bootstrap", kind="database"), io.config, {}, io)
    assert checks.check_foundation() is assignment_exists
    assert io.runner.commands == []
    if assignment_exists:
        assert set(reads) == set(resources.values())
        assert reads[resources["azurerm_postgresql_flexible_server_database.application"]] == (
            "2024-08-01"
        )
    else:
        assert len(roles & reads.keys()) == 1
        message = capsys.readouterr().out
        assert "FOUNDATION_RESOURCE_UNVERIFIED azurerm_role_assignment." in message
        assert "/providers/" not in message


def test_inventory_refuses_unreviewed_terraform_drift(monkeypatch, tmp_path) -> None:
    from tirodhan.deployment import foundation_inventory

    (tmp_path / "deploy").mkdir()
    (tmp_path / "infra/terraform/nonprod").mkdir(parents=True)
    source = Path("deploy/nonprod-foundation-inventory.json").read_text()
    (tmp_path / "deploy/nonprod-foundation-inventory.json").write_text(source)
    monkeypatch.setattr(foundation_inventory, "ROOT", tmp_path)
    with pytest.raises(AdmissionDenied, match="SOURCE_CHANGED"):
        required_addresses({})


def test_optional_firewall_is_required_exactly_when_configured() -> None:
    address = "azurerm_postgresql_flexible_server_firewall_rule.bootstrap[0]"
    assert address not in required_addresses({})
    assert address in required_addresses({"postgres_bootstrap_ipv4": "192.0.2.1"})


@pytest.mark.parametrize(
    "registered,allowed,deleted,result",
    [
        (True, True, None, True),
        (False, True, None, False),
        (True, False, None, False),
        (True, True, {"id": "soft-deleted-instance"}, False),
    ],
)
def test_apim_preflight_requires_registration_write_access_and_no_tombstone(
    monkeypatch, registered, allowed, deleted, result
) -> None:
    io = MemoryObservations()
    io.config["terraform"].update(
        apim_publisher_email="publisher@example.invalid", location="centralindia"
    )
    checks = Checks(auth("runtime-plan"), io.config, {}, io)
    monkeypatch.setattr(checks, "check_providers", lambda: registered)
    monkeypatch.setattr(checks, "permits", lambda scope, actions: allowed and scope == io.scope)

    def deleted_lookup(resource_id, version):
        assert resource_id.endswith(
            "/locations/centralindia/deletedservices/tirodhan-np-test01-apim"
        )
        assert version == "2024-05-01"
        return deleted

    monkeypatch.setattr(io, "arm", deleted_lookup)
    assert checks.check_apim_preflight() is result


@pytest.mark.parametrize("azure_error", ["ServiceNotFound", "AuthorizationFailed"])
def test_apim_preflight_classifies_deleted_service_errors_through_arm(monkeypatch, azure_error):
    config = MemoryObservations().config
    config["terraform"]["apim_publisher_email"] = "publisher@example.invalid"
    io = Observations(config)
    checks = Checks(auth("runtime-plan"), config, {}, io)
    monkeypatch.setattr(checks, "check_providers", lambda: True)
    monkeypatch.setattr(checks, "permits", lambda scope, actions: True)

    def read(*args):
        assert args[:4] == ("az", "rest", "--method", "get")
        assert args[5].endswith(
            "/providers/Microsoft.ApiManagement/locations/centralindia/"
            "deletedservices/tirodhan-np-test01-apim?api-version=2024-05-01"
        )
        raise ObservationError(azure_error)

    monkeypatch.setattr(io.runner, "json", read)
    if azure_error == "ServiceNotFound":
        assert checks.check_apim_preflight()
    else:
        with pytest.raises(ObservationError, match="^AuthorizationFailed$"):
            checks.check_apim_preflight()


@pytest.mark.parametrize("operation", ["apim-plan", "apim-apply"])
@pytest.mark.parametrize("reconcile", [False, True])
def test_existing_apim_configuration_does_not_check_creation_tombstone(
    monkeypatch, operation, reconcile
):
    io = MemoryObservations()
    io.config["terraform"].update(
        apim_publisher_email="publisher@example.invalid",
        apim_web_session={"secret_name": "gateway-secret"},
        apim_session_reconcile=reconcile,
    )
    checks = Checks(auth(operation), io.config, {}, io)
    monkeypatch.setattr(checks, "check_providers", lambda: True)
    monkeypatch.setattr(checks, "permits", lambda scope, actions: scope == io.scope)
    monkeypatch.setattr(io, "arm", lambda *_: pytest.fail("creation tombstone was queried"))

    assert checks.check_apim_preflight()
    monkeypatch.setattr(checks, "permits", lambda *_: False)
    assert not checks.check_apim_preflight()


@pytest.mark.parametrize("operation", ["migration-plan", "runtime-apply"])
@pytest.mark.parametrize(
    ("tracked", "provisioning", "expected"),
    [(True, "Succeeded", True), (True, "Stopped", False), (False, "Succeeded", False)],
)
def test_workload_release_uses_live_original_gateway_for_configured_sessions(
    monkeypatch, operation, tracked, provisioning, expected
):
    io = MemoryObservations()
    io.config["terraform"].update(
        apim_publisher_email="publisher@example.invalid",
        apim_web_session={"secret_name": "gateway-secret"},
    )
    original = gateway_id(io)
    io.current = state({ADDRESS: original} if tracked else {})
    checks = Checks(auth(operation), io.config, {}, io)
    monkeypatch.setattr(checks, "check_providers", lambda: True)
    monkeypatch.setattr(checks, "permits", lambda *_: True)

    def read(resource_id, _version):
        assert resource_id == original
        return {
            "id": original,
            "sku": {"name": "Consumption", "capacity": 0},
            "properties": {"provisioningState": provisioning},
        }

    monkeypatch.setattr(io, "arm", read)
    assert checks.check_apim_preflight() is expected


def test_deleted_service_error_does_not_hide_active_gateway_read_failure(monkeypatch):
    io = Observations(MemoryObservations().config)

    def read(*args):
        raise ObservationError("ServiceNotFound")

    monkeypatch.setattr(io.runner, "json", read)
    active = f"{io.scope}/providers/Microsoft.ApiManagement/service/tirodhan-np-test01-apim"
    with pytest.raises(ObservationError, match="^ServiceNotFound$"):
        io.arm(active, "2024-05-01")
