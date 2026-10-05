from pathlib import Path

from tirodhan.deployment import release

ROOT = Path(__file__).resolve().parents[2]
TF = ROOT / "infra/terraform/nonprod"


def test_migration_secret_gate_excludes_business_runtime_secrets(monkeypatch) -> None:
    metadata = [
        {"name": "ghcr-pull-pat", "attributes": {"enabled": True}},
        {"name": "fluent-bit-sas", "attributes": {"enabled": True}},
    ]
    monkeypatch.setattr(release, "cli_json", lambda _command: metadata)

    release.check_secret_metadata(
        {"key_vault_name": "ignored"}, release.MIGRATION_SECRET_NAMES
    )

    assert "google-maps-api-key" not in release.MIGRATION_SECRET_NAMES
    assert "kaleyra-api-key" not in release.MIGRATION_SECRET_NAMES
    assert release.SECRET_NAMES < release.RUNTIME_SECRET_NAMES


def test_migration_job_does_not_inject_runtime_business_secrets() -> None:
    jobs = (TF / "jobs.tf").read_text(encoding="utf-8")
    migration = jobs.split('resource "azurerm_container_app_job" "scheduled"', maxsplit=1)[0]

    assert "for_each = local.migration_secret_refs" in migration
    assert "for_each = var.runtime_secret_names" not in migration


def test_full_runtime_env_is_required_only_when_api_is_enabled() -> None:
    variables = (TF / "variables.tf").read_text(encoding="utf-8")

    assert "condition = var.deployment_stage < 2 || alltrue([" in variables
