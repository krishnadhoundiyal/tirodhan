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

## Increment 5 fleet facts

Migration 0026 uses relational fleet control/cycles/anchors/exceptions/restrictions,
actual FKs and SQL immutable-history guards. Typed policy/measurement JSON is not a
replacement for core entities. GPS and physical manifests are append-only; disputed
manifest holds survive ownership release. Downgrade refuses adopted history rather
than manufacturing legacy groups. See [Increment 5](../V2_ROUTING_INCREMENT_5.md).

## Commercial proof sharing — migration 0030

Keep the existing PostgreSQL database. Store stable commercial obligations, immutable
per-route proofs, temporal membership and advisory allocations relationally; retain a small
versioned reservation envelope and legacy decoder. Use identifier-only coalesced source
notices and a separate transactional source-head fence. Bounded process-local caches are
disposable accelerators, not authoritative storage. No Redis/new database is introduced.
Drain preparation before upgrade; populated new proof history blocks lossy downgrade. See
[implementation and measured limits](../COMMERCIAL_ADMISSION_SCALABILITY.md).
