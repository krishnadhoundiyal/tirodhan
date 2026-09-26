# ADR-011: Backup and Recovery

**Status:** Accepted for MVP

## Decision

Recovery objectives:

- RPO: approximately 15 minutes or better for authoritative transactional data;
- RTO: approximately 2–4 hours for a major recovery event.

PostgreSQL uses Azure Database for PostgreSQL Flexible Server native backup / point-in-time restore with an initial 7-day retention window.

MVP does not pay for geo-redundant PostgreSQL backup, long-term retention, or active-active regional database architecture.

Use resource locks where practical to reduce accidental deletion risk. Perform periodic restore tests.

For Blob media/evidence, enable blob soft delete and container soft delete, keep lifecycle management, and do not enable blob versioning initially unless a real overwrite-recovery need appears.

Logs receive no separate backup policy beyond normal Blob retention/lifecycle rules. Service Bus is not authoritative recovery storage; PostgreSQL state must allow required work to be recreated/re-driven.

## Rationale

The system handles paid customer commitments, so recoverability matters, but MVP economics do not justify active-active/high-availability regional design.

## Consequences

- recovery must be tested, not assumed;
- infrastructure required after restore must be reproducible from source-controlled IaC;
- regional disaster protection may be revisited when business value justifies it.
