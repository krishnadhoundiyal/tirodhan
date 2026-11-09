"""Actual SQL facts must survive a signed round trip before D admits migration."""

import json
from unittest.mock import AsyncMock, Mock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from lifecycle_fakes import SCOPE, TENANT, MemoryObservations, auth

from tirodhan.deployment import bootstrap_database, database_evidence, lifecycle_checks
from tirodhan.deployment.deployment_admission import AdmissionDenied
from tirodhan.deployment.lifecycle_checks import Checks


@pytest.mark.parametrize(
    "invalid", [None, "missing", "id", "objectId", "tenantId", "principalType"]
)
def test_database_bootstrap_checks_exact_live_entra_administrator(monkeypatch, invalid):
    authorization = auth("database-bootstrap", kind="database", database_bootstrap_authorized=True)
    server_id = f"{SCOPE}/providers/Microsoft.DBforPostgreSQL/flexibleServers/pg"
    administrator_id = f"{server_id}/administrators/{authorization.principal_id}"
    properties = {
        "objectId": authorization.principal_id,
        "tenantId": TENANT,
        "principalType": "User",
    }
    administrator = {"id": administrator_id, "properties": properties}
    if invalid == "id":
        administrator["id"] += "-other"
    elif invalid is not None and invalid != "missing":
        properties[invalid] = "other"
    io = Mock()
    io.arm.return_value = None if invalid == "missing" else administrator
    checks = Checks(
        authorization,
        {
            "terraform": {
                "postgres_admin_object_id": authorization.principal_id,
                "postgres_admin_type": "User",
                "tenant_id": TENANT,
            }
        },
        {},
        io,
    )
    monkeypatch.setattr(checks, "database_target", lambda: {"server_id": server_id})
    signer = Mock()
    monkeypatch.setattr(database_evidence, "signer", signer)

    assert checks.check_database_authority() is (invalid is None)
    signer.assert_called_once_with(authorization)
    io.arm.assert_called_once_with(administrator_id, "2024-08-01")


@pytest.mark.parametrize(
    "representation", ["canonical", "crlf", "no-newline", "wrong-key", "wrong-principal"]
)
def test_attester_enrollment_compares_actual_key_material(monkeypatch, tmp_path, representation):
    authorization = auth("database-bootstrap", kind="database")
    private = Ed25519PrivateKey.generate()
    public_key = (
        Ed25519PrivateKey.generate().public_key()
        if representation == "wrong-key"
        else private.public_key()
    )
    public = public_key.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    if representation == "crlf":
        public = public.replace("\n", "\r\n")
    if representation == "no-newline":
        public = public.rstrip()
    policy = {
        "maximum_evidence_age_seconds": 300,
        "database_evidence_keys": {
            "test": {
                "principal_id": "other"
                if representation == "wrong-principal"
                else authorization.principal_id,
                "public_key": public,
            }
        },
    }
    (tmp_path / "deploy").mkdir()
    (tmp_path / "deploy/nonprod-release-policy.json").write_text(json.dumps(policy))
    path = tmp_path / "private.pem"
    path.write_bytes(
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    monkeypatch.setattr(database_evidence, "ROOT", tmp_path)
    monkeypatch.setenv("TIRODHAN_DATABASE_EVIDENCE_KEY_ID", "test")
    monkeypatch.setenv("TIRODHAN_DATABASE_EVIDENCE_PRIVATE_KEY_FILE", str(path))
    if representation.startswith("wrong-"):
        with pytest.raises(AdmissionDenied, match="DATABASE_EVIDENCE_SIGNER_NOT_AUTHORIZED"):
            database_evidence.signer(authorization)
    else:
        identifier, key, age = database_evidence.signer(authorization)
        assert identifier == "test" and age == 300
        assert key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        ) == private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid",
    [
        None,
        "oid",
        "admin",
        "elevated",
        "postgis",
        "grant",
        "schema-usage",
        "schema-create",
        "spatial-select",
    ],
)
async def test_signed_database_facts_require_verified_principal_and_every_sql_fact(
    monkeypatch, tmp_path, invalid
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
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
    for connection in (admin, application):
        connection.transaction = Mock(return_value=AsyncMock())
    admin.fetch.return_value = [
        {
            "rolename": "runtime",
            "objectId": "wrong" if invalid == "oid" else "runtime-oid",
            "principalType": "service",
            "isAdmin": 1 if invalid == "admin" else 0,
        }
    ]
    admin.fetchval.side_effect = [invalid == "elevated", False]
    application.fetchval.side_effect = [
        invalid != "postgis",
        invalid != "grant",
        invalid != "schema-usage",
        invalid != "schema-create",
        invalid != "spatial-select",
    ]
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
