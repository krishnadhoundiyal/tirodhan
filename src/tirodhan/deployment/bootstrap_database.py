"""Entra-admin bootstrap only. Pre-create PostGIS; UAMI owns its migration-created tables."""

from __future__ import annotations

import asyncio
import os
import ssl

import asyncpg
from azure.identity import AzureCliCredential


async def bootstrap() -> None:
    role = os.environ["RUNTIME_DATABASE_ROLE"]
    oid = os.environ["RUNTIME_PRINCIPAL_ID"]
    with AzureCliCredential() as credential:
        token = credential.get_token("https://ossrdbms-aad.database.windows.net/.default").token
    connection = await asyncpg.connect(
        host=os.environ["POSTGRES_HOST"],
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
            if not any(
                str(row.get("rolename")) == role
                and str(row.get("objectId", row.get("objectid"))) == oid
                and row.get("principalType", row.get("principaltype")) == "service"
                and row.get("isAdmin", row.get("isadmin")) == 0
                for row in principals
            ):
                raise RuntimeError("Existing DB role is not the expected Entra principal")
        # Role names are server-generated, but still quote them rather than interpolate raw input.
        quoted_role = '"' + role.replace('"', '""') + '"'
        await connection.execute(f"GRANT CONNECT ON DATABASE tirodhan TO {quoted_role}")
    finally:
        await connection.close()
    application = await asyncpg.connect(
        host=os.environ["POSTGRES_HOST"],
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
    print("PostGIS and UAMI schema privileges prepared; no admin role granted to runtime")


if __name__ == "__main__":
    asyncio.run(bootstrap())
