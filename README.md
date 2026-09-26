# Tirodhan

This repository contains the implementation of the Tirodhan collection platform.

The architecture is intentionally documented before substantial implementation begins. Coding agents must read the architecture and agent instructions before modifying application or infrastructure code.

## Read first

1. `AGENTS.md`
2. `docs/PROJECT_CONTEXT.md`
3. `docs/ARCHITECTURE.md`
4. `docs/DATA_PROTECTION.md`
5. Relevant ADRs under `docs/adr/`

## Current architecture status

The backend/cloud architecture has been substantially defined for the MVP. The following are intentionally still open and must not be silently decided by an agent:

- physical geographic cell sizing/resolution;
- final clustering/compaction algorithm;
- detailed rider-selection algorithm inside a fleet;
- exact payment gateway vendor;
- exact OTP commercial rate/vendor confirmation;
- final CI/CD provider selection (Azure DevOps Pipelines or GitHub Actions);
- frontend/client technology;
- final retention periods for transactional data, media, and logs;
- final values for configurable timing/retry limits.

Azure is the reference MVP cloud, but application/domain code should avoid unnecessary Azure coupling.
