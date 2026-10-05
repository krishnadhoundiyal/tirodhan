from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/seed-ghcr-pull-pat.yml"
SEEDER = ROOT / "src/tirodhan/deployment/seed_ghcr_pull_pat.py"


def test_ghcr_pat_seeding_is_manual_main_only_and_approval_gated() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "workflow_dispatch:" in workflow
    assert "github.ref == 'refs/heads/main'" in workflow
    assert "environment: nonprod" in workflow
    assert "id-token: write" in workflow
    assert "secrets.NONPROD_GHCR_PULL_PAT" in workflow
    assert "vars.NONPROD_KEY_VAULT_URL" in workflow


def test_ghcr_pat_value_is_not_embedded_or_echoed() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    seeder = SEEDER.read_text(encoding="utf-8")

    assert "ghcr-pull-pat" in seeder
    assert "vault_put(vault_url, SECRET_NAME, value)" in seeder
    assert "print(value)" not in seeder
    assert "echo $GHCR_PULL_PAT" not in workflow
    assert "echo ${GHCR_PULL_PAT}" not in workflow
