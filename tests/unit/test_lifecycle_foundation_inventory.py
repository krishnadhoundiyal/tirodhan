"""The prerequisite inventory must cover Terraform, including every repeated binding."""

import json
from pathlib import Path

import hcl2
import pytest
from lifecycle_fakes import MemoryObservations, auth, state

from tirodhan.deployment.deployment_admission import AdmissionDenied
from tirodhan.deployment.foundation_inventory import required_addresses
from tirodhan.deployment.lifecycle_checks import Checks


def test_reviewed_inventory_covers_all_nonworkload_terraform_resources() -> None:
    manifest = json.loads(Path("deploy/nonprod-foundation-inventory.json").read_text())
    actual = {}
    for path in Path("infra/terraform/nonprod").glob("*.tf"):
        for resource in hcl2.load(path.open()).get("resource", []):
            for kind, names in resource.items():
                if kind in {"azurerm_container_app", "azurerm_container_app_job"}:
                    continue
                for name, block in names.items():
                    actual[kind + "." + name] = block
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
        io.current = state({key: "/synthetic/" + key for key in required - {missing}})
        assert not Checks(
            auth("database-bootstrap", kind="database"), io.config, {}, io
        ).check_foundation()
        assert io.runner.commands == []


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
