"""Read-only foundation preflight and guarded first-apply rollback.

Never touches bootstrap-owned state, credentials, or resources outside the
approved application resource group. No implicit privilege escalation.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from tirodhan.deployment.check_destroy_plan import validate_destroy_plan


class GateError(RuntimeError):
    """An actionable deployment boundary failure."""


def command(*args: str) -> str:
    timeout = 1800 if args[:2] == ("terraform", "apply") else 180
    result = subprocess.run(list(args), check=True, capture_output=True, text=True, timeout=timeout)
    return result.stdout.strip()


def json_command(*args: str) -> Any:
    return json.loads(command(*args))


def settings() -> dict[str, Any]:
    return json.loads(Path("nonprod.auto.tfvars.json").read_text(encoding="utf-8"))


def scope(config: dict[str, Any]) -> str:
    return (
        f"/subscriptions/{config['subscription_id']}/resourceGroups/{config['resource_group_name']}"
    )


def key_vault_name(config: dict[str, Any]) -> str:
    return f"td-np-{config['suffix']}-kv"


def inventory(config: dict[str, Any]) -> list[dict[str, Any]]:
    resources = json_command(
        "az",
        "resource",
        "list",
        "--resource-group",
        config["resource_group_name"],
        "--subscription",
        config["subscription_id"],
        "--output",
        "json",
    )
    if not isinstance(resources, list):
        raise GateError("Azure resource inventory was not a JSON list")
    return resources


def disposable_ids(config: dict[str, Any]) -> set[str]:
    prefix = scope(config).lower() + "/"
    vault_id = (
        scope(config) + "/providers/Microsoft.KeyVault/vaults/" + key_vault_name(config)
    ).lower()
    result = set()
    for item in inventory(config):
        resource_id = item.get("id")
        if not isinstance(resource_id, str) or not resource_id.lower().startswith(prefix):
            raise GateError("Azure inventory contains an out-of-scope resource")
        if resource_id.lower() != vault_id:
            result.add(resource_id.lower())
    return result


def tracked_managed_ids(config: dict[str, Any]) -> set[str]:
    state = json_command("terraform", "show", "-json")
    root = (state.get("values") or {}).get("root_module", {})
    prefix = scope(config).lower() + "/"
    ids: set[str] = set()

    def visit(module: dict[str, Any]) -> None:
        for resource in module.get("resources", []):
            if resource.get("mode") != "managed":
                continue
            resource_id = resource.get("values", {}).get("id")
            if not isinstance(resource_id, str) or not resource_id.lower().startswith(prefix):
                raise GateError("Managed Terraform state contains an out-of-scope ID")
            ids.add(resource_id.lower())
        for child in module.get("child_modules", []):
            visit(child)

    visit(root)
    return ids


def assert_owned_inventory(config: dict[str, Any], active: set[str]) -> None:
    owned = tracked_managed_ids(config)
    if any(
        item not in owned and not any(item.startswith(parent + "/") for parent in owned)
        for item in active
    ):
        raise GateError("BLOCKED_RECONCILIATION: untracked/foreign Azure resources")


def state_addresses() -> list[str]:
    return [line for line in command("terraform", "state", "list").splitlines() if line]


def managed_addresses() -> list[str]:
    return [item for item in state_addresses() if not item.startswith("data.")]


def assert_bootstrap(config: dict[str, Any]) -> None:
    actual = json_command("az", "account", "show", "--output", "json")
    if actual.get("id", "").lower() != config["subscription_id"].lower():
        raise GateError("Authenticated Azure subscription differs from approved configuration")
    if actual.get("tenantId", "").lower() != config["tenant_id"].lower():
        raise GateError("Authenticated Azure tenant differs from approved configuration")

    app = json_command(
        "az",
        "group",
        "show",
        "--name",
        config["resource_group_name"],
        "--subscription",
        config["subscription_id"],
        "--output",
        "json",
    )
    state_name = os.environ["STATE_RESOURCE_GROUP"]
    state_account = os.environ["STATE_ACCOUNT_NAME"]
    if state_name == config["resource_group_name"]:
        raise GateError("Bootstrap and application resource groups must differ")
    state = json_command(
        "az",
        "group",
        "show",
        "--name",
        state_name,
        "--subscription",
        config["subscription_id"],
        "--output",
        "json",
    )
    for resource, name in ((app, config["resource_group_name"]), (state, state_name)):
        expected = f"/subscriptions/{config['subscription_id']}/resourceGroups/{name}"
        if str(resource.get("id", "")).lower() != expected.lower():
            raise GateError("Bootstrap resource group identity mismatch")

    account = json_command(
        "az",
        "storage",
        "account",
        "show",
        "--name",
        state_account,
        "--resource-group",
        state_name,
        "--subscription",
        config["subscription_id"],
        "--output",
        "json",
    )
    if account.get("allowSharedKeyAccess") is not False:
        raise GateError("Bootstrap state account must disable shared-key access")
    if account.get("name") != state_account:
        raise GateError("Bootstrap state account name mismatch")
    # The operator bootstrap owns this vault. Never restore/delete it here.
    vault = json_command(
        "az",
        "keyvault",
        "show",
        "--name",
        key_vault_name(config),
        "--resource-group",
        config["resource_group_name"],
        "--subscription",
        config["subscription_id"],
        "--output",
        "json",
    )
    props = vault.get("properties", {})
    expected_vault = (
        scope(config) + "/providers/Microsoft.KeyVault/vaults/" + key_vault_name(config)
    ).lower()
    if (
        str(vault.get("id", "")).lower() != expected_vault
        or str(vault.get("location", "")).lower() != str(config["location"]).lower()
        or props.get("enablePurgeProtection") is not True
        or props.get("enableRbacAuthorization") is not True
    ):
        raise GateError("Persistent bootstrap Key Vault identity/security mismatch")


def sku_candidates(tree: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(tree, list):
        for value in tree:
            found.update(sku_candidates(value))
    elif isinstance(tree, dict):
        for key, value in tree.items():
            if key in {"name", "skuName", "sku_name"} and isinstance(value, str):
                found.add(value.lower())
            found.update(sku_candidates(value))
    return found


def ensure_postgres_sku(config: dict[str, Any]) -> None:
    # The approved hardcoded SKU is not substituted with a more expensive one.
    # The list API is a capability signal, not a guarantee of live Azure capacity.
    names = sku_candidates(
        json_command(
            "az",
            "postgres",
            "flexible-server",
            "list-skus",
            "--location",
            config["location"],
            "--subscription",
            config["subscription_id"],
            "--output",
            "json",
        )
    )
    if not ({"b_standard_b1ms", "standard_b1ms"} & names):
        raise GateError(
            "B_Standard_B1ms is not advertised for this subscription/region; "
            "review PostgreSQL cost/region and the approved Terraform SKU"
        )


def preflight(*, baseline: Path | None = None) -> str:
    cfg = settings()
    assert_bootstrap(cfg)
    addresses = state_addresses()
    if any("azurerm_key_vault.runtime" in line for line in addresses):
        raise GateError(
            "Legacy Key Vault remains in application Terraform state; "
            "an approved state ownership migration is required"
        )
    outputs = json_command("terraform", "output", "-json")
    if not isinstance(outputs, dict):
        raise GateError("Terraform output shape is unexpected")
    stage = outputs.get("deployment_stage", {}).get("value")
    if stage not in (None, 0):
        raise GateError("Foundation-only operation cannot target a deployed environment")
    ids = disposable_ids(cfg)
    if not addresses and ids:
        raise GateError(
            "Foreign/untracked Azure resources exist in application RG; "
            "no automatic adoption or deletion"
        )
    if managed_addresses():
        assert_owned_inventory(cfg, ids)
    ensure_postgres_sku(cfg)
    state = "CLEAN" if not addresses and not ids else "PARTIAL"
    if baseline is not None:
        baseline.write_text(
            json.dumps(
                {
                    "classification": state,
                    "subscription_id": cfg["subscription_id"],
                    "resource_group_name": cfg["resource_group_name"],
                    "initial_state_count": len(addresses),
                    "initial_inventory_count": len(ids),
                }
            ),
            encoding="utf-8",
        )
    print(f"FOUNDATION_PREFLIGHT_{state}: bootstrap verified; PostgreSQL SKU advertised")
    return state


def verify_clean() -> None:
    cfg = settings()
    assert_bootstrap(cfg)
    if managed_addresses():
        raise GateError("Terraform managed application state is not empty")
    remaining = disposable_ids(cfg)
    if remaining:
        raise GateError(
            f"BLOCKED_RECONCILIATION: {len(remaining)} disposable/foreign Azure resource(s) remain"
        )
    print("CLEAN_ROLLBACK: zero disposable state entries and Azure resources")


def rollback(baseline: Path) -> None:
    cfg = settings()
    evidence = json.loads(baseline.read_text(encoding="utf-8"))
    if (
        evidence.get("classification") != "CLEAN"
        or evidence.get("subscription_id") != cfg["subscription_id"]
        or evidence.get("resource_group_name") != cfg["resource_group_name"]
        or evidence.get("initial_state_count") != 0
        or evidence.get("initial_inventory_count") != 0
    ):
        raise GateError("BLOCKED_RECONCILIATION: pre-existing state is never auto-destroyed")
    if not managed_addresses():
        verify_clean()
        return

    command("terraform", "plan", "-destroy", "-input=false", "-out=foundation-rollback.tfplan")
    plan = json_command("terraform", "show", "-json", "foundation-rollback.tfplan")
    checked = validate_destroy_plan(
        plan,
        expected_subscription_id=cfg["subscription_id"],
        expected_resource_group=cfg["resource_group_name"],
        protected_state_account=os.environ["STATE_ACCOUNT_NAME"],
        protected_state_container="tfstate",
    )
    if checked != len(managed_addresses()):
        raise GateError("BLOCKED_RECONCILIATION: delete set does not equal tracked state")

    # Do not delete untracked Azure assets, even after a failed apply.
    existing = disposable_ids(cfg)
    assert_owned_inventory(cfg, existing)
    command("terraform", "apply", "-input=false", "foundation-rollback.tfplan")
    verify_clean()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "operation", choices=["preflight", "rollback", "verify-clean", "verify-foundation"]
    )
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args()
    try:
        if args.operation == "preflight":
            preflight(baseline=args.baseline)
        elif args.operation == "rollback":
            if args.baseline is None:
                raise GateError("Rollback requires a recorded baseline")
            rollback(args.baseline)
        elif args.operation == "verify-clean":
            verify_clean()
        else:
            cfg = settings()
            assert_bootstrap(cfg)
            if command("terraform", "output", "-raw", "deployment_stage") != "0":
                raise GateError("Foundation stage is not zero")
            if not managed_addresses() or not disposable_ids(cfg):
                raise GateError("Foundation resources are missing")
            print("FOUNDATION_READY: verified bootstrap and deployed foundation")
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        KeyError,
        ValueError,
        OSError,
        GateError,
    ) as exc:
        raise SystemExit(f"BLOCKED_RECONCILIATION: {type(exc).__name__}: {exc}") from exc


if __name__ == "__main__":
    main()
