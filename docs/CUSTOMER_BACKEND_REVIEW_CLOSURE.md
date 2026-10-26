# Customer backend review closure — CB-01, CB-02, CB-03

This follow-up extends `daa33acc0bf8d5792d3bec2465d693cea4c26499` on
`codex/customer-backend-capabilities`. It changes backend code only. Provider activation,
production migration, deployment, frontend changes and cloud provisioning remain outside this work.

## CB-01: bounded DNS and HTTPS resources

Web Push no longer uses `asyncio.to_thread` or the parent's default executor. One
`WebPushTransport` belongs to each finite `ConfiguredDelivery` port. It admits one live
child, rejects concurrent sends with retryable failure, and has no waiting queue. The worker
constructs one port and sends sequentially. Each call launches the existing Python runtime
without a shell; endpoint, encrypted body and VAPID headers travel only through a private
stdin pipe, never command arguments or logs. Child stderr is discarded; stdout contains only
a three-digit HTTP status. The bounded request envelope is at most 16 KiB.

The parent applies a 20-second budget across launch and exchange, then kills and reaps the
child and drains its pipes. Cancellation during launch is shielded until the child handle is
acquired, so it can also be reaped. Closing the port cancels an active send and waits for
cleanup. POSIX SIGTERM cancels the finite worker through its existing `finally` cleanup.
Ordinary `asyncio.run` shutdown also cancels/reaps pending sends. A daemon watchdog inside
the child calls `os._exit(1)` after 20 seconds, including while libc `getaddrinfo` is blocked
and has released the GIL. Thus a killed parent cannot leave stalled DNS children indefinitely.
As with any process supervision, process creation/kill/reaping depends on a functioning OS;
the deadline does not claim real-time scheduling under a frozen kernel.

The existing whole-answer public-address check, pinned destination IP, original hostname
TLS verification/SNI/Host, no redirects/proxy use and response-body exclusion are retained.
Timeout or ambiguous termination retains the same leased delivery's retry debt. A provider
may have accepted a request before termination, so duplicate signals remain possible;
neither delivery status nor a subprocess result is business acknowledgement.

Tests enter the production connection path with a resolver stalled for an hour. They verify
repeated deadline cleanup, one-child admission under 50 competing calls, a saturated one-thread
default executor, cancellation during launch, pipe success, event-loop shutdown, child self-exit
without supervision, and POSIX worker SIGTERM. These use synthetic credentials and no live HTTP.

## CB-02: approved device admission and lifecycle

The user approved these values and semantics on 26 October 2026:

| Policy | Approved value |
|---|---:|
| Active native registrations per customer | 5 |
| Active browser registrations per customer | 10 |
| Successfully committed new registrations/rotations, combined across both | 30 |
| Rolling window | 24 hours (86,400 seconds) |

Settings expose these approved defaults as `customer_native_registration_limit`,
`customer_browser_registration_limit`, `customer_registration_change_limit` and
`customer_registration_window_seconds`. All must be positive. Changes to these business
values require policy approval and consistent deployment configuration across API replicas.

The existing PostgreSQL customer `NO KEY UPDATE` lock serializes admission across all
sessions/devices/IPs of that identity. Database time is obtained after that lock. Immutable
binding insertions newer than `database_now - window` count successful changes;
rollback contributes nothing, and rotation adds exactly one new history row. A customer/time
index supports this count. Exact active replay returns before admission checks and inserts
nothing. Explicit revocation remains exempt, even when quota is exhausted. Login and logout
do not reset the customer's rolling quota. No rejected operation revokes, evicts or inserts.

The prospective live-device count excludes only matching credentials/installation being
replaced by the existing generation/session rules, and sessions that are already expired or
revoked. The limit cannot evict an unrelated device. On successful admission only, expired
bindings are revoked in bulk; immutable tombstones and newer-session replay fencing remain.
Session logout still revokes bindings atomically. A partial active index supports counting and
cleanup. Fan-out also excludes expired/revoked sessions, bounding eligible registrations to
the approved combined allowance of 15, including when no later registration performs cleanup.
Historical notifications and completed delivery records are preserved.

Exhaustion returns HTTP 429 with only `{"error":{"code":"DEVICE_ACTIVE_LIMIT"}}` or
`{"error":{"code":"DEVICE_CHANGE_LIMIT"}}`. Responses do not disclose counts, device IDs,
credentials or other customers. Clients should revoke a known registration explicitly or
wait for rolling quota to expire; retries with new identities cannot evade customer scope.

Tests cover excess simultaneous distinct native/browser registrations; shared customer quota
across two sessions and both platforms; competing final-quota rotations; replay/revocation at
exhaustion; sanitized HTTP errors; rejection preserving all existing bindings; pre-commit
rollback; expired-session lifecycle; history outside the rolling window; and 15-recipient fan-out.

## CB-03: PostgreSQL measurement and incremental consumption

