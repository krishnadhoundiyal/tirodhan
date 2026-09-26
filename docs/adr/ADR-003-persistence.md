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

- media bytes do not belong in PostgreSQL;
- exact lat/long remains queryable for PostGIS;
- database and object-store access are protected independently;
- physical storage SKUs remain subject to cost sizing.
