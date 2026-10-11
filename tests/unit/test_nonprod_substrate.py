from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import hcl2
import pytest
import yaml

from tirodhan.deployment.azure_controls import LIST_KEYS, permission_allows_list_keys
from tirodhan.deployment.check_state import populated_key_paths
from tirodhan.deployment.release import migration_variables
from tirodhan.deployment.rotate_log_sas import main as rotate

ROOT = Path(__file__).resolve().parents[2]
STACK = ROOT / "infra/terraform/nonprod"


def hcl(name: str) -> dict[str, Any]:
    with (STACK / name).open(encoding="utf-8") as file:
        return hcl2.load(file)


def resource(filename: str, kind: str, name: str) -> dict[str, Any]:
    return next(
        value[kind][name]
        for value in hcl(filename)["resource"]
        if kind in value and name in value[kind]
    )


def test_storage_and_backend_are_entra_only() -> None:
    storage = resource("storage.tf", "azurerm_storage_account", "application")
    assert storage["shared_access_key_enabled"] is False
    assert storage["default_to_oauth_authentication"] is True
    assert storage["allow_nested_items_to_be_public"] is False
    assert storage["min_tls_version"] == "TLS1_2"
    assert storage["account_replication_type"] == "LRS"
    assert hcl("providers.tf")["provider"][0]["azurerm"]["storage_use_azuread"] is True
    backend = hcl("backend.tf")["terraform"][0]["backend"][0]["azurerm"]
    assert backend["use_azuread_auth"] is True
    assert backend["key"] == "tirodhan/nonprod.tfstate"
    assert "access_key" not in backend and "sas_token" not in backend
    containers = hcl("storage.tf")["resource"]
    blobs = [
        item["azurerm_storage_container"]
        for item in containers
        if "azurerm_storage_container" in item
    ]
    assert blobs and all(
        config["container_access_type"] == "private" for item in blobs for config in item.values()
    )


def test_planning_sessions_and_runtime_cell_contract() -> None:
    queues = resource("service_bus.tf", "azurerm_servicebus_queue", "work")
    assert queues["requires_session"] == '${each.key == "planning"}'
    assert queues["requires_duplicate_detection"] is False
    assert queues["max_delivery_count"] == 10
    assert queues["lock_duration"] == "PT1M"
    namespace = resource("service_bus.tf", "azurerm_servicebus_namespace", "bus")
    assert namespace["sku"] == "Standard"
    assert namespace["local_auth_enabled"] is False
    publisher = (ROOT / "src/tirodhan/modules/reliability/publisher.py").read_text()
    assert 'session_id=str(event.payload["cell_id"])' in publisher


def test_all_workloads_have_budgeted_sidecar_and_emptydir() -> None:
    apps = hcl("container_apps.tf")["resource"]
    jobs = hcl("jobs.tf")["resource"]
    for item in apps + jobs:
        config = next(iter(next(iter(item.values())).values()))
        template = config["template"][0]
        assert config["workload_profile_name"] == "Consumption"
        assert template["volume"][0]["storage_type"] == "EmptyDir"
        assert len(template["container"]) == 2
        main, sidecar = template["container"]
        assert sidecar["cpu"] == 0.25 and sidecar["memory"] == "0.5Gi"
        assert sidecar["args"] in (["service"], ["job"])
        assert main["volume_mounts"] == sidecar["volume_mounts"]
        assert all(
            "key_vault_secret_id" in entry["content"][0] and "value" not in entry["content"][0]
            for entry in config["dynamic"]
            if "secret" in entry
            for entry in [entry["secret"]]
        )
    locals_ = hcl("locals.tf")["locals"][0]
    expected = {
        "serviceability": (0.25, "0.5Gi", 2),
        "rider": (0.25, "0.5Gi", 2),
        "refund": (0.25, "0.5Gi", 1),
        "planning": (0.5, "1Gi", 2),
        "financial_webhook": (0.25, "0.5Gi", 1),
    }
    for name, (cpu, memory, max_) in expected.items():
        assert locals_["workers"][name]["cpu"] == cpu
        assert locals_["workers"][name]["memory"] == memory
        assert locals_["workers"][name]["max"] == max_
        total = (cpu + 0.25, float(memory.removesuffix("Gi")) + 0.5)
        assert total in {(0.5, 1.0), (0.75, 1.5)}
    api = resource("container_apps.tf", "azurerm_container_app", "api")
    assert api["template"][0]["container"][0]["cpu"] == 0.5
    assert api["template"][0]["container"][0]["memory"] == "1Gi"
    assert api["ingress"][0]["allow_insecure_connections"] is False
    worker = resource("container_apps.tf", "azurerm_container_app", "worker")
    assert "ingress" not in worker
    assert worker["template"][0]["min_replicas"] == 0
    assert api["template"][0]["min_replicas"] == 0
    rule = worker["template"][0]["custom_scale_rule"][0]
    assert rule["custom_rule_type"] == "azure-servicebus"
    assert rule["identity_id"] == (
        '${each.key == "financial_webhook" ? '
        "azurerm_user_assigned_identity.financial_webhook_receiver.id : "
        "azurerm_user_assigned_identity.runtime.id}"
    )
    assert "authentication" not in rule
    for name in ("migration", "scheduled"):
        job = resource("jobs.tf", "azurerm_container_app_job", name)
        main = job["template"][0]["container"][0]
        assert (main["cpu"], main["memory"]) == (0.25, "0.5Gi")
        assert "tirodhan.deployment.job" in main["args"]


