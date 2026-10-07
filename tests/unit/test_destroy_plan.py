from __future__ import annotations

from copy import deepcopy

import pytest

from tirodhan.deployment.check_destroy_plan import validate_destroy_plan


def _plan(resource_type: str = "azurerm_container_app") -> dict:
    return {
        "resource_changes": [
            {
                "address": f"{resource_type}.example",
                "mode": "managed",
                "type": resource_type,
                "change": {
                    "actions": ["delete"],
                    "before": {"name": "tirodhan-np-tdnp01-example"},
                },
            }
        ]
    }


def _validate(plan: dict) -> int:
    return validate_destroy_plan(
        plan,
        protected_state_account="tdnpstatetdnp01",
        protected_state_container="tfstate",
    )


def test_destroy_guard_accepts_known_managed_delete() -> None:
    assert _validate(_plan()) == 1


def test_destroy_guard_rejects_resource_group_delete() -> None:
    with pytest.raises(RuntimeError, match="unapproved resource type"):
        _validate(_plan("azurerm_resource_group"))


def test_destroy_guard_rejects_unknown_resource_type() -> None:
    with pytest.raises(RuntimeError, match="unapproved resource type"):
        _validate(_plan("azurerm_virtual_network"))


def test_destroy_guard_rejects_replacement_action() -> None:
    plan = _plan()
    plan["resource_changes"][0]["change"]["actions"] = ["delete", "create"]

    with pytest.raises(RuntimeError, match="replacement/mixed"):
        _validate(plan)


def test_destroy_guard_rejects_state_account_reference() -> None:
    plan = _plan("azurerm_storage_account")
    plan["resource_changes"][0]["change"]["before"]["name"] = "tdnpstatetdnp01"

    with pytest.raises(RuntimeError, match="state account"):
        _validate(plan)


def test_destroy_guard_rejects_state_container_reference() -> None:
    plan = _plan("azurerm_storage_container")
    plan["resource_changes"][0]["change"]["before"]["name"] = "tfstate"

    with pytest.raises(RuntimeError, match="state container"):
        _validate(plan)


def test_destroy_guard_requires_at_least_one_delete() -> None:
    plan = deepcopy(_plan())
    plan["resource_changes"][0]["change"]["actions"] = ["no-op"]

    with pytest.raises(RuntimeError, match="no managed deletes"):
        _validate(plan)
