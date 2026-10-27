"""Idempotent, fail-closed NONPROD recovery operations with separate trust boundaries.

Only the protected recovery principal can recover an exact-name soft-deleted vault.
The restricted deployment principal alone can release a *verified* legacy state
address after backing up the remote state blob. Neither operation purges anything.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from typing import Any

from tirodhan.deployment.foundation_lifecycle import GateError, key_vault_name, scope, settings


def run(*args: str) -> str:
    return subprocess.run(
        list(args), check=True, capture_output=True, text=True, timeout=180
    ).stdout.strip()


def query(*args: str) -> Any:
    return json.loads(run(*args))


def verified_subscription(cfg: dict[str, Any]) -> None:
    current = query("az", "account", "show", "--output", "json")
    if (
        current.get("id", "").lower() != cfg["subscription_id"].lower()
        or current.get("tenantId", "").lower() != cfg["tenant_id"].lower()
    ):
        raise GateError("Recovery identity subscription/tenant mismatch")


def vault_id(cfg: dict[str, Any]) -> str:
    return scope(cfg) + "/providers/Microsoft.KeyVault/vaults/" + key_vault_name(cfg)


def verify_vault(cfg: dict[str, Any], vault: dict[str, Any]) -> None:
    props = vault.get("properties") or {}
    if (
        str(vault.get("id", "")).lower() != vault_id(cfg).lower()
        or str(vault.get("location", "")).lower() != cfg["location"].lower()
        or str(props.get("tenantId", "")).lower() != cfg["tenant_id"].lower()
        or props.get("enablePurgeProtection") is not True
        or props.get("enableRbacAuthorization") is not True
        or str(props.get("provisioningState", "")).lower() not in {"succeeded", ""}
    ):
        raise GateError("Persistent Key Vault does not match approved identity/protection")


def recover_persistent_vault() -> None:
    cfg = settings()
    verified_subscription(cfg)
    # Never create bootstrap resources here. Bootstrap script owns first creation.
    name = key_vault_name(cfg)
    active = query(
        "az", "keyvault", "list", "--resource-group", cfg["resource_group_name"],
        "--subscription", cfg["subscription_id"], "--output", "json"
    )
    if not isinstance(active, list):
        raise GateError("Active Key Vault inventory unavailable")
    named = [v for v in active if v.get("name", "").lower() == name.lower()]
    if len(named) > 1:
        raise GateError("Ambiguous active vault ownership")
    if named:
        verify_vault(cfg, named[0])
        print("PERSISTENT_VAULT_READY: exact active vault verified")
        return

    deleted = query(
        "az", "keyvault", "list-deleted", "--subscription", cfg["subscription_id"],
        "--resource-type", "vault", "--output", "json"
    )
    if not isinstance(deleted, list):
        raise GateError("Deleted Key Vault inventory unavailable")
    matching = [v for v in deleted if v.get("name", "").lower() == name.lower()]
    if len(matching) != 1:
        raise GateError("No uniquely matching deleted vault; operator bootstrap required")
    candidate = matching[0]
    if (
        str(candidate.get("properties", {}).get("vaultId", "")).lower() != vault_id(cfg).lower()
        or str(candidate.get("location", "")).lower() != cfg["location"].lower()
    ):
        raise GateError("Soft-deleted Key Vault has a foreign identity or location")
    run(
        "az", "keyvault", "recover", "--subscription", cfg["subscription_id"],
        "--name", name, "--resource-group", cfg["resource_group_name"],
        "--location", cfg["location"], "--output", "none"
    )
    verified = query(
        "az", "keyvault", "show", "--subscription", cfg["subscription_id"],
        "--name", name, "--resource-group", cfg["resource_group_name"],
        "--output", "json"
    )
    verify_vault(cfg, verified)
    print("PERSISTENT_VAULT_RECOVERED: exact identity verified; no purge")


def release_legacy_state() -> None:
    cfg = settings()
    verified_subscription(cfg)
    # No direct editing state outside its configured remote backend.
    # Backend and state container are created by operator bootstrap.
    address = "azurerm_key_vault.runtime"
    raw = run("terraform", "state", "list").splitlines()
    if address not in raw:
        print("LEGACY_VAULT_STATE_ALREADY_RELEASED")
        return
    if any(x.endswith(".azurerm_key_vault.runtime") for x in raw if x != address):
        raise GateError("Multiple legacy Key Vault state addresses")
    # Terraform state show has no JSON variant; use Terraform show -json instead.
    state = query("terraform", "show", "-json")
    entries = (state.get("values") or {}).get("root_module", {}).get("resources", [])
    matching = [x for x in entries if x.get("address") == address and x.get("mode") == "managed"]
    if len(matching) != 1:
        raise GateError("Legacy Key Vault state entry missing or ambiguous")
    if str(matching[0].get("values", {}).get("id", "")).lower() != vault_id(cfg).lower():
        raise GateError("Legacy Key Vault state ID is not the bootstrap vault")
    active = query(
        "az", "keyvault", "show", "--subscription", cfg["subscription_id"],
        "--name", key_vault_name(cfg), "--resource-group", cfg["resource_group_name"],
        "--output", "json"
    )
    verify_vault(cfg, active)

    account = os.environ["STATE_ACCOUNT_NAME"]
    state_rg = os.environ["STATE_RESOURCE_GROUP"]
    if state_rg.lower() == cfg["resource_group_name"].lower():
        raise GateError("Backend resource group cannot be application group")
    # Write-once Azure blob snapshot before Terraform state mutation.
    snapshot = query(
        "az", "storage", "blob", "snapshot", "--account-name", account,
        "--container-name", "tfstate", "--name", "tirodhan/nonprod.tfstate",
        "--auth-mode", "login", "--output", "json"
    )
    if not isinstance(snapshot, dict) or not (
        snapshot.get("snapshot") or snapshot.get("snapshotTime")
    ):
        raise GateError("Backend snapshot missing; refusing Terraform state mutation")
    print("Verified backend snapshot before legacy state ownership release")
    run("terraform", "state", "rm", "-lock=true", address)
    if address in run("terraform", "state", "list").splitlines():
        raise GateError("Legacy Key Vault state entry persists after release")
    print("LEGACY_VAULT_STATE_RELEASED: bootstrap vault retained and state snapshotted")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("recover-vault", "release-legacy-state"))
    args = parser.parse_args()
    try:
        if args.operation == "recover-vault":
            recover_persistent_vault()
        else:
            release_legacy_state()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, KeyError, OSError,
            ValueError, GateError) as exc:
        raise SystemExit(f"BLOCKED_RECONCILIATION: {type(exc).__name__}: {exc}") from exc


if __name__ == "__main__":
    main()
