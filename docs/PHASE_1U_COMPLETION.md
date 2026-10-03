# Phase 1U — Fleet-first dispatch and asynchronous rider push

Branch: `phase1/fleet-dispatch-push-clean`.
Clean base main: `ec40fa4fac332843a0cb88bde1e03a522cb77ebd` (verified against origin/main).
The rejected Jules branch was not used. No merge or deployment was performed.

## Implemented flow

Normal/fallback planning commits groups, pickups and one `CollectionGroupDispatchRequested`
outbox event per new group. The finite publisher routes one group/stage message to Service Bus
Standard's configured rider notification queue. The notification consumer locks the group and
establishes the complete eligible fleet cohort for its canonical H3 resolution-7 cell, or an
immediate independent round 1 if no fleet rider qualifies. All offers share offered_at, expires_at
and round. Offers and the complete current device/delivery set commit before any push.

Real FCM multicast sends generic navigation notifications outside PostgreSQL transactions.
The existing synchronous rider offer-acceptance endpoint still means durable assignment ownership
on success. After fleet expiry, a finite scanner requests one independent next round under the
same group lock; independent expiry leaves manager/manual fallback. No saga, orchestration,
automatic fleet assignment, per-rider broker messages, queued acceptance or new planning policy.

## Production and adversarial proof

| Invariant | Production enforcement | PostgreSQL protection | Adversarial proof |
| --- | --- | --- | --- |
| No inactive fleet receives fresh offers | `create_offer_cohort` selects/rechecks ACTIVE fleet and current matching coverage | Fleet row lock shared with fleet state/coverage/membership manager mutations | `test_fleet_fresh_eligibility_exclusions[inactive_fleet]`, left/wrong/deactivated coverage variants |
| No disabled/revoked rider receives fresh work | Cohort reuses `_lock_eligible_rider` and `lock_active_user_role` | Existing profile, availability, AppUser and live UserRole FOR UPDATE; role partial uniqueness | Disabled/revoked/suspended/offline/busy eligibility variants; existing Phase 1P fresh-work regressions |
| One common cohort timestamp | `create_offer_cohort` uses one evaluation_time/lifetime/round for all rows | Group FOR UPDATE + `uq_assignment_offer_business_key` + expiry/round CHECKs | `test_complete_fleet_cohort_common_timestamp_and_historical_replay` |
| FCM outside DB transactions | `process_dispatch_message` closes preparation/read sessions before `send_batch` | No provider I/O under a database row lock | `test_crash_after_prepare_resumes_entire_durable_device_set` checks checked-out connections and pg_stat_activity during provider invocation |
| Redelivery resumes PROCESSING | `prepare_dispatch_message` skips only PROCESSED; pending delivery rows reused | Inbox PK/row lock, group lock, `uq_offer_notification_delivery_device` | Crash-after-prepare, transient retry, ambiguous-send retry, duplicate-consumer tests |
| Serviceability publisher preserved | `publish_outbox_batch` still calls `send(serviceability_entity, serviceability_message(event))` before mark | Short outbox transactions; PUBLISHED conditional update only after successful send | `test_publisher_explicit_routes_send_before_mark_and_no_arbitrary_events` plus existing serviceability runtime suite |
| Planning really emits dispatch | Shared `_persist_groups` appends dispatch in normal/fallback result transaction | Outbox event-key uniqueness; planning batch/result locks and rollback | `test_planning_normal_and_fallback_emit_per_group_once[False/True]`, completed replay, existing planning atomicity tests |
| Rider push APIs exist and enforce ownership | New `post_push_device`/`delete_push_device`, existing live RIDER dependency; actor only from principal | Rider lock, current-device partial uniqueness, registration FK | `test_rider_own_device_register_replace_revoke_and_live_rbac` |
| Real FCM, not simulated production | `FcmPushNotificationPort` uses Firebase Admin Certificate/initialize_app/send_each_for_multicast/delete_app; missing/invalid configuration fails explicitly | Per-device delivery outcomes persisted, never provider body/token copies | SDK-call tests with actual MulticastMessage objects, configuration failure, per-device mapping, sanitized exception and off-event-loop cleanup tests |
| ORM/migration aligned | `0017_fleet_dispatch_notification`, dispatch models | Real FKs, CHECKs, partial current-membership/coverage/device uniqueness, delivery uniqueness | `test_0017_roundtrip_and_metadata_alignment` compares columns/types/defaults/FKs/indexes/uniqueness and new-table CHECK names |
| Fleet timeout/acceptance race safe | `scan_expired_fleet_cohorts` and unchanged acceptance share `_lock_group`; acceptance checks deadline after locks | Group FOR UPDATE, active assignment partial uniqueness, unique independent dispatch event key | `test_real_group_lock_serializes_fleet_acceptance_and_timeout[acceptance/timeout]`, two-rider winner and manager/offer convergence tests |
| No sequential push-order bias | Complete cohort/device snapshot first; port receives whole batch; `asyncio.gather` launches <=500-device chunks concurrently | Offers and all delivery pairs commit atomically before send | `test_fcm_real_sdk_multicast_chunks_start_concurrently` requires both chunk calls start before either completes; complete-batch/multi-device integration test |

## Reliability and APIs

PROCESSING inbox state is resumable, including a valid empty recipient snapshot. PROCESSED
skips further work. SENT and PERMANENTLY_FAILED deliveries are terminal; transient failures remain
PENDING and fail/abandon the broker invocation. Invalid tokens revoke only the token actually sent,
so a concurrent token replacement is not revoked by an old result. Settlement follows durable
completion. Ambiguous push may duplicate; offers and assignments cannot duplicate. Push is
at-least-once/best-effort, not guaranteed phone receipt.

