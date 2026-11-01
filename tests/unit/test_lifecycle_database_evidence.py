"""Actual SQL facts must survive a signed round trip before D admits migration."""

import json
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_nonprod_lifecycle_contract import MemoryObservations, auth

from tirodhan.deployment import bootstrap_database, database_evidence, lifecycle_checks
from tirodhan.deployment.lifecycle_checks import Checks


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [None, "oid", "admin", "elevated", "postgis", "grant"])
async def test_signed_database_facts_require_verified_principal_and_every_sql_fact(
    monkeypatch, tmp_path, invalid
) -> None:
    authorization = auth("database-bootstrap", kind="database")
    target = {
        "host": "synthetic.postgres.database.azure.com",
        "database": "tirodhan",
        "role": "runtime",
        "principal_id": "runtime-oid",
        "server_id": "/synthetic/server",
    }
    key = Ed25519PrivateKey.generate()
    public = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    policy = {
        "maximum_evidence_age_seconds": 300,
        "database_evidence_keys": {
            "test": {"principal_id": authorization.principal_id, "public_key": public}
        },
    }
    (tmp_path / "deploy").mkdir()
    (tmp_path / "deploy/nonprod-release-policy.json").write_text(json.dumps(policy))
    monkeypatch.setattr(lifecycle_checks, "ROOT", tmp_path)
    monkeypatch.setattr(database_evidence, "signer", lambda _: ("test", key, 300))
    issued = []
    monkeypatch.setattr(database_evidence, "write_evidence", issued.append)
    monkeypatch.setattr(bootstrap_database, "database_token", lambda: "synthetic-token")
    monkeypatch.setenv("POSTGRES_ADMIN_NAME", "admin")
    admin, application = AsyncMock(), AsyncMock()
    admin.fetch.return_value = [
        {
            "rolename": "runtime",
            "objectId": "wrong" if invalid == "oid" else "runtime-oid",
            "principalType": "service",
            "isAdmin": 1 if invalid == "admin" else 0,
        }
    ]
    admin.fetchval.side_effect = [invalid == "elevated", False]
    application.fetchval.side_effect = [invalid != "postgis", invalid != "grant", True, True, True]
    connect = AsyncMock(side_effect=[admin, application])
    monkeypatch.setattr(bootstrap_database.asyncpg, "connect", connect)
    if invalid is not None:
        with pytest.raises(RuntimeError, match="postconditions"):
            await bootstrap_database.write_verified_evidence(target, authorization)
        assert issued == []
    else:
        await bootstrap_database.write_verified_evidence(target, authorization)
        monkeypatch.setenv("TIRODHAN_DATABASE_PREREQUISITE_EVIDENCE", issued[0])
        monkeypatch.setattr(Checks, "database_target", lambda _: target)
        io = MemoryObservations()
        assert Checks(auth("migration-plan"), io.config, {}, io).check_database()
        monkeypatch.setattr(Checks, "database_target", lambda _: target | {"principal_id": "new"})
        assert not Checks(auth("migration-plan"), io.config, {}, io).check_database()
    assert [c.kwargs["database"] for c in connect.call_args_list] == ["postgres", "tirodhan"]
    admin.close.assert_awaited_once()
    application.close.assert_awaited_once()
    admin.execute.assert_not_awaited()
    application.execute.assert_not_awaited()
