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
    assert "Foundation state already exists; refusing" in text
    assert "Foundation state appeared after planning; refusing" in text


def test_staged_deployment_stops_cleanly_until_foundation_exists() -> None:
    deploy = yaml.safe_load((WORKFLOWS / "nonprod-deploy.yml").read_text())
    jobs = deploy["jobs"]

    assert jobs["foundation-ready"]["environment"] == "nonprod-plan"
    assert jobs["migration-plan"]["needs"] == ["build", "foundation-ready"]
    assert jobs["migration-plan"]["if"] == "needs.foundation-ready.outputs.ready == 'true'"