def test_financial_webhook_trust_uses_exclusive_queue_scoped_workload_identities() -> None:
    for role in ("sender", "receiver"):
        shared = resource("rbac.tf", "azurerm_role_assignment", f"bus_{role}")
        assert 'key != "financial_webhook"' in shared["for_each"]
        dedicated = resource("rbac.tf", "azurerm_role_assignment", f"financial_webhook_{role}")
        assert dedicated["scope"] == '${azurerm_servicebus_queue.work["financial_webhook"].id}'
        assert dedicated["principal_id"] == (
            "${azurerm_user_assigned_identity.financial_webhook_" + role + ".principal_id}"
        )
        assert dedicated["role_definition_name"] == ("Azure Service Bus Data " + role.title())
    api = resource("container_apps.tf", "azurerm_container_app", "api")
    worker = resource("container_apps.tf", "azurerm_container_app", "worker")
    assert "financial_webhook_sender.id" in repr(api["identity"])
    assert "financial_webhook_sender.id" not in repr(worker["identity"])
    assert "financial_webhook_receiver.id" in repr(worker["identity"])
    assert "financial_webhook_receiver.id" not in repr(api["identity"])
    variables = hcl("variables.tf")["variable"]
    schedule = next(
        item["financial_inventory_schedule"]
        for item in variables
        if "financial_inventory_schedule" in item
    )
    assert schedule["default"] is None


@pytest.mark.parametrize("name, startup_seconds", [("migration", "300"), ("scheduled", "120")])
def test_job_sidecar_has_explicit_startup_tolerance(name: str, startup_seconds: str) -> None:
    job = resource("jobs.tf", "azurerm_container_app_job", name)
    sidecar = next(
        container
        for container in job["template"][0]["container"]
        if container["name"] == "fluent-bit"
    )
    env = {entry["name"]: entry.get("value") for entry in sidecar["env"]}
    assert env["JOB_STARTUP_SECONDS"] == startup_seconds


def test_database_exception_is_nonprod_with_no_password() -> None:
    db = resource("database.tf", "azurerm_postgresql_flexible_server", "database")
    assert db["sku_name"] == "B_Standard_B1ms"
    assert db["storage_mb"] == 32768
    assert db["backup_retention_days"] == 7
    assert db["geo_redundant_backup_enabled"] is False
    assert "high_availability" not in db and "administrator_password" not in db
    assert db["authentication"][0]["password_auth_enabled"] is False
    firewall = resource(
        "database.tf", "azurerm_postgresql_flexible_server_firewall_rule", "azure_nonprod"
    )
    assert firewall["start_ip_address"] == firewall["end_ip_address"] == "0.0.0.0"
    environment = resource(
        "container_apps_environment.tf", "azurerm_container_app_environment", "nonprod"
    )
    assert environment["workload_profile"] == [
        {"name": "Consumption", "workload_profile_type": "Consumption"}
    ]
    assert "log_analytics_workspace_id" not in environment
    assert "infrastructure_subnet_id" not in environment


def test_fluent_bit_pins_image_and_explicit_sas_only_settings() -> None:
    image = (ROOT / "deploy/fluent-bit/Dockerfile").read_text()
    assert "fluent-bit:4.0.11-debug@sha256:" in image and "latest" not in image
    config = (ROOT / "deploy/fluent-bit/tirodhan.conf").read_text()
    settings = dict(
        line.strip().split(None, 1) for line in config.splitlines() if line.startswith("    ")
    )
    assert settings["Auth_Type"] == "sas"
    assert settings["Tls"] == settings["Tls.Verify"] == settings["Tls.Verify_Hostname"] == "On"
    assert settings["Auto_Create_Container"] == "Off"
    assert settings["Container_Name"] == "app-logs"
    assert settings["Blob_Type"] == "blockblob" and settings["Compress_Blob"] == "On"
    assert settings["Flush"] == "1" and settings["Io_Timeout"] == "5s"
    assert settings["Upload_Timeout"] == "1m" and settings["Upload_File_Size"] == "1M"
    assert "shared_key" not in config.lower()