Manager fleet commands use `client_command_id`, manager-specific idempotency scope, SHA-256
fingerprint and internal result ID. Command/history/completion commit together; replay returns the
historical resource even after membership ends. Rider/device registration is a serialized natural
current-state upsert; same value is a no-op, changed token replaces the current value, and DELETE
revokes the current device idempotently. Neither operation stores sensitive request copies.

Added Rider endpoints:

- `POST /v1/rider/me/push-devices`
- `DELETE /v1/rider/me/push-devices/{client_device_id}`

Added live MANAGER-authorized endpoints:

- `POST /v1/manager/fleets`
- `PUT /v1/manager/fleets/{fleet_id}/status`
- `POST /v1/manager/fleets/{fleet_id}/memberships`
- `POST /v1/manager/fleets/{fleet_id}/memberships/end`
- `PUT /v1/manager/fleets/{fleet_id}/service-cells`
- `PUT /v1/manager/riders/{rider_id}/service-cells`

Comparison against main confirms existing Rider/Manager handlers/models/transaction ownership
are unchanged: only imports and appended new models/handlers changed. Assignment acceptance,
manual assignment, pickup/reassignment, serviceability worker, payment/refund and planning
algorithm source are unchanged.

## FCM/runtime implementation

Dependency: `firebase-admin>=7.3,<8` (verification installed 7.7.0). Supported
`messaging.send_each_for_multicast` performs concurrent per-device fan-out, called through
`asyncio.to_thread`; all multicast chunks are launched concurrently. SDK cleanup is also offloaded
because Firebase messaging app cleanup runs its own event loop. No production fake is provided.
Credentials/project come only from `TIRODHAN_FCM_CREDENTIALS_JSON` (secret) and
`TIRODHAN_FCM_PROJECT_ID`. No application Key Vault calls or credential files are added.

Runtime entry points:

- `python -m tirodhan.workers.outbox_publisher` (existing finite publisher, one added route)
- `python -m tirodhan.workers.rider_notifications` (separate Peek-Lock consumer)
- `python -m tirodhan.workers.fleet_timeout` (bounded finite scan)

Set positive `TIRODHAN_RIDER_OFFER_LIFETIME_SECONDS` and
`TIRODHAN_RIDER_NOTIFICATION_QUEUE_NAME`; existing Service Bus namespace/operation timeout and
workload-identity configuration still apply. There is no invented lifetime default, broker
connection string, Redis, routing/scoring, new infrastructure or Terraform.

SDK references: [multicast API](https://firebase.google.com/docs/reference/admin/python/firebase_admin.messaging),
[Firebase Admin release notes](https://firebase.google.com/support/release-notes/admin/python).

## Changed files

Configuration/dependency: `.env.example`, `pyproject.toml`, `src/tirodhan/core/config.py`.

Migration: `migrations/versions/0017_fleet_dispatch_notification.py`
from `0016_refund_lifecycle`. Six new tables plus AssignmentOffer audience/fleet columns only.

Production:

- `src/tirodhan/api/routes/rider.py`
- `src/tirodhan/api/routes/manager.py`
- `src/tirodhan/modules/dispatch/models.py`
- `src/tirodhan/modules/dispatch/cohorts.py`
- `src/tirodhan/modules/dispatch/consumer.py`
- `src/tirodhan/modules/dispatch/devices.py`
- `src/tirodhan/modules/dispatch/events.py`
- `src/tirodhan/modules/dispatch/fleet_service.py`
- `src/tirodhan/modules/dispatch/push.py`
- `src/tirodhan/modules/planning/service.py`
- `src/tirodhan/modules/reliability/publisher.py`
- `src/tirodhan/modules/reliability/service_bus.py`
- `src/tirodhan/workers/outbox_publisher.py`
- `src/tirodhan/workers/rider_notifications.py`
- `src/tirodhan/workers/fleet_timeout.py`

Tests:

- `tests/integration/conftest.py` (new-table teardown only)
- `tests/integration/test_fleet_dispatch.py`
- `tests/integration/test_fleet_dispatch_api.py`
- `tests/integration/test_dispatch_routing_planning.py`
- `tests/integration/test_fleet_migration.py`
- `tests/unit/test_dispatch_push.py`

Authoritative docs: `docs/ARCHITECTURE.md`, `docs/DOMAIN_MODEL.md`, `docs/SCHEMA_DESIGN.md`,
`docs/IDEMPOTENCY.md`, `docs/adr/ADR-002-messaging.md`,
`docs/adr/ADR-006-rider-assignment.md`, plus this completion report.

## Verification and remaining operational inputs

Final focused verification: 37 passed (8 unit, 29 PostgreSQL integration).
Migration round-trip `0017 -> 0016 -> 0017` and metadata comparison passed.
Ruff check/format, mypy (105 source files), git diff check and a single Alembic head passed.
Final full suite with the disposable PostgreSQL/PostGIS database: **509 passed**
(316 PostgreSQL integration, 193 unit), no skips,
in 311.79 seconds. Four warnings are the supported Firebase registration-token API deprecation
described below. No existing test was suppressed, removed or weakened.

No unresolved implementation/architecture decision was silently selected. Deployment still needs
the offer lifetime, queue provisioning/entity-scoped Azure RBAC, Firebase project credentials and
hosting/scheduling review. No real Google/FCM/Azure network calls were made by tests. Supported FCM
token multicast emits the SDK's deprecation warning in 7.7.0; the explicitly requested registration
token contract is retained rather than silently changing to installation IDs. Future major-SDK
upgrade/client identifier migration needs separate review.
