"""NONPROD reconciliation is identity-bound, idempotent and never purges."""

from __future__ import annotations

from typing import Any
from unittest.mock import Mock

import pytest

from tirodhan.deployment import foundation_reconcile as mod


@pytest.fixture
def cfg(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    config = {
        "subscription_id": "11111111-1111-1111-1111-111111111111",
        "tenant_id": "22222222-2222-2222-2222-222222222222",
        "location": "centralindia",
        "resource_group_name": "tirodhan-np-test01",
        "suffix": "test01",
    }
    monkeypatch.setattr(mod, "settings", lambda: config)
    monkeypatch.setattr(mod, "verified_subscription", lambda *_: None)
    return config


def vault(cfg: dict[str, str]) -> dict[str, Any]:
    return {
        "id": mod.vault_id(cfg),
        "name": mod.key_vault_name(cfg),
        "location": cfg["location"],
        "properties": {
            "tenantId": cfg["tenant_id"],
            "enablePurgeProtection": True,
            "enableRbacAuthorization": True,
            "provisioningState": "Succeeded",
        },
    }


def test_active_vault_is_idempotent_without_mutation(
    cfg: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mod, "query", lambda *_: [vault(cfg)])
    call = Mock()
    monkeypatch.setattr(mod, "run", call)
    mod.recover_persistent_vault()
    call.assert_not_called()


def test_soft_deleted_exact_vault_recovers_but_never_purges(
    cfg: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    recovered = {
        "name": mod.key_vault_name(cfg),
        "location": cfg["location"],
        "properties": {"vaultId": mod.vault_id(cfg)},
    }
    seen: list[tuple[str, ...]] = []

    def query(*args: str) -> Any:
        if args[:3] == ("az", "keyvault", "list"):
            return []
        if args[:3] == ("az", "keyvault", "list-deleted"):
            return [recovered]
        if args[:3] == ("az", "keyvault", "show"):
            return vault(cfg)
        raise AssertionError(args)

    monkeypatch.setattr(mod, "query", query)
    monkeypatch.setattr(mod, "run", lambda *args: seen.append(args) or "")
    mod.recover_persistent_vault()
    assert len(seen) == 1
    assert seen[0][1:3] == ("keyvault", "recover")
    assert "purge" not in seen[0]


def test_other_tenant_or_vault_is_blocked(
    cfg: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    wrong = vault(cfg)
    wrong["properties"] = {**wrong["properties"], "tenantId": "different"}
    monkeypatch.setattr(mod, "query", lambda *_: [wrong])
    mutation = Mock()
    monkeypatch.setattr(mod, "run", mutation)
    with pytest.raises(mod.GateError, match="identity/protection"):
        mod.recover_persistent_vault()
    mutation.assert_not_called()


def test_unexpected_legacy_state_address_does_not_mutate_backend(
    cfg: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        mod,
        "run",
        lambda *a: "azurerm_key_vault.runtime\nmodule.foreign.azurerm_key_vault.runtime",
    )
    with pytest.raises(mod.GateError, match="Multiple legacy"):
        mod.release_legacy_state()


def test_legacy_release_requires_verified_snapshot_before_state_rm(
    cfg: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    address = "azurerm_key_vault.runtime"
    calls: list[tuple[str, ...]] = []
    monkeypatch.setenv("STATE_ACCOUNT_NAME", "safe-state")
    monkeypatch.setenv("STATE_RESOURCE_GROUP", "safe-state-rg")
    monkeypatch.setattr(
        mod, "run", lambda *args: calls.append(args) or address
    )

    def query(*args: str) -> Any:
        if args[:3] == ("terraform", "show", "-json"):
            return {
                "values": {"root_module": {"resources": [{
                    "address": address,
                    "mode": "managed",
                    "values": {"id": mod.vault_id(cfg)}
                }]}}
            }
        if args[:3] == ("az", "keyvault", "show"):
            return vault(cfg)
        if args[:4] == ("az", "storage", "blob", "snapshot"):
            return {}
        raise AssertionError(args)

    monkeypatch.setattr(mod, "query", query)
    with pytest.raises(mod.GateError, match="snapshot"):
        mod.release_legacy_state()
    assert not any(x[:3] == ("terraform", "state", "rm") for x in calls)
