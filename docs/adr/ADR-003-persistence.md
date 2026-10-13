# ADR-003: Persistence and Object Storage

**Status:** Accepted for MVP

## Decision

Use PostgreSQL + PostGIS as the transactional/spatial database.

Use Azure Blob Storage for photos, videos, and archived application logs.

Use Blob lifecycle policies for tier transitions and retention rather than application cron code.

## Rationale

PostgreSQL provides transactional integrity and PostGIS provides native spatial indexing/query capability required by the planning domain.

Object storage is the correct cost/operational model for media.

## Consequences

Increment 4 adds relational route authority, immutable revisions/schedules, execution and
discharge/recovery proof, with SQL identity/history guards in the same database. Optional
group links permit new V2 work without fabricated V1 groups. Activated history blocks
lossy downgrade. No database/cache or production deployment is added. See
[implemented schema and rollback boundary](../V2_ROUTING_INCREMENT_4.md).

Increment 3 adds relational operating inputs and PostGIS positions in the existing database.
JSONB is limited to versioned shadow configuration/contract/result documents, with no
assignment authority or core relational substitute. It introduces no database/cache or
production component. See [schema and downgrade implications](../V2_ROUTING_INCREMENT_3.md).

- media bytes do not belong in PostgreSQL;
- exact lat/long remains queryable for PostGIS;
- database and object-store access are protected independently;
- physical storage SKUs remain subject to cost sizing.
