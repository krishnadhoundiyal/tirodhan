# ADR-009: MVP Observability and Logging

**Status:** Accepted for MVP

## Decision

Do not use Application Insights, Log Analytics, or Azure Diagnostic Settings as the primary application-log path at launch.

Applications write structured JSON logs to a shared replica-local volume.

A Fluent Bit sidecar:

- tails the logs;
- buffers/batches/compresses;
- uploads directly to Azure Blob Storage;
- authenticates with a narrowly scoped SAS to the dedicated log container.

Blob lifecycle policies control tiering/retention.

Use low-cost/native Azure platform metrics and a small number of actionable alerts.

## Invariants

- logs contain internal identifiers/correlation IDs rather than PII;
- logs never contain tokens, OTPs, addresses, phone numbers, exact coordinates, provider secrets, or payment instrument data;
- durable business/audit history belongs in PostgreSQL, not solely in logs.
