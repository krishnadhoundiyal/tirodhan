from __future__ import annotations

from copy import deepcopy

import pytest
from lifecycle_fakes import MemoryObservations

from tirodhan.deployment.check_destroy_plan import validate_destroy_plan
from tirodhan.deployment.deployment_admission import AdmissionDenied
from tirodhan.deployment.destroy_preservation import verify_gateway_removed
from tirodhan.deployment.lifecycle_evidence import ObservationError

SUBSCRIPTION_ID = "362d539c-c919-481e-97f5-1a9cda362747"
RESOURCE_GROUP = "tirodhan-np-tdnp01"


def _plan(resource_type: str = "azurerm_container_app") -> dict:
    return {
        "resource_changes": [
            {
                "address": f"{resource_type}.example",
                "mode": "managed",
                "type": resource_type,
                "change": {
                    "actions": ["delete"],
                    "before": {
                        "id": (
                            f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/"
                            f"{RESOURCE_GROUP}/providers/Microsoft.App/containerApps/example"
                        ),
                        "name": "tirodhan-np-tdnp01-example",
                    },
                },
            }
        ]
    }


def _validate(plan: dict, *, allow_empty: bool = False, require_empty: bool = False) -> int:
    return validate_destroy_plan(
        plan,
        expected_subscription_id=SUBSCRIPTION_ID,
        expected_resource_group=RESOURCE_GROUP,
        protected_state_account="tdnpstatetdnp01",
        protected_state_container="tfstate",
        allow_empty=allow_empty,
        require_empty=require_empty,
    )


def test_destroy_guard_accepts_known_managed_delete() -> None:
    assert _validate(_plan()) == 1


@pytest.mark.parametrize("name", ["tirodhan-np-tdnp01-apim", "unrelated-apim"])
def test_destroy_guard_only_accepts_exact_nonprod_gateway(name: str) -> None:
    plan = _plan("azurerm_api_management")
    row = plan["resource_changes"][0]
    row["address"] = "azurerm_api_management.nonprod[0]"
    row["change"]["before"]["id"] = (
        f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/{RESOURCE_GROUP}"
        f"/providers/Microsoft.ApiManagement/service/{name}"
    )
    if name == "tirodhan-np-tdnp01-apim":
        assert _validate(plan) == 1
    else:
        with pytest.raises(RuntimeError, match="approved NONPROD APIM instance"):
            _validate(plan)


def test_destroy_guard_accepts_case_insensitive_azure_scope() -> None:
    plan = _plan()
    plan["resource_changes"][0]["change"]["before"]["id"] = (
        f"/SUBSCRIPTIONS/{SUBSCRIPTION_ID.upper()}/RESOURCEGROUPS/"
        f"{RESOURCE_GROUP.upper()}/providers/Microsoft.App/containerApps/example"
    )

    assert _validate(plan) == 1


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


def test_destroy_guard_rejects_wrong_subscription() -> None:
    plan = _plan()
    plan["resource_changes"][0]["change"]["before"]["id"] = (
        "/subscriptions/00000000-0000-0000-0000-000000000000/"
        f"resourceGroups/{RESOURCE_GROUP}/providers/Microsoft.App/containerApps/example"
    )

    with pytest.raises(RuntimeError, match="outside expected subscription/resource group"):
        _validate(plan)


def test_destroy_guard_rejects_wrong_resource_group() -> None:
    plan = _plan()
    plan["resource_changes"][0]["change"]["before"]["id"] = (
        f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/not-tirodhan/"
        "providers/Microsoft.App/containerApps/example"
    )

    with pytest.raises(RuntimeError, match="outside expected subscription/resource group"):
        _validate(plan)


def test_destroy_guard_rejects_missing_resource_id() -> None:
    plan = _plan()
    del plan["resource_changes"][0]["change"]["before"]["id"]

    with pytest.raises(RuntimeError, match="without an Azure resource ID"):
        _validate(plan)


def test_destroy_guard_requires_at_least_one_delete_by_default() -> None:
    plan = deepcopy(_plan())
    plan["resource_changes"][0]["change"]["actions"] = ["no-op"]

    with pytest.raises(RuntimeError, match="no managed deletes"):
        _validate(plan)


def test_destroy_guard_allows_empty_terminal_convergence_when_explicit() -> None:
    plan = deepcopy(_plan())
    plan["resource_changes"][0]["change"]["actions"] = ["no-op"]

    assert _validate(plan, allow_empty=True) == 0


def test_destroy_guard_rejects_create_even_when_empty_is_allowed() -> None:
    plan = deepcopy(_plan())
    plan["resource_changes"][0]["change"]["actions"] = ["create"]

    with pytest.raises(RuntimeError, match="non-destroy managed action"):
        _validate(plan, allow_empty=True)


def test_destroy_guard_ignores_non_managed_read_in_terminal_plan() -> None:
    plan = {
        "resource_changes": [
            {
                "address": "data.azurerm_resource_group.nonprod",
                "mode": "data",
                "type": "azurerm_resource_group",
                "change": {
                    "actions": ["read"],
                    "before": None,
                },
            }
        ]
    }

    assert _validate(plan, allow_empty=True) == 0


def test_post_failure_convergence_rejects_any_remaining_delete() -> None:
    with pytest.raises(RuntimeError, match="requires zero remaining managed deletes"):
        _validate(_plan("azurerm_container_app_environment"), require_empty=True)


def test_post_failure_convergence_accepts_only_zero_delete() -> None:
    plan = deepcopy(_plan())
    plan["resource_changes"][0]["change"]["actions"] = ["no-op"]

    assert _validate(plan, require_empty=True) == 0


def test_post_failure_convergence_rejects_new_resource_creation() -> None:
    plan = deepcopy(_plan())
    plan["resource_changes"][0]["change"]["actions"] = ["create"]

    with pytest.raises(RuntimeError, match="non-destroy managed action"):
        _validate(plan, require_empty=True)


@pytest.mark.parametrize("active,deleted", [(True, False), (False, True), (False, False)])
def test_gateway_teardown_distinguishes_active_absent_and_reserved(
    monkeypatch, capsys, active: bool, deleted: bool
) -> None:
    io = MemoryObservations()
    io.tf["apim_publisher_email"] = "publisher@example.invalid"

    def arm(resource_id: str, version: str) -> dict[str, str] | None:
        assert version == "2024-05-01"
        assert resource_id.endswith("/tirodhan-np-test01-apim")
        present = active if resource_id.startswith(io.scope + "/") else deleted
        return {"id": resource_id} if present else None

    monkeypatch.setattr(io, "arm", arm)
    if active:
        with pytest.raises(AdmissionDenied, match="NONPROD_APIM_STILL_ACTIVE"):
            verify_gateway_removed(io)
    else:
        verify_gateway_removed(io)
        output = capsys.readouterr().out
        result = "NONPROD_APIM_INACTIVE_NAME_RESERVED" if deleted else "NONPROD_APIM_ABSENT"
        assert output.strip() == result


def test_gateway_teardown_cannot_treat_denied_read_as_absence(monkeypatch) -> None:
    io = MemoryObservations()
    io.tf["apim_publisher_email"] = "publisher@example.invalid"

    def deny(*args):
        raise ObservationError("AuthorizationFailed")

    monkeypatch.setattr(io, "arm", deny)
    with pytest.raises(AdmissionDenied, match="AuthorizationFailed"):
        verify_gateway_removed(io)
