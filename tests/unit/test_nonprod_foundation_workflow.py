from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github/workflows"


def test_foundation_workflow_is_manual_main_only_and_approval_gated() -> None:
    foundation = yaml.safe_load((WORKFLOWS / "nonprod-foundation.yml").read_text())
    jobs = foundation["jobs"]

    assert "workflow_dispatch" in foundation[True]
    assert jobs["foundation-plan"]["if"] == "github.ref == 'refs/heads/main'"
    assert jobs["foundation-plan"]["environment"] == "nonprod-plan"
    assert jobs["foundation-apply"]["environment"] == "nonprod"
    assert jobs["foundation-apply"]["needs"] == "foundation-plan"

    text = (WORKFLOWS / "nonprod-foundation.yml").read_text()
    assert "TF_VAR_deployment_stage=0 terraform plan" in text
    assert 'existing_stage" != "0"' in text
    assert "Foundation state is already beyond stage 0; refusing foundation plan." in text
    assert "Foundation state advanced after planning; refusing stage-0 apply." in text
    assert "partial failed first apply can converge safely" in text


def test_staged_deployment_stops_cleanly_until_foundation_exists() -> None:
    deploy_path = WORKFLOWS / "nonprod-deploy.yml"
    deploy = yaml.safe_load(deploy_path.read_text())
    jobs = deploy["jobs"]

    assert jobs["foundation-ready"]["environment"] == {
        "name": "nonprod-plan",
        "deployment": False,
    }
    assert jobs["migration-plan"]["needs"] == ["build", "foundation-ready"]
    assert jobs["migration-plan"]["if"] == "needs.foundation-ready.outputs.ready == 'true'"

    text = deploy_path.read_text()
    assert "TF_VAR_deployment_stage=0 terraform plan -input=false -detailed-exitcode" in text
    assert "plan_status=$?" in text
    assert 'echo "ready=true" >> "$GITHUB_OUTPUT"' in text
    assert 'echo "ready=false" >> "$GITHUB_OUTPUT"' in text
    assert "NONPROD foundation is incomplete" in text
    assert ".deployment_stage.value != null" not in text
