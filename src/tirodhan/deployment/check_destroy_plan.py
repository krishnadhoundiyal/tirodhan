"""Fail closed unless a Terraform destroy plan contains only approved NONPROD resources."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from typing import Any

ALLOWED_DESTROY_TYPES = frozenset(
    {
        "azurerm_container_app",
        "azurerm_container_app_environment",
        "azurerm_container_app_job",
        "azurerm_key_vault",
        "azurerm_postgresql_flexible_server",
        "azurerm_postgresql_flexible_server_active_directory_administrator",
        "azurerm_postgresql_flexible_server_configuration",
        "azurerm_postgresql_flexible_server_database",
        "azurerm_postgresql_flexible_server_firewall_rule",
        "azurerm_role_assignment",
        "azurerm_servicebus_namespace",
        "azurerm_servicebus_queue",
        "azurerm_storage_account",
        "azurerm_storage_container",
        "azurerm_storage_management_policy",
        "azurerm_user_assigned_identity",
    }
)


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def validate_destroy_plan(
    plan: dict[str, Any],
    *,
    protected_state_account: str,
    protected_state_container: str,
) -> int:
    delete_count = 0

    for change in plan.get("resource_changes", []):
        actions = change.get("change", {}).get("actions", [])
        if "delete" not in actions:
            continue

        address = change.get("address", "<unknown>")
        resource_type = change.get("type", "")

        if change.get("mode", "managed") != "managed":
            raise RuntimeError(f"Refusing non-managed delete: {address}")
        if actions != ["delete"]:
            raise RuntimeError(\n                f"Refusing replacement/mixed destroy action for {address}: {actions}"\n            )
        if resource_type not in ALLOWED_DESTROY_TYPES:
            raise RuntimeError(
                f"Refusing destroy of unapproved resource type {resource_type!r}: {address}"
            )

        before_strings = set(_strings(change.get("change", {}).get("before")))
        if protected_state_account in before_strings:
            raise RuntimeError(f"Refusing destroy that references state account: {address}")
        if protected_state_container in before_strings:
            raise RuntimeError(f"Refusing destroy that references state container: {address}")

        delete_count += 1

    if delete_count == 0:
        raise RuntimeError("Destroy plan contains no managed deletes")

    return delete_count


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--protected-state-account", required=True)
    parser.add_argument("--protected-state-container", required=True)
    args = parser.parse_args()

    plan = json.load(sys.stdin)
    count = validate_destroy_plan(
        plan,
        protected_state_account=args.protected_state_account,
        protected_state_container=args.protected_state_container,
    )
    print(
        f"PASS: destroy plan contains {count} approved managed deletes; "
        "backend resources are excluded"
    )


if __name__ == "__main__":
    main()
