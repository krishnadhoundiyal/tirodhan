# ADR-015: Serviceability Location and Messaging Runtime

**Status:** Accepted for MVP (Phase 1T)

## Decision

Google Maps Platform stable Geocoding API v3 is the production LocationResolver.
Use server-side REST through reusable `httpx.AsyncClient` at
`https://maps.googleapis.com/maps/api/geocode/json`, with bounded timeout and no
automatic retries. No Google SDK, preview API, compute, database or messaging is used.
The dedicated Geocoding API key is delivered Key Vault -> ACA secret/reference ->
process configuration; application code does not call Key Vault.

Serviceability is Delhi-first: structured country must be IN and administrative
area level 1 must match configured Delhi/NCT aliases after trim/case-fold.
Supplied customer-confirmed pins are reverse-geocoded for area validation and
retained exactly. Address-only forward results must be unambiguous, non-partial,
household-compatible ROOFTOP or RANGE_INTERPOLATED results. Weak/no matches are
controlled UNSERVICEABLE outcomes; provider technical failures are TECHNICAL_FAILURE.
Descriptors are optional supplemental data, never stored or used as pickup points.

H3 is independent of Google. MVP resolution is exactly **7**, an architecture/code
constant, not an environment option. `cell_id` stores the canonical raw H3 string.
Changing resolution requires an explicit architecture/data transition. Planning
continues to operate on one upstream cell without cross-cell compaction.

The primary path is PENDING context + transactional ServiceabilityRequested outbox
-> outbox publisher runtime -> Azure Service Bus **Standard**
-> serviceability consumer runtime. The consumer uses Peek-Lock and durable inbox,
with identifier-only messages. This asynchronous path is primary. Checkout resolves an
owned, unexpired still-PENDING context synchronously using exactly the same domain
operation, only as fallback. Persisted status GET never invokes the provider.

Resolution reads immutable input in a short DB transaction, closes it before
decrypting/calling Google/deriving H3, then conditionally commits PENDING -> terminal
in another short transaction. PostgreSQL selects the authoritative result. Concurrent
worker/API calls may both call Google; the loser returns the persisted winner.
No Redis, distributed lock or global mutex is introduced.

Inbox PROCESSING commits before resolution and is resumable after crash; PROCESSED
commits before broker settlement. Publisher sends outside DB transactions and marks
PUBLISHED only afterward. Send-before-mark may resend the stable outbox UUID as
message ID. At-least-once transport plus inbox/domain idempotency produces exactly-once
intended business effect, not distributed exactly-once execution.

Service Bus uses workload identity and entity-scoped Data Sender/Data Receiver RBAC,
not connection strings. Azure resource access continues to use workload identity;
no duplicate platform estate or new default Log Analytics/Application Insights is introduced.

Exact hosting/scheduling/scaling topology for the publisher and consumer is deployment
architecture and is deferred. The current finite publisher and consumer process entry
points do not themselves freeze ACA Job/worker deployment choices.

## Protection and operational consequences

Google receives only necessary address/pin input. Raw requests, full URLs, responses,
descriptors, exact coordinates and key must never enter logs/reliability metadata.
Only existing context point/cell/status/failure/timestamp facts persist. Missing
provider configuration fails closed. Provider timeouts/rate failures have controlled
codes, never response-body errors. No schema migration is required.

Runtime schedule, timeouts, delivery limits and capacity are explicit operations
configuration, not new product constants. Deployment requires review of Google key
restrictions/billing, Azure permissions and environment inputs; no live provisioning
is implicit in implementation.

Functional Phase 1T is complete independently of deployment topology. The unvalidated
Phase 1T Terraform artifacts have been removed; this ADR does not accept resource
provisioning or hosting choices for these runtimes. Later deployment review remains
subject to the existing platform and IaC ADRs, without changing the frozen functional
outbox/inbox, asynchronous-primary, synchronous-fallback or transaction contracts.

## References

- [Google v3 geocoding](https://developers.google.com/maps/documentation/geocoding/guides-v3/requests-geocoding)
- [Google v3 reverse geocoding](https://developers.google.com/maps/documentation/geocoding/guides-v3/requests-reverse-geocoding)
- [H3 indexing](https://h3geo.org/docs/api/indexing/)
- ADR-001, ADR-002, ADR-003, ADR-010, ADR-012, ADR-014.
