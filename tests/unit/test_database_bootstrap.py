from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tirodhan.deployment import bootstrap_database as module


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [False, True])
async def test_database_bootstrap_is_rerunnable_and_grants_only_schema_privileges(
    monkeypatch: pytest.MonkeyPatch,
    existing: bool,
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
            "rolename": "synthetic-runtime",
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
@pytest.mark.parametrize("field,value", [("objectId", "different-oid"), ("isAdmin", 1)])
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
        "rolename": "synthetic-runtime",
        "objectId": "oid",
        "principalType": "service",
        "isAdmin": 0,
    }
    principal[field] = value
    connection.fetch.return_value = [principal]
    monkeypatch.setattr(module.asyncpg, "connect", AsyncMock(return_value=connection))
    monkeypatch.setattr(module, "AzureCliCredential", MagicMock())
    with pytest.raises(RuntimeError, match="not the expected Entra principal"):
        await module.bootstrap()
    connection.execute.assert_not_awaited()
    connection.close.assert_awaited_once()
