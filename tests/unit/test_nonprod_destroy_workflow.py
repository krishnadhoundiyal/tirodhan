from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOW = Path(".github/workflows/nonprod-destroy.yml")


def test_nonprod_destroy_is_manual_main_only_and_approval_gated() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]

    assert "workflow_dispatch" in workflow[True]
    assert jobs["destroy-plan"]["environment"] == {
        "name": "nonprod-plan",
        "deployment": False,
    }
    assert jobs["destroy-apply"]["environment"] == "nonprod"
    assert jobs["destroy-apply"]["needs"] == "destroy-plan"

    for job_name in ("destroy-plan", "destroy-apply"):
        assert "github.ref == 'refs/heads/main'" in jobs[job_name]["if"]
        assert "inputs.confirmation == 'DESTROY_NONPROD'" in jobs[job_name]["if"]


def test_nonprod_destroy_is_resumable_after_partial_apply() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "terraform output -raw resource_group_name" not in text
    assert "terraform output -raw deployment_stage" not in text
    assert "terraform output -raw storage_account_name" not in text

    assert "configured_subscription=" in text
    assert "configured_rg=" in text
    assert "configured_suffix=" in text
    assert '--expected-subscription-id "$ARM_SUBSCRIPTION_ID"' in text
    assert '--expected-resource-group "$NONPROD_RESOURCE_GROUP"' in text
    assert text.count("python -m tirodhan.deployment.check_destroy_plan") == 2


def test_nonprod_destroy_preserves_backend_boundary_checks() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert '[[ "$STATE_RESOURCE_GROUP" == "$NONPROD_RESOURCE_GROUP" ]]' in text
    assert '--protected-state-account "$STATE_ACCOUNT_NAME"' in text
    assert "--protected-state-container tfstate" in text
    assert "terraform state list" in text
    assert "az storage container show" in text


def test_persistent_key_vault_and_preflight_rollback_wiring() -> None:
    foundation = Path(".github/workflows/nonprod-foundation.yml").read_text()
    teardown = WORKFLOW.read_text()
    assert foundation.count("foundation_lifecycle preflight") == 2
    assert "foundation_lifecycle rollback" in foundation
    assert "foundation_lifecycle verify-foundation" in foundation
    assert "trap on_failure ERR" in foundation
    assert "foundation_lifecycle verify-clean" in teardown
    assert "az keyvault show" in teardown
    assert "az keyvault purge" not in teardown
    check = Path("src/tirodhan/deployment/check_destroy_plan.py").read_text()
    assert '"azurerm_key_vault",' not in check
    stack = Path("infra/terraform/nonprod/key_vault.tf").read_text()
    assert 'data "azurerm_key_vault" "runtime"' in stack
    assert 'resource "azurerm_key_vault"' not in stack


def test_reconciliation_precedes_any_foundation_or_destroy_plan() -> None:
    foundation = yaml.safe_load(Path(".github/workflows/nonprod-foundation.yml").read_text())
    destroy = yaml.safe_load(WORKFLOW.read_text())
    assert foundation["jobs"]["foundation-plan"]["needs"] == "state-reconcile"
    assert foundation["jobs"]["state-reconcile"]["needs"] == "recovery"
    assert foundation["jobs"]["recovery"]["environment"] == "nonprod-recovery"
    assert destroy["jobs"]["destroy-plan"]["needs"] == "destroy-state-reconcile"
    assert destroy["jobs"]["destroy-state-reconcile"]["needs"] == "destroy-recovery"
    assert destroy["jobs"]["destroy-recovery"]["environment"] == "nonprod-recovery"

    for workflow in (foundation, destroy):
        text = str(workflow)
        assert "foundation_reconcile recover-vault" in text
        assert "foundation_reconcile release-legacy-state" in text
        assert "nonprod-recovery requires protected branches and human reviewers" in text
        assert "az keyvault purge" not in text


def test_repeated_teardown_requires_verified_clean_azure_inventory() -> None:
    from tirodhan.deployment.check_destroy_plan import validate_destroy_plan

    kwargs = {
        "expected_subscription_id": "11111111-1111-1111-1111-111111111111",
        "expected_resource_group": "tirodhan-np-test01",
        "protected_state_account": "protectedstate",
        "protected_state_container": "tfstate",
    }
    with __import__("pytest").raises(RuntimeError, match="no managed deletes"):
        validate_destroy_plan({"resource_changes": []}, **kwargs)
    assert validate_destroy_plan(
        {"resource_changes": []}, allow_empty=True, **kwargs
    ) == 0
    text = WORKFLOW.read_text()
    assert text.count("--allow-empty") == 2
    assert 'if [[ "$delete_count" == "0" ]]; then' in text
    assert "foundation_lifecycle verify-clean" in text
