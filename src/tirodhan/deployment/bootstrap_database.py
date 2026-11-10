"""Explicit Entra-admin bootstrap and fresh, signed verification of actual SQL facts."""

from __future__ import annotations

import asyncio
import os
import ssl
import time
from collections.abc import Iterable
from typing import Any

import asyncpg
import jwt
from azure.identity import AzureCliCredential

from tirodhan.deployment.deployment_admission import Authorization


def authorized_target() -> tuple[dict[str, str], Authorization]:
    from tirodhan.deployment.deployment_admission import configuration
    from tirodhan.deployment.lifecycle_checks import Checks
    from tirodhan.deployment.prerequisites import require_provisioning

    grant = require_provisioning("database-bootstrap")
    config = configuration()
    target = Checks(grant.authorization, config, {}).database_target()
    expected = {
        "POSTGRES_HOST": target["host"],
        "RUNTIME_DATABASE_ROLE": target["role"],
        "RUNTIME_PRINCIPAL_ID": target["principal_id"],
        "POSTGRES_ADMIN_NAME": config["terraform"]["postgres_admin_name"],
    }
    if any(os.environ.get(key) != value for key, value in expected.items()):
        raise RuntimeError("Database bootstrap target differs from authorized resources")
    return target, grant.authorization


def database_token() -> str:
    with AzureCliCredential() as credential:
        return str(credential.get_token("https://ossrdbms-aad.database.windows.net/.default").token)


def _entra_principal(principals: Iterable[asyncpg.Record], role: str) -> dict[str, Any]:
    # Azure can return the PostgreSQL catalog spelling or the documented spelling.
    for principal in principals:
        if principal.get("rolname", principal.get("rolename")) == role:
            return dict(principal)
    return {}


async def bootstrap(*, verify_only: bool = False) -> None:
    target, authorization = await asyncio.to_thread(authorized_target)
    if verify_only:
        await write_verified_evidence(target, authorization)
        return
    role, oid = target["role"], target["principal_id"]
    token = await asyncio.to_thread(database_token)
    connection = await asyncpg.connect(
        host=target["host"],
        database="postgres",
        user=os.environ["POSTGRES_ADMIN_NAME"],
        password=token,
        ssl=ssl.create_default_context(),
    )
    try:
        exists = await connection.fetchval("SELECT 1 FROM pg_roles WHERE rolname=$1", role)
        if not exists:
            await connection.execute(
                "SELECT pgaadauth_create_principal_with_oid($1, $2, 'service', false, false)",
                role,
                oid,
            )
        else:
            principals = await connection.fetch("SELECT * FROM pgaadauth_list_principals(false)")
            principal = _entra_principal(principals, role)
            if not (
                str(principal.get("objectId", principal.get("objectid"))).casefold()
                == oid.casefold()
                and principal.get("principalType", principal.get("principaltype")) == "service"
                and principal.get("isAdmin", principal.get("isadmin")) == 0
            ):
                raise RuntimeError("Existing DB role is not the expected Entra principal")
        quoted_role = '"' + role.replace('"', '""') + '"'
        await connection.execute(f"GRANT CONNECT ON DATABASE tirodhan TO {quoted_role}")
    finally:
        await connection.close()
    application = await asyncpg.connect(
        host=target["host"],
        database="tirodhan",
        user=os.environ["POSTGRES_ADMIN_NAME"],
        password=token,
        ssl=ssl.create_default_context(),
    )
    try:
        await application.execute("CREATE EXTENSION IF NOT EXISTS postgis")
        await application.execute(f"GRANT USAGE, CREATE ON SCHEMA public TO {quoted_role}")
        await application.execute(f"GRANT SELECT ON public.spatial_ref_sys TO {quoted_role}")
    finally:
        await application.close()
    await write_verified_evidence(target, authorization)
    print("PostGIS and UAMI schema privileges verified; no runtime administrator role")