def test_deployment_custom_role_cannot_list_keys_even_with_wildcards() -> None:
    role = json.loads((ROOT / "deploy/bootstrap/deployment-role.json").read_text())
    assert not permission_allows_list_keys(
        [{"actions": role["Actions"], "notActions": role["NotActions"]}]
    )
    assert permission_allows_list_keys([{"actions": ["*"], "notActions": []}])
    assert permission_allows_list_keys([{"actions": [LIST_KEYS], "notActions": []}])
    # NotActions in one role does NOT deny permissions granted by another/inherited role.
    assert permission_allows_list_keys(
        [
            {"actions": ["*"], "notActions": [LIST_KEYS]},
            {"actions": ["*"], "notActions": []},
        ]
    )


def test_state_check_ignores_annotations_but_detects_key_values() -> None:
    assert (
        populated_key_paths(
            {
                "values": {"primary_access_key": None},
                "sensitive_values": {"primary_access_key": True},
            }
        )
        == []
    )
    found = populated_key_paths({"values": [{"primary_access_key": "synthetic-placeholder"}]})
    assert found == ["$.values[0].primary_access_key"]
    assert "synthetic-placeholder" not in str(found)
    assert populated_key_paths(
        {
            "secret": [
                {
                    "key_vault_secret_id": "https://example/secrets/log",
                    "value": "synthetic-placeholder",
                }
            ]
        }
    ) == ["$.secret[0].value"]


@pytest.mark.parametrize("stage", [0, 1, 2, 3, 4])
def test_migration_release_never_reverts_established_runtime(stage: int) -> None:
    state = {
        "deployment_stage": stage,
        "application_image": "old-app",
        "fluent_bit_image": "old-sidecar",
    }
    result = migration_variables(state, "new-app", "new-sidecar")
    assert result["deployment_stage"] == max(stage, 1)
    assert result["application_image"] == "old-app"
    assert result["fluent_bit_image"] == "old-sidecar"
    assert result["migration_image"] == "new-app"
    with pytest.raises(RuntimeError):
        migration_variables({}, "new-app", "new-sidecar")


def test_sas_rotation_is_user_delegation_write_only_with_overlap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tirodhan.deployment import rotate_log_sas as module

    monkeypatch.setenv("LOG_ACCOUNT_NAME", "syntheticnonprod")
    monkeypatch.setenv("KEY_VAULT_URL", "https://synthetic.vault.azure.net")
    client = MagicMock()
    client.__enter__.return_value = client
    monkeypatch.setattr(module, "BlobServiceClient", MagicMock(return_value=client))
    monkeypatch.setattr(module, "AzureCliCredential", MagicMock())
    generate, put = MagicMock(return_value="synthetic-placeholder"), MagicMock()
    monkeypatch.setattr(module, "generate_container_sas", generate)
    monkeypatch.setattr(module, "vault_put", put)
    rotate()
    first = generate.call_args.kwargs
    rotate()
    second = generate.call_args.kwargs
    assert "account_key" not in second and "user_delegation_key" in second
    assert second["container_name"] == "app-logs" and second["protocol"] == "https"
    assert str(second["permission"]) == "w"
    assert second["start"] < first["expiry"]
    assert (second["expiry"] - second["start"]).total_seconds() < 7 * 86400
    assert put.call_count == 2 and put.call_args.args[1] == "fluent-bit-sas"


def test_workflows_enforce_trust_and_migration_gate() -> None:
    deploy = yaml.safe_load((ROOT / ".github/workflows/nonprod-deploy.yml").read_text())
    jobs = deploy["jobs"]
    assert jobs["build"]["if"] == "github.ref == 'refs/heads/main'"
    assert jobs["migration-apply"]["environment"] == "nonprod"
    assert jobs["runtime-plan"]["needs"] == "migration-apply"
    assert jobs["runtime-apply"]["needs"] == "runtime-plan"
    pr = yaml.safe_load((ROOT / ".github/workflows/pr-checks.yml").read_text())
    assert "id-token" not in pr["permissions"]
    for path in (ROOT / ".github").rglob("*.yml"):
        text = path.read_text()
        assert "az containerapp update" not in text and "client-secret:" not in text
    for path in STACK.glob("*.tf"):
        assert "azurerm_key_vault_secret" not in path.read_text()
