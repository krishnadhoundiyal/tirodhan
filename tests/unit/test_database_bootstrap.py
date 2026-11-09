from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from tirodhan.deployment import bootstrap_database as module
from tirodhan.deployment.deployment_admission import AdmissionDenied


@pytest.fixture(autouse=True)
def authorized_sql_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise SQL invariants after admission; admission denial has separate effect tests."""
    from tirodhan.deployment import deployment_admission, lifecycle_checks, prerequisites

    monkeypatch.setattr(
        prerequisites, "require_provisioning", lambda _: SimpleNamespace(authorization=None)
    )
    monkeypatch.setattr(
        deployment_admission,
        "configuration",
        lambda: {"terraform": {"postgres_admin_name": "admin"}},
    )
    monkeypatch.setattr(
        lifecycle_checks,
        "Checks",
        lambda *_: SimpleNamespace(
            database_target=lambda: {
                "host": "synthetic",
                "role": "synthetic-runtime",
                "principal_id": "oid",
            }
        ),
    )
    monkeypatch.setattr(module, "write_verified_evidence", AsyncMock())


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("role_column", ["rolename", "rolname"])
async def test_database_bootstrap_is_rerunnable_and_grants_only_schema_privileges(
    monkeypatch: pytest.MonkeyPatch,
    existing: bool,
    role_column: str,
) -> None:
    for key, value in {
        "RUNTIME_DATABASE_ROLE": "synthetic-runtime",
        "RUNTIME_PRINCIPAL_ID": "oid",
        "POSTGRES_HOST": "synthetic",
        "POSTGRES_ADMIN_NAME": "admin",
    }.items():
        monkeypatch.setenv(key, value)
    admin, application = AsyncMock(), AsyncMock()
    admin.fetchval.return_value = 1 if existing else None
    admin.fetch.return_value = [
        {
            role_column: "synthetic-runtime",
            "objectId": "oid",
            "principalType": "service",
            "isAdmin": 0,
        }
    ]
    connect = AsyncMock(side_effect=[admin, application])
    monkeypatch.setattr(module.asyncpg, "connect", connect)
    monkeypatch.setattr(module, "AzureCliCredential", MagicMock())
    await module.bootstrap()
    sql = [
        call.args[0] for client in (admin, application) for call in client.execute.call_args_list
    ]
    assert any("CREATE EXTENSION IF NOT EXISTS postgis" in statement for statement in sql)
    assert not any(
        "SUPERUSER" in statement or "azure_pg_admin" in statement or "GRANT ALL" in statement
        for statement in sql
    )
    assert sum("pgaadauth_create_principal_with_oid" in statement for statement in sql) == (
        0 if existing else 1
    )
    assert all(call.kwargs["ssl"].check_hostname for call in connect.call_args_list)
    admin.close.assert_awaited_once()
    application.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value", [("rolname", "different-role"), ("objectid", "different-oid"), ("isadmin", 1)]
)
async def test_bootstrap_rejects_wrong_or_privileged_existing_principal(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str | int,
) -> None:
    for key, item in {
        "RUNTIME_DATABASE_ROLE": "synthetic-runtime",
        "RUNTIME_PRINCIPAL_ID": "oid",
        "POSTGRES_HOST": "synthetic",
        "POSTGRES_ADMIN_NAME": "admin",
    }.items():
        monkeypatch.setenv(key, item)
    connection = AsyncMock()
    connection.fetchval.return_value = 1
    principal = {
        "rolname": "synthetic-runtime",
        "objectid": "oid",
        "principaltype": "service",
        "isadmin": 0,
    }
    principal[field] = value
    connection.fetch.return_value = [principal]
    monkeypatch.setattr(module.asyncpg, "connect", AsyncMock(return_value=connection))
    monkeypatch.setattr(module, "AzureCliCredential", MagicMock())
    with pytest.raises(RuntimeError, match="not the expected Entra principal"):
        await module.bootstrap()
    connection.execute.assert_not_awaited()
    connection.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario", ["create", "replay", "wrong-principal", "missing-metadata", "unsafe", "denied"]
)
async def test_verifier_enrollment_uses_exact_signed_role_and_no_extra_grants(
    monkeypatch, scenario
):
    from tirodhan.deployment import lifecycle_checks, prerequisites

    role = "tirodhan-np-test01-database-verifier"
    binding = {
        "verifier_principal": {"role": role, "principal_id": "verifier", "tenant_id": "tenant"}
    }
    grant = SimpleNamespace(authorization=object())

    def require(operation):
        assert operation == "enroll-database-verifier"
        if scenario == "denied":
            raise AdmissionDenied("RELEASE_AUTHORIZATION_INVALID")
        return grant

    def checks(authorization, *_):
        assert authorization is grant.authorization
        return SimpleNamespace(
            database_target=lambda: {"host": "synthetic", "database": "tirodhan"},
            binding=lambda: binding,
        )

    monkeypatch.setattr(prerequisites, "require_provisioning", require)
    monkeypatch.setattr(prerequisites, "revalidate_database", lambda current: current is grant)
    monkeypatch.setattr(lifecycle_checks, "Checks", checks)
    monkeypatch.setattr(module, "database_token", lambda: "synthetic-token")
    application, admin = AsyncMock(), AsyncMock()
    for connection in (application, admin):
        connection.transaction = MagicMock(return_value=AsyncMock())
    application.fetchval.return_value = scenario != "unsafe"
    creates_role = scenario in {"create", "missing-metadata"}
    admin.fetchval.side_effect = [
        not creates_role,
        True,
        scenario != "missing-metadata",
        False,
        True,
        True,
        False,
        True,
        True,
    ]
    admin.fetch.return_value = [
        {
            "rolname": role,
            "objectid": "other" if scenario == "wrong-principal" else "verifier",
            "tenantid": "tenant",
            "principaltype": "service",
            "isadmin": 0,
        }
    ]
    connect = AsyncMock(side_effect=[application, admin])
    monkeypatch.setattr(module.asyncpg, "connect", connect)
    if scenario == "denied":
        with pytest.raises(AdmissionDenied):
            await module.enroll_verifier()
        connect.assert_not_awaited()
        return
    if scenario in {"unsafe", "wrong-principal", "missing-metadata"}:
        with pytest.raises(RuntimeError):
            await module.enroll_verifier()
    else:
        await module.enroll_verifier()
    application.transaction.assert_called_once_with(readonly=True)
    if scenario == "unsafe":
        assert connect.await_count == 1
        admin.execute.assert_not_awaited()
        return
    admin.transaction.assert_called_once_with()
    exit_error = admin.transaction.return_value.__aexit__.await_args.args[0]
    assert exit_error is (
        RuntimeError if scenario in {"wrong-principal", "missing-metadata"} else None
    )
    assert "pg_advisory_xact_lock" in admin.execute.call_args_list[0].args[0]
    statements = admin.execute.call_args_list[1:]
    if creates_role:
        assert len(statements) == 1
        assert "pgaadauth_create_principal_with_oid" in statements[0].args[0]
        assert statements[0].args[1:] == (role, "verifier")
    else:
        assert not statements