async def enroll_verifier() -> None:
    from tirodhan.deployment.deployment_admission import configuration
    from tirodhan.deployment.lifecycle_checks import Checks
    from tirodhan.deployment.prerequisites import require_provisioning, revalidate_database

    grant = await asyncio.to_thread(require_provisioning, "enroll-database-verifier")
    config = configuration()
    checks = Checks(grant.authorization, config, {})
    target = await asyncio.to_thread(checks.database_target)
    verifier = checks.binding()["verifier_principal"]
    role = verifier["role"]
    token = await asyncio.to_thread(database_token)
    options = {
        "host": target["host"],
        "user": config["terraform"]["postgres_admin_name"],
        "password": token,
        "ssl": ssl.create_default_context(),
        "timeout": 30,
        "command_timeout": 30,
    }
    application = await asyncpg.connect(database=target["database"], **options)
    try:
        async with application.transaction(readonly=True):
            safe = await application.fetchval(
                """SELECT NOT EXISTS (
                    SELECT FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname !~ '^(pg_|information_schema$)'
                      AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')
                      AND NOT EXISTS (SELECT FROM pg_depend d
                                      WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid
                                        AND d.refclassid = 'pg_extension'::regclass
                                        AND d.deptype = 'e')
                ) AND NOT EXISTS (
                    SELECT FROM pg_namespace n
                    WHERE n.nspname !~ '^(pg_|information_schema$)' AND (
                        EXISTS (SELECT FROM aclexplode(
                                    coalesce(n.nspacl, acldefault('n', n.nspowner))) a
                                WHERE a.grantee = 0 AND a.privilege_type = 'CREATE')
                        OR EXISTS (SELECT FROM pg_roles r WHERE r.rolname = $1
                                   AND has_schema_privilege(r.oid, n.oid, 'CREATE'))
                    )
                )""",
                role,
            )
            if safe is not True:
                raise RuntimeError("Verifier enrollment requires a safe pre-migration database")
    finally:
        await application.close()

    connection = await asyncpg.connect(database="postgres", **options)
    try:
        async with connection.transaction():
            await connection.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", role)
            await asyncio.to_thread(revalidate_database, grant)
            if not await connection.fetchval("SELECT 1 FROM pg_roles WHERE rolname=$1", role):
                await connection.execute(
                    "SELECT pg_catalog.pgaadauth_create_principal_with_oid"
                    "($1, $2, 'service', false, false)",
                    role,
                    verifier["principal_id"],
                )
            principal = _entra_principal(
                await connection.fetch("SELECT * FROM pg_catalog.pgaadauth_list_principals(false)"),
                role,
            )
            if not (
                principal.get("objectId", principal.get("objectid")) == verifier["principal_id"]
                and principal.get("tenantId", principal.get("tenantid")) == verifier["tenant_id"]
                and principal.get("principalType", principal.get("principaltype")) == "service"
                and principal.get("isAdmin", principal.get("isadmin")) == 0
                and await connection.fetchval(
                    "SELECT rolcanlogin AND NOT (rolsuper OR rolcreaterole OR rolcreatedb "
                    "OR rolreplication OR rolbypassrls) AND NOT EXISTS "
                    "(SELECT FROM pg_auth_members WHERE member = pg_roles.oid) "
                    "FROM pg_roles WHERE rolname=$1",
                    role,
                )
                is True
                and await connection.fetchval(
                    "SELECT has_function_privilege($1, "
                    "'pg_catalog.pgaadauth_list_principals(boolean)', 'EXECUTE')",
                    role,
                )
                is True
            ):
                raise RuntimeError("Verifier principal or metadata privileges failed verification")
            quoted_role = '"' + role.replace('"', '""') + '"'
            for database in ("postgres", "tirodhan"):
                if (
                    await connection.fetchval(
                        "SELECT has_database_privilege($1, $2, 'CREATE')", role, database
                    )
                    is not False
                ):
                    raise RuntimeError("Verifier has unexpected database CREATE privilege")
                if not await connection.fetchval(
                    "SELECT has_database_privilege($1, $2, 'CONNECT')", role, database
                ):
                    await connection.execute(
                        f"GRANT CONNECT ON DATABASE {database} TO {quoted_role}"
                    )
                if (
                    await connection.fetchval(
                        "SELECT has_database_privilege($1, $2, 'CONNECT')", role, database
                    )
                    is not True
                ):
                    raise RuntimeError("Verifier database CONNECT failed verification")
    finally:
        await connection.close()
    print("DATABASE_VERIFIER_SQL_ENROLLMENT_VERIFIED")


