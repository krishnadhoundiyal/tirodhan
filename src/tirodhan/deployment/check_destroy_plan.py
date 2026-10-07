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


def _expected_scope(subscription_id: str, resource_group: str) -> str:
    return f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}".lower()


def validate_destroy_plan(
    plan: dict[str, Any],
    *,
    expected_subscription_id: str,
    expected_resource_group: str,
    protected_state_account: str,
    protected_state_container: str,
) -> int:
    delete_count = 0
    expected_scope = _expected_scope(expected_subscription_id, expected_resource_group)

    for change in plan.get("resource_changes", []):
        actions = change.get("change", {}).get("actions", [])
        if "delete" not in actions:
            continue

        address = change.get("address", "<unknown>")
        resource_type = change.get("type", "")

        if change.get("mode", "managed") != "managed":
            raise RuntimeError(f"Refusing non-managed delete: {address}")
        if actions != ["delete"]:
            raise RuntimeError(
                f"Refusing replacement/mixed destroy action for {address}: {actions}"
            )
        if resource_type not in ALLOWED_DESTROY_TYPES:
            raise RuntimeError(
                f"Refusing destroy of unapproved resource type {resource_type!r}: {address}"
            )

        before = change.get("change", {}).get("before")
        before_strings = set(_strings(before))
        if protected_state_account in before_strings:
            raise RuntimeError(f"Refusing destroy that references state account: {address}")
        if protected_state_container in before_strings:
            raise RuntimeError(f"Refusing destroy that references state container: {address}")

        resource_id = before.get("id") if isinstance(before, dict) else None
        if not isinstance(resource_id, str) or not resource_id.strip():
            raise RuntimeError(f"Refusing destroy without an Azure resource ID: {address}")

        normalized_id = resource_id.lower()
        if not normalized_id.startswith(expected_scope + "/"):
            raise RuntimeError(
                "Refusing destroy outside expected subscription/resource group: "
                f"{address} ({resource_id})"
            )

        delete_count += 1

    if delete_count == 0:
        raise RuntimeError("Destroy plan contains no managed deletes")

    return delete_count


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-subscription-id", required=True)
    parser.add_argument("--expected-resource-group", required=True)
    parser.add_argument("--protected-state-account", required=True)
    parser.add_argument("--protected-state-container", required=True)
    args = parser.parse_args()

    plan = json.load(sys.stdin)
    count = validate_destroy_plan(
        plan,
        expected_subscription_id=args.expected_subscription_id,
        expected_resource_group=args.expected_resource_group,
        protected_state_account=args.protected_state_account,
        protected_state_container=args.protected_state_container,
    )
    print(
        f"PASS: destroy plan contains {count} approved managed deletes inside "
        "the expected application scope; backend resources are excluded"
    )


if __name__ == "__main__":
    main()
