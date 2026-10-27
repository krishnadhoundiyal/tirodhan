"""Fail-closed NONPROD preflight, rollback, and bootstrap boundary tests."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tirodhan.deployment import foundation_lifecycle as lifecycle


@pytest.fixture
def config(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    values = {
        "subscription_id": "11111111-1111-1111-1111-111111111111",
        "tenant_id": "22222222-2222-2222-2222-222222222222",
        "resource_group_name": "tirodhan-np-test01",
        "location": "centralindia",
        "suffix": "test01",
    }
    monkeypatch.setattr(lifecycle, "settings", lambda: values)
    monkeypatch.setattr(lifecycle, "assert_bootstrap", lambda *_: None)
    monkeypatch.setattr(lifecycle, "ensure_postgres_sku", lambda *_: None)
    monkeypatch.setattr(lifecycle, "json_command", lambda *a: {})
    return values


def test_first_preflight_requires_empty_state_and_azure_inventory(
    monkeypatch: pytest.MonkeyPatch,
    config: dict[str, str],
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(lifecycle, "state_addresses", lambda: [])
    monkeypatch.setattr(lifecycle, "disposable_ids", lambda *_: set())
    record = tmp_path / "baseline.json"
    assert lifecycle.preflight(baseline=record) == "CLEAN"
    saved = json.loads(record.read_text())
    assert saved["classification"] == "CLEAN"
    assert saved["initial_state_count"] == 0
    assert saved["initial_inventory_count"] == 0
    assert saved["subscription_id"] == config["subscription_id"]


def test_partial_foundation_is_resumable_but_never_rollback_eligible(
    monkeypatch: pytest.MonkeyPatch,
    config: dict[str, str],
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(lifecycle, "state_addresses", lambda: ["azurerm_servicebus_namespace.bus"])
    monkeypatch.setattr(
        lifecycle, "disposable_ids", lambda *_: {"/subscriptions/111/resourcegroups/rg/bus"}
    )
    monkeypatch.setattr(lifecycle, "assert_owned_inventory", lambda *_: None)
    record = tmp_path / "baseline.json"
    assert lifecycle.preflight(baseline=record) == "PARTIAL"
    reject = MagicMock()
    monkeypatch.setattr(lifecycle, "command", reject)
    with pytest.raises(lifecycle.GateError, match="pre-existing state"):
        lifecycle.rollback(record)
    reject.assert_not_called()


def test_untracked_azure_resources_are_not_adopted(
    monkeypatch: pytest.MonkeyPatch,
    config: dict[str, str],
) -> None:
    monkeypatch.setattr(lifecycle, "state_addresses", lambda: [])
    monkeypatch.setattr(lifecycle, "disposable_ids", lambda *_: {"untracked"})
    with pytest.raises(lifecycle.GateError, match="Foreign/untracked"):
        lifecycle.preflight()


def test_legacy_vault_state_address_is_blocked(
    monkeypatch: pytest.MonkeyPatch,
    config: dict[str, str],
) -> None:
    monkeypatch.setattr(lifecycle, "state_addresses", lambda: ["azurerm_key_vault.runtime"])
    with pytest.raises(lifecycle.GateError, match="state ownership migration"):
        lifecycle.preflight()


def test_sku_preflight_fails_closed_on_unadvertised_sku(
    monkeypatch: pytest.MonkeyPatch,
    config: dict[str, str],
) -> None:
    monkeypatch.setattr(
        lifecycle,
        "json_command",
        lambda *a: {"supportedFlexibleServerEditions": [{"name": "Standard_D2s_v3"}]},
    )
    with pytest.raises(lifecycle.GateError, match="B_Standard_B1ms"):
        lifecycle.ensure_postgres_sku(config)
    monkeypatch.setattr(
        lifecycle,
        "json_command",
        lambda *a: {"supportedFlexibleServerEditions": [{"name": "Standard_B1ms"}]},
    )
    lifecycle.ensure_postgres_sku(config)


def test_rollback_does_not_delete_foreign_azure_resources(
    monkeypatch: pytest.MonkeyPatch,
    config: dict[str, str],
    tmp_path: Path,
) -> None:
    record = tmp_path / "baseline.json"
    record.write_text(
        json.dumps(
            {
                "classification": "CLEAN",
                "subscription_id": config["subscription_id"],
                "resource_group_name": config["resource_group_name"],
                "initial_state_count": 0,
                "initial_inventory_count": 0,
            }
        )
    )
    address = "azurerm_servicebus_namespace.bus"
    resource_id = lifecycle.scope(config) + "/providers/Microsoft.ServiceBus/namespaces/bus"
    monkeypatch.setattr(lifecycle, "managed_addresses", lambda: [address])
    fake_plan = {
        "resource_changes": [
            {
                "address": address,
                "type": "azurerm_servicebus_namespace",
                "mode": "managed",
                "change": {"actions": ["delete"], "before": {"id": resource_id}},
            }
        ]
    }

    def fake_json(*args: str) -> dict:
        if args[:3] == ("terraform", "show", "-json"):
            return (
                fake_plan
                if args[-1].endswith(".tfplan")
                else {"values": {"root_module": {"resources": []}}}
            )
        raise AssertionError(f"Unexpected JSON command: {args}")

    monkeypatch.setattr(lifecycle, "json_command", fake_json)
    monkeypatch.setattr(lifecycle, "disposable_ids", lambda *_: {resource_id.lower()})
    monkeypatch.setenv("STATE_ACCOUNT_NAME", "protected-state")
    monkeypatch.setenv("STATE_RESOURCE_GROUP", "protected-rg")
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(lifecycle, "command", lambda *args: commands.append(args) or "")
    with pytest.raises(lifecycle.GateError, match="untracked/foreign Azure resources"):
        lifecycle.rollback(record)
    assert not any(args[:2] == ("terraform", "apply") for args in commands)


def test_state_and_bootstrap_security_guards(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = {
        "subscription_id": "11111111-1111-1111-1111-111111111111",
        "tenant_id": "22222222-2222-2222-2222-222222222222",
        "resource_group_name": "tirodhan-np-test01",
        "location": "centralindia",
        "suffix": "test01",
    }
    monkeypatch.setenv("STATE_ACCOUNT_NAME", "protectedstate")
    monkeypatch.setenv("STATE_RESOURCE_GROUP", "protected-rg")
    app_id = lifecycle.scope(cfg)
    state_id = f"/subscriptions/{cfg['subscription_id']}/resourceGroups/protected-rg"
    vault_id = app_id + "/providers/Microsoft.KeyVault/vaults/td-np-test01-kv"

    def query(*args: str) -> dict:
        if args[:3] == ("az", "account", "show"):
            return {"id": cfg["subscription_id"], "tenantId": cfg["tenant_id"]}
        if args[:3] == ("az", "group", "show"):
            return {"id": app_id if "tirodhan-np-test01" in args else state_id}
        if args[:4] == ("az", "storage", "account", "show"):
            return {"name": "protectedstate", "allowSharedKeyAccess": False}
        if args[:3] == ("az", "keyvault", "show"):
            return {
                "id": vault_id,
                "location": "centralindia",
                "properties": {
                    "enablePurgeProtection": True,
                    "enableRbacAuthorization": True,
                },
            }
        raise AssertionError(f"Unexpected Azure query: {args}")

    monkeypatch.setattr(lifecycle, "json_command", query)
    lifecycle.assert_bootstrap(cfg)

    def insecure_query(*args: str) -> dict:
        result = query(*args)
        if args[:4] == ("az", "storage", "account", "show"):
            return {"name": "protectedstate", "allowSharedKeyAccess": True}
        return result

    monkeypatch.setattr(lifecycle, "json_command", insecure_query)
    with pytest.raises(lifecycle.GateError, match="disable shared-key"):
        lifecycle.assert_bootstrap(cfg)


def test_partial_inventory_must_be_subset_of_remote_state(
    monkeypatch: pytest.MonkeyPatch,
    config: dict[str, str],
) -> None:
    root = lifecycle.scope(config)
    managed_id = root + "/providers/Microsoft.ServiceBus/namespaces/bus"
    assert managed_id.startswith(root)
    monkeypatch.setattr(
        lifecycle,
        "json_command",
        lambda *_: {
            "values": {
                "root_module": {
                    "resources": [
                        {"mode": "managed", "values": {"id": managed_id}},
                    ]
                }
            }
        },
    )
    lifecycle.assert_owned_inventory(config, {managed_id.lower()})
    with pytest.raises(lifecycle.GateError, match="untracked/foreign"):
        lifecycle.assert_owned_inventory(config, {root.lower() + "/providers/Microsoft.App/jobs/foreign"})
    with pytest.raises(lifecycle.GateError, match="out-of-scope"):
        lifecycle.tracked_managed_ids({**config, "resource_group_name": "other-rg"})
