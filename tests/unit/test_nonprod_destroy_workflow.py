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
    assert text.count("python -m tirodhan.deployment.check_destroy_plan") == 3


def test_nonprod_destroy_allows_only_explicit_terminal_convergence() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert text.count("--allow-empty") == 2
    assert "terraform state list" in text
    assert "remaining_azure_resources=" in text
    assert "az resource list \\" in text
    assert '--resource-group "$NONPROD_RESOURCE_GROUP"' in text
    assert "Active Azure resources remain in the NONPROD application RG:" in text
    assert "az account get-access-token \\" in text
    assert "--resource https://management.azure.com/ \\" in text
    assert "python -m tirodhan.deployment.check_nonprod_bootstrap_rbac principal" in text
    assert '--client-id "$ARM_CLIENT_ID"' in text
    assert '--tenant-id "$ARM_TENANT_ID"' in text
    assert "az role assignment list \\" in text
    assert '--resource-group "$NONPROD_RESOURCE_GROUP"' in text
    assert "--fill-principal-name false \\" in text
    assert "python -m tirodhan.deployment.check_nonprod_bootstrap_rbac verify" in text
    assert '--expected-scope "$DEPLOYMENT_SCOPE"' in text
    assert '--expected-principal-id "$deployment_principal_id"' in text
    assert '--expected-suffix "${{ inputs.expected_suffix }}"' in text


def test_nonprod_destroy_preserves_backend_boundary_checks() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert '[[ "$STATE_RESOURCE_GROUP" == "$NONPROD_RESOURCE_GROUP" ]]' in text
    assert '--protected-state-account "$STATE_ACCOUNT_NAME"' in text
    assert "--protected-state-container tfstate" in text
    assert "az storage container show" in text


def test_nonprod_destroy_aca_polling_error_requires_authoritative_convergence() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["destroy-apply"]["steps"]
    apply = next(step for step in steps if step.get("id") == "destroy_apply")
    recovery = next(
        step for step in steps
        if step.get("name") == "Verify and reconcile only an empty post-failure destroy"
    )
    assert "terraform apply -input=false destroy.tfplan" in apply["run"]
    assert "apply_status=$?" in apply["run"]
    assert 'grep -c \'Error:\'' in apply["run"]
    assert 'polling support for the Content-Type "" was not implemented' in apply["run"]
    assert 'Managed Environment Name:' in apply["run"]
    assert 'reconcile=true' in apply["run"]
    assert 'exit "$apply_status"' in apply["run"]

    assert recovery["if"] == "steps.destroy_apply.outputs.reconcile == 'true'"
    assert "az resource list" in recovery["run"]
    assert "empty_observations >= 2" in recovery["run"]
    assert (
        "terraform plan -destroy -input=false -out=terminal-convergence.tfplan"
        in recovery["run"]
    )
    assert "--require-empty" in recovery["run"]
    assert "terraform apply -input=false terminal-convergence.tfplan" in recovery["run"]
    assert "terraform state rm" not in recovery["run"]
