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
