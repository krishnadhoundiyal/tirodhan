"""BDEP-02: PostgreSQL SQL setup is an automated, identity-isolated migration gate."""

from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import hcl2
import pytest
import yaml

from tirodhan.deployment import bootstrap_database, release

ROOT = Path(__file__).resolve().parents[2]
INFRA = ROOT / "infra/terraform/nonprod"


def tf(filename: str) -> dict:
    with (INFRA / filename).open(encoding="utf-8") as handle:
        return hcl2.load(handle)


def named(blocks: list[dict], resource: str, name: str) -> dict:
    return next(
        item[resource][name] for item in blocks if resource in item and name in item[resource]
    )


def test_pg_bootstrap_identity_is_separate_and_administrator_is_terraform_owned() -> None:
    identities = tf("identity.tf")["resource"]
    bootstrap = named(identities, "azurerm_user_assigned_identity", "postgres_bootstrap")
    assert bootstrap["name"] == "${local.prefix}-pgboot"
    admins = tf("database.tf")["resource"]
    admin = named(
        admins, "azurerm_postgresql_flexible_server_active_directory_administrator", "bootstrap"
    )
    assert admin["object_id"] == "${azurerm_user_assigned_identity.postgres_bootstrap.principal_id}"
    assert admin["principal_type"] == "ServicePrincipal"
    assert admin["principal_name"] == "${azurerm_user_assigned_identity.postgres_bootstrap.name}"
    admin_resources = [
        entry["azurerm_postgresql_flexible_server_active_directory_administrator"]
        for entry in admins
        if "azurerm_postgresql_flexible_server_active_directory_administrator" in entry
    ]
    assert any("administrator" in entry for entry in admin_resources)


def test_pg_bootstrap_job_is_finite_and_cannot_be_scheduled() -> None:
    job = named(
        tf("postgres_bootstrap_job.tf")["resource"],
        "azurerm_container_app_job",
        "postgres_bootstrap",
    )
    assert job["count"] == "${var.deployment_stage >= 1 ? 1 : 0}"
    assert job["name"] == "${local.prefix}-pgboot"
    assert "manual_trigger_config" in job and "schedule_trigger_config" not in job
    assert job["identity"][0]["identity_ids"] == [
        "${azurerm_user_assigned_identity.postgres_bootstrap.id}"
    ]
    assert job["workload_profile_name"] == "Consumption"
    assert (
        job["template"][0]["container"][0]["args"][-1] == "tirodhan.deployment.bootstrap_database"
    )
    assert len(job["template"][0]["container"]) == 2
    env = {e["name"]: e["value"] for e in job["template"][0]["container"][0]["env"]}
    assert env["POSTGRES_BOOTSTRAP_CLIENT_ID"] == (
        "${azurerm_user_assigned_identity.postgres_bootstrap.client_id}"
    )
    assert env["RUNTIME_PRINCIPAL_ID"] == ("${azurerm_user_assigned_identity.runtime.principal_id}")
    assert "TIRODHAN_DATABASE_URL" not in env


def test_pg_bootstrap_mi_has_only_two_secret_scoped_keyvault_roles() -> None:
    roles = tf("postgres_bootstrap_job.tf")["resource"]
    grant = named(roles, "azurerm_role_assignment", "postgres_bootstrap_secrets")
    assert "ghcr-pull-pat" in grant["for_each"]
    assert "fluent-bit-sas" in grant["for_each"]
    assert grant["scope"] == "${azurerm_key_vault.runtime.id}/secrets/${each.value}"
    assert grant["role_definition_name"] == "Key Vault Secrets User"
    assert grant["principal_id"] == (
        "${azurerm_user_assigned_identity.postgres_bootstrap.principal_id}"
    )
    assert "azurerm_user_assigned_identity.runtime.id" not in repr(roles)


def test_pg_bootstrap_is_prior_to_alembic_and_automatic_in_trusted_workflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = {
        "resource_group_name": "synthetic-np",
        "postgres_bootstrap_job_name": "synthetic-pgboot",
        "migration_job_name": "synthetic-migrate",
    }
    monkeypatch.setattr(release, "output_values", lambda: state)
    completed: list[str] = []
    monkeypatch.setattr(
        release, "run_finite_job", lambda group, name, purpose: completed.append(name)
    )
    release.run_migration()
    assert completed == ["synthetic-pgboot", "synthetic-migrate"]
    deploy = yaml.safe_load((ROOT / ".github/workflows/nonprod-deploy.yml").read_text())
    apply = deploy["jobs"]["migration-apply"]
    assert apply["environment"] == "nonprod"
    steps = repr(apply["steps"])
    assert "tirodhan.deployment.release migrate" in steps
    assert "python -m tirodhan.deployment.bootstrap_database" not in steps
    assert "postgres_bootstrap_job_name" in (INFRA / "outputs.tf").read_text()


@pytest.mark.asyncio
async def test_hosted_bootstrap_uses_only_dedicated_mi_and_grants_nonadmin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = inspect.getsource(bootstrap_database.bootstrap)
    assert "pgaadauth_create_principal_with_oid" in source
    assert "CREATE EXTENSION IF NOT EXISTS postgis" in source
    assert "GRANT USAGE, CREATE ON SCHEMA public" in source
    assert "GRANT SELECT ON public.spatial_ref_sys" in source
    for name, value in {
        "POSTGRES_BOOTSTRAP_CLIENT_ID": "test-bootstrap-client",
        "POSTGRES_HOST": "synthetic-pg.example",
        "POSTGRES_ADMIN_NAME": "synthetic-pgboot",
        "RUNTIME_DATABASE_ROLE": "synthetic-runtime",
        "RUNTIME_PRINCIPAL_ID": "00000000-0000-0000-0000-000000000010",
    }.items():
        monkeypatch.setenv(name, value)
    credential = MagicMock()
    credential.__enter__.return_value = credential
    credential.get_token.return_value.token = "test-token"
    mi_factory = MagicMock(return_value=credential)
    cli_factory = MagicMock()
    monkeypatch.setattr(bootstrap_database, "ManagedIdentityCredential", mi_factory)
    monkeypatch.setattr(bootstrap_database, "AzureCliCredential", cli_factory)

    admin_connection = MagicMock(
        fetchval=AsyncMock(return_value=None),
        execute=AsyncMock(),
        close=AsyncMock(),
    )
    application_connection = MagicMock(execute=AsyncMock(), close=AsyncMock())
    connections = iter([admin_connection, application_connection])
    connect = AsyncMock(side_effect=lambda **kwargs: next(connections))
    monkeypatch.setattr(bootstrap_database.asyncpg, "connect", connect)
    await bootstrap_database.bootstrap()
    mi_factory.assert_called_once_with(client_id="test-bootstrap-client")
    cli_factory.assert_not_called()
    assert connect.await_count == 2
    assert all(item.kwargs["user"] == "synthetic-pgboot" for item in connect.await_args_list)
    created = admin_connection.execute.await_args_list[0].args
    assert created[0].startswith("SELECT pgaadauth_create_principal_with_oid")
    assert created[3] == "service"
    assert created[4:] == (False, False)
    assert application_connection.execute.await_args_list[0].args == (
        "CREATE EXTENSION IF NOT EXISTS postgis",
    )


def test_all_production_workloads_exclude_pg_bootstrap_identity() -> None:
    for path in ("container_apps.tf", "jobs.tf"):
        text = (INFRA / path).read_text()
        assert "azurerm_user_assigned_identity.postgres_bootstrap" not in text