The old algorithm's increasing history cost was measured locally, without assuming production
overload. `scripts/benchmark_customer_notification_scan.py` creates a uniquely named isolated
schema on the disposable PostgreSQL 17.5 instance, copies deployed table/index definitions,
seeds synthetic processed histories plus ten sparse new events, runs each query three times
with `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`, and drops that exact schema transactionally.
Regular tables use shared buffers. No application rows or providers are touched. The fixture
contains one selected event type and a dedicated consumer inbox; real production distributions,
cache pressure, concurrent workload and storage will affect absolute times.

Full plans and server version are in
[the measurement artifact](benchmarks/customer_notification_scan_comparison.json).

| Processed events | Old median ms | Incremental median ms | Old shared hits* | Incremental shared hits* |
|---:|---:|---:|---:|---:|
| 1,000 | 3.771 | 0.485 | 3,057 | 32 |
| 10,000 | 51.958 | 0.107 | 40,304 | 41 |
| 100,000 | 513.305 | 0.099 | 402,662 | 51 |
| 500,000 | 4,842.810 | 0.106 | 2,490,959 | 51 |

*Buffer counts use the second execution; every query returned ten rows. At 500,000 events
the old second execution also read 22,194 shared blocks, while the incremental query read zero.
The plans show repeated old-history traversal, not just a timing fluctuation. This is a
synthetic scalability result, not a claim that deployed traffic has caused a runtime bottleneck.

Migration `0040_customer_notification_work` adds a concrete `customer_notification_pending`
relation with a real source-outbox FK, source UUID primary key and `(available_at, source UUID)`
index. An outbox INSERT/availability-update trigger maintains work membership in the source
transaction. Installation of that trigger and one-time historical adoption commit together.
Adoption skips already-established notification history; deferred events retain availability.
This uses pending-set membership rather than a timestamp/UUID/high-water mark, so a late
commit, backdated insert or future event becoming due cannot fall behind a scan cursor.

Materialization locks bounded due membership using `FOR UPDATE OF pending SKIP LOCKED`,
resolves the real source and retains independent inbox and unique source-notification replay
checks. History, recipient work, inbox completion and deletion of pending membership commit
together. Failure before commit leaves membership intact; response loss cannot duplicate
history. Concurrent workers claim disjoint memberships. Explicit replay may reinsert membership
for a retained source; missing inbox metadata still cannot create duplicate notifications or
expand historical recipients. Shared outbox publication state remains independent.

There is no recurring historical anti-join. The one-time adoption scan and trigger's indexed
per-source history lookup are intentional. Pending membership is derived, so downgrade to 0039
can drop/reconstruct it without losing business facts; older code resumes its original scan.

## Migration and operational gates

Drain old customer materializers and registration writers for the 0039-to-0040 cutover, apply
the migration, then run the updated code with identical approved policy settings. Migration
refuses pre-existing live populations above 5 native or 10 browser bindings with the sanitized
`CUSTOMER_DEVICE_POLICY_REQUIRES_REVOCATION` error. Resolve excess through explicit authorized
revocations; migration never silently chooses a victim. Large-history adoption/index/DDL locking
must be validated for the target production volume before rollout. No production migration ran.

Android delivery stays disabled until the Mobile decoder normalizes FCM string `"1"` to the
numeric schema version, followed by real-device acceptance. Native PUT/DELETE paths already
match. APNs, browser registration adapter/real-browser acceptance, VAPID/FCM credentials,
approved legal/support content, retention/access decisions and real payment-method discovery
remain the previously reported release gates. No capability is activated by these fixes.

## Verification

- Final Linux customer suite: **90 passed in 118.03 seconds** (39 unit and 51 real-PostgreSQL
  integration cases). Includes actual POSIX SIGTERM, stalled resolver/executor/shutdown cases,
  concurrent admission and replay, late commit/availability, pre-commit failure, migration
  adoption/roundtrip and refusal of legacy excess without eviction.
- Existing financial/outbox producer regressions plus the corrected expiry case: **98 passed
  in 195.41 seconds**. Files: `test_collection_payment.py`, `test_reliability.py`,
  `test_financial_correctness_closure.py`, `test_customer_financial_cancellation.py` and
  `test_customer_financial_migration.py` (97 existing cases, plus one expiry case).
- Windows Python 3.10 provider/lifetime checks: **37 passed in 12.03 seconds**, before adding
  the final two Linux-covered cases. Windows sandbox IOCP stalled initial async runs; those
  verified test processes were stopped and checks rerun with local process access.
- Ruff lint and formatting: **572 Python files pass**. Strict mypy: **267 source modules pass**.
  `git diff --check` passes. Both disposable databases finish at migration 0040. The synthetic
  benchmark schema is removed; temporary test containers use `--rm`.
- The customer suite has one existing Firebase `MulticastMessage.tokens` deprecation warning.
  No live provider send, browser acceptance, frontend edit, production migration or deployment
  occurred. Frontend remains clean at `f4d96d2b828cb39e4edfd282827a10ed07cab261`; backend main
  remains `b281e03a47d202fd8b5c3d895075aa358062c5fa`.
- The full unrelated suite was not rerun for this follow-up. Its earlier Linux result and
  independently reproduced baseline `test_twofactor` failure remain disclosed in the original
  implementation report; these focused passes are not a claim of an entirely green full suite.
