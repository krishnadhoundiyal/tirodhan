from pathlib import Path


def test_foundation_readiness_accepts_existing_higher_stage() -> None:
    workflow = Path(".github/workflows/nonprod-deploy.yml").read_text(encoding="utf-8")

    assert "terraform output -raw deployment_stage" in workflow
    assert "2>/dev/null || true" in workflow
    assert '[[ "$current_stage" =~ ^[0-9]+$ ]] && (( current_stage >= 1 ))' in workflow
    assert 'echo "ready=true" >> "$GITHUB_OUTPUT"' in workflow
    assert "TF_VAR_deployment_stage=0 terraform plan -input=false -detailed-exitcode" in workflow


def test_migration_still_requires_foundation_ready_output() -> None:
    workflow = Path(".github/workflows/nonprod-deploy.yml").read_text(encoding="utf-8")

    assert "if: needs.foundation-ready.outputs.ready == 'true'" in workflow
