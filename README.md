# Tirodhan

This repository contains the implementation of the Tirodhan collection platform.

The architecture and domain model are intentionally documented before substantial implementation begins. Coding agents must read the approved design documents before modifying application, database, worker, or infrastructure code.

## Read first

1. `AGENTS.md`
2. `docs/PROJECT_CONTEXT.md`
3. `docs/ARCHITECTURE.md`
4. `docs/DATA_PROTECTION.md`
5. `docs/DOMAIN_MODEL.md`
6. `docs/IDEMPOTENCY.md`
7. `docs/SCHEMA_DESIGN.md`
8. `docs/ER_DIAGRAM.md`
9. Relevant ADRs under `docs/adr/`

## Current architecture status

The backend/cloud architecture, core domain boundaries, idempotency model, and initial PostgreSQL/PostGIS schema design have been substantially defined for the MVP.

The following remain intentionally open and must not be silently decided by an agent:

- physical geographic cell sizing/resolution;
- final clustering/compaction algorithm;
- detailed route-optimization algorithm, if any;
- detailed rider-selection algorithm inside a fleet;
- item category taxonomy and final pricing formula;
- exact payment gateway vendor;
- exact OTP commercial rate/vendor confirmation;
- final CI/CD provider selection: Azure DevOps Pipelines or GitHub Actions;
- frontend/mobile technology;
- final offline-evidence validation policy;
- final retention periods for transactional data, media, logs, inbox/outbox/idempotency records;
- final configurable timing/retry values;
- detailed workflow when actual collected material differs from the booking.

Azure is the reference MVP cloud, but application/domain code should avoid unnecessary Azure coupling.