async def write_verified_evidence(
    target: dict[str, str],
    authorization: Authorization,
    *,
    database_user: str | None = None,
    consumer_context: dict[str, str] | None = None,
) -> None:
    from tirodhan.deployment.database_evidence import signer, write_evidence

    key_id, private, age = await asyncio.to_thread(signer, authorization)
    observed_at = int(time.time())
    token = await asyncio.to_thread(database_token)
    connection = await asyncpg.connect(
        host=target["host"],
        database="postgres",
        user=database_user or os.environ["POSTGRES_ADMIN_NAME"],
        password=token,
        ssl=ssl.create_default_context(),
        timeout=30,
        command_timeout=30,
    )
    try:
        async with connection.transaction(readonly=True):
            principals = await connection.fetch("SELECT * FROM pgaadauth_list_principals(false)")
            verifier_ok = True
            if authorization.identity_kind == "database_verification":
                verifier = _entra_principal(principals, database_user or "")
                verifier_ok = (
                    await connection.fetchval("SELECT current_user") == database_user
                    and str(verifier.get("objectId", verifier.get("objectid"))).casefold()
                    == authorization.principal_id.casefold()
                    and verifier.get("principalType", verifier.get("principaltype")) == "service"
                    and verifier.get("isAdmin", verifier.get("isadmin")) == 0
                    and await connection.fetchval(
                        "SELECT NOT (rolsuper OR rolcreaterole OR rolcreatedb OR rolreplication "
                        "OR rolbypassrls) FROM pg_roles WHERE rolname=$1",
                        database_user,
                    )
                    is True
                    and await connection.fetchval(
                        "SELECT NOT (pg_has_role($1, 'azure_pg_admin', 'MEMBER') "
                        "OR pg_has_role($1, 'pg_read_all_data', 'MEMBER') "
                        "OR pg_has_role($1, 'pg_write_all_data', 'MEMBER') "
                        "OR pg_has_role($1, $2, 'MEMBER'))",
                        database_user,
                        target["role"],
                    )
                    is True
                )
            principal = _entra_principal(principals, target["role"])
            role_flags = await connection.fetchval(
                "SELECT rolsuper OR rolcreaterole OR rolcreatedb OR rolreplication "
                "OR rolbypassrls "
                "FROM pg_roles WHERE rolname=$1",
                target["role"],
            )
            admin_member = await connection.fetchval(
                "SELECT pg_has_role($1, 'azure_pg_admin', 'MEMBER')",
                target["role"],
            )
            elevated = role_flags is not False or admin_member is not False
    finally:
        await connection.close()
    connection = await asyncpg.connect(
        host=target["host"],
        database=target["database"],
        user=database_user or os.environ["POSTGRES_ADMIN_NAME"],
        password=token,
        ssl=ssl.create_default_context(),
        timeout=30,
        command_timeout=30,
    )
    try:
        async with connection.transaction(readonly=True):
            checks = {
                "principal_oid": str(
                    principal.get("objectId", principal.get("objectid"))
                ).casefold()
                == target["principal_id"].casefold()
                and principal.get("principalType", principal.get("principaltype")) == "service",
                "non_admin": principal.get("isAdmin", principal.get("isadmin")) == 0
                and not elevated,
                "postgis": bool(
                    await connection.fetchval(
                        "SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname='postgis')"
                    )
                ),
                "connect": bool(
                    await connection.fetchval(
                        "SELECT has_database_privilege($1, 'tirodhan', 'CONNECT')", target["role"]
                    )
                ),
                "schema_usage": bool(
                    await connection.fetchval(
                        "SELECT has_schema_privilege($1, 'public', 'USAGE')", target["role"]
                    )
                ),
                "schema_create": bool(
                    await connection.fetchval(
                        "SELECT has_schema_privilege($1, 'public', 'CREATE')", target["role"]
                    )
                ),
                "spatial_reference_select": bool(
                    await connection.fetchval(
                        "SELECT has_table_privilege($1, 'public.spatial_ref_sys', 'SELECT')",
                        target["role"],
                    )
                ),
            }
    finally:
        await connection.close()
    if authorization.identity_kind == "database_verification":
        checks["verifier_non_admin"] = verifier_ok
    if not all(checks.values()):
        raise RuntimeError("Database bootstrap postconditions are incomplete")
    now = int(time.time())
    if now - observed_at >= age:
        raise RuntimeError("Database observations expired before completion")
    evidence = jwt.encode(
        {
            "iss": "tirodhan-database-bootstrap",
            "aud": "tirodhan-nonprod-prerequisites",
            "sub": authorization.principal_id,
            "iat": now,
            "exp": observed_at + age,
            "observed_at": observed_at,
            "configuration_hash": authorization.configuration_hash,
            "contract_hash": authorization.contract_hash,
            "target": target,
            "checks": checks,
            "identity_kind": authorization.identity_kind,
            "consumer_context": consumer_context,
        },
        private,
        algorithm="EdDSA",
        headers={"kid": key_id},
    )
    await asyncio.to_thread(write_evidence, evidence)


if __name__ == "__main__":
    import argparse

    from tirodhan.deployment.deployment_admission import (
        AdmissionDenied,
        require_release_authorization,
    )

    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--verify-only", action="store_true")
    mode.add_argument("--enroll-verifier", action="store_true")
    args = parser.parse_args()
    try:
        if args.enroll_verifier:
            require_release_authorization("enroll-database-verifier")
            asyncio.run(enroll_verifier())
        else:
            require_release_authorization("database-bootstrap")
            asyncio.run(bootstrap(verify_only=args.verify_only))
    except AdmissionDenied as exc:
        raise SystemExit(str(exc)) from None
    except Exception:
        raise SystemExit("DATABASE_BOOTSTRAP_UNVERIFIED: no readiness evidence issued") from None
