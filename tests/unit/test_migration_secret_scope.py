from pathlib import Path

from tirodhan.deployment import release

ROOT = Path(__file__).resolve().parents[2]
TF = ROOT / "infra/terraform/nonprod"


def test_migration_secret_gate_excludes_business_runtime_secrets() -> None:
    available = frozenset({"ghcr-pull-pat", "fluent-bit-sas"})

    release.require_secrets(available, release.MIGRATION_SECRET_NAMES)

    assert "google-maps-api-key" not in release.MIGRATION_SECRET_NAMES
    assert "kaleyra-api-key" not in release.MIGRATION_SECRET_NAMES


def test_migration_job_does_not_inject_runtime_business_secrets() -> None:
    jobs = (TF / "jobs.tf").read_text(encoding="utf-8")
    migration = jobs.split('resource "azurerm_container_app_job" "scheduled"', maxsplit=1)[0]

    assert "for_each = local.migration_secret_refs" in migration
    assert "for_each = local.runtime_secret_env" not in migration
    assert "for_each = var.runtime_secret_names" not in migration


def test_runtime_binds_only_enabled_key_vault_secrets() -> None:
    locals_text = (TF / "locals.tf").read_text(encoding="utf-8")
    apps = (TF / "container_apps.tf").read_text(encoding="utf-8")
    variables = (TF / "variables.tf").read_text(encoding="utf-8")

    assert "if contains(var.enabled_runtime_secret_names, name)" in locals_text
    assert apps.count("for_each = local.runtime_secret_env") == 2
    assert 'variable "enabled_runtime_secret_names"' in variables


def test_runtime_env_is_allowlisted_but_not_required_for_deployment() -> None:
    variables = (TF / "variables.tf").read_text(encoding="utf-8")

    assert 'variable "runtime_env"' in variables
    assert "for key in keys(var.runtime_env)" in variables
    assert "Configure all explicit runtime values" not in variables
    assert "var.deployment_stage < 2" not in variables
