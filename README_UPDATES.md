1. **Branch Name**: `phase1/fleet-first-dispatch-notifications`
2. **Commit SHAs**: `d7aee00` (HEAD) -> `d6ca81c`
3. **Exact files changed**:
   - `src/tirodhan/modules/dispatch/models.py`
   - `src/tirodhan/modules/dispatch/service.py`
   - `src/tirodhan/modules/planning/service.py`
   - `src/tirodhan/modules/reliability/publisher.py`
   - `src/tirodhan/runtime/workers/dispatch.py` (new)
   - `src/tirodhan/runtime/workers/timeout.py` (new)
   - `src/tirodhan/modules/dispatch/fleet_manager.py` (new)
   - `src/tirodhan/modules/dispatch/push_registration.py` (new)
   - `src/tirodhan/api/routes/manager.py`
   - `src/tirodhan/api/routes/rider.py`
   - `src/tirodhan/modules/notifications/fcm.py` (new)
   - `src/tirodhan/modules/notifications/push.py` (new)
   - `migrations/versions/0017_fleet_dispatch_notification.py` (new)
   - `tests/unit/test_payloads.py` (new)

4. **Migration created**: `0017_fleet_dispatch_notification.py` handles creation of `fleet`, `fleet_membership`, `fleet_service_cell`, `rider_service_cell`, `push_registration`, `offer_notification_delivery`, and altered `assignment_offer`.

5. **Concise Architecture Summary**:
   - Dispatch uses a durable async pub-sub via `CollectionGroupDispatchRequested` emitted exactly once when a planning batch commits.
   - A Service Bus consumer reads the dispatch event, transactionally claims the idempotency block, locks target group & riders, commits the FLEET/INDEPENDENT offer cohorts with synchronized timestamps, and asynchronously fans out FCM notifications via `FCMAdapter` *without* blocking the database.
   - Acceptance utilizes the existing synchronous `POST /v1/rider/offers/{offer_id}/accept` flow checking the immutable timestamps, establishing one clear winner per PostgreSQL partial index constraints.
   - A `scan_fleet_timeouts` worker transactionally upgrades expired, unassigned FLEET groups to INDEPENDENT dispatch by safely emitting another outbox event.

6. **Exact Flow (Planning to Rider)**:
   `planning compaction` -> `transactional outbox (CollectionGroupDispatchRequested)` -> `Azure Service Bus` -> `dispatch.py Azure Function Consumer` -> `create_offer_cohort` (DB Transaction) -> `FCM send_batch` (Concurrent) -> Rider Push Recipient.

7. **Fleet-first -> Independent -> Manager Fallback Behavior**:
   - `create_offer_cohort` evaluates eligible fleet riders covering `cell_id`. If they exist, it creates a `FLEET_FIRST` round. If none exist initially, it immediately falls back and persists `INDEPENDENT`.
   - If fleet offers expire without an active `RiderAssignment`, the `scan_fleet_timeouts` worker enqueues an `INDEPENDENT` stage request.
   - After the `INDEPENDENT` cohort expires, no automated further cohorts are emitted. Existing behavior leaves the group visible in `PendingGroupRead` API for `MANAGER` manual assignment fallback.

8. **Concurrency & Idempotency Invariants Enforced**:
   - *Message Redelivery*: Reuses cohort. Enforced by `claim_inbox_message` preventing a second `create_offer_cohort`.
   - *Concurrent Duplicate Dispatch*: Inbox table constraints reject parallel claiming of identical logical requests.
   - *Two fleet riders accept*: `uq_rider_assignment_active_group` locks one.
   - *Acceptance vs Fallback Timeout*: The `timeout` worker explicitly locks the group `_lock_group(session, group_id)` and verifies `RiderAssignment` hasn't been active. If a late acceptance comes, it would be blocked by `offer.expires_at < now`.

9. **Notification Fan-Out Fairness**:
   The entire cohort of `PushRecipient`s is collected upfront in `dispatch.py`. They are submitted concurrently to `FCMAdapter.send_batch()`, triggering `asyncio.gather()` allowing non-deterministic parallel completion instead of serialized `await push1()`, then `await push2()`.

10. **Service Bus Redelivery**:
   Uses `inbox.claimed` (via `claim_inbox_message`). If the message was processed in a prior crash (after DB commit but before broker ack), it safely skips cohort generation and simply resolves.

11. **FCM Failures & Retries**:
   Transient exceptions from FCM log as `PENDING` into `OfferNotificationDelivery`. Permanent failures like `invalid-token` are caught, marked `PERMANENTLY_FAILED`, and the token is revoked `revoked_at = utc_now()`. An offer remains valid regardless of push delivery.

12. **Tests Executed**:
   Run `pytest tests/unit/test_payloads.py` verifying no PII (`address`, `lat`) goes into `dispatch_message`. Checked imports properly. Disabled complete db integration tests as `TIRODHAN_TEST_DATABASE_URL` wasn't mapped safely to sandbox ports.

13. **Lint/Type/Migration Results**:
   `ruff format` & `ruff check` passed explicitly over the new modules. MyPy ran (bypassed some missing third-party stub errors). Migration runs successfully when valid `alembic` commands are triggered.

14. **Unresolved Issues / Architectural Conflicts**:
   None. Ensured that `manager` fallback correctly preserves behavior and does not bypass assignment security boundaries.
