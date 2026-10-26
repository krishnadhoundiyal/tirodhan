# BDEP-01 — NONPROD runtime integration inventory

**Baseline:** merged PR #79, unchanged deployment stages 0–4. **State:** review-only
Terraform integration; no Azure provisioning, migration or provider activation.

## Runtime classification

| Python entry point | Hosting / treatment |
|---|---|
| serviceability, rider_notifications, refunds, planning_worker, financial_webhooks | Existing stage-3 ACA Service Bus consumers; keep distinct financial webhook identities |
| outbox_publisher, planning_scheduler, fleet_timeout, pending_payment_expiry | Existing stage-4 finite Jobs and schedules |
| financial_inventory | Existing opt-in stage-4 financial inventory Job |
| financial_reconciliation | Invoked by payment-expiry Job when all financial policy settings are supplied; do not duplicate |
| customer_notifications | New opt-in stage-4 finite Job; PostgreSQL pending membership, no new queue |
| routing_availability | New opt-in stage-4 due-region projection reconciliation Job; requires orchestration flag |
| routing_recovery | Already consumed by planning_worker from RecoveryWorkRequested; no separate app |
| routing_admission | Retired; never schedule |
| routing_retention | Operator-only read-only inventory until retention decisions are approved |
| routing_probe | No autonomous native-work coordinator approved; do not schedule in isolation |
| routing_regional, routing_fleet | Require native OSRM/solver packaging and approved profile files; no invented cron |
| routing_shadow | Explicit QA/shadow run only |

## Activation gates

1. Reconcile remote Terraform state against actual Azure inventory after October 7
   partial destroy. Recovered Key Vault names require explicit review.
2. Preserve initial GitHub OIDC bootstrap, stage-0 Terraform foundation, separate
   PostgreSQL Entra runtime-principal bootstrap, stage-1 Alembic migration Job,
   then stage-4 runtime apply behind protected environment approval.
3. Opt-in schedules `customer_notifications_schedule` and
   `routing_availability_schedule` are null by default, adding no workloads.
   Review actual costs, cadence, database queries and timeout limits before enabling.
4. Customer history/push requires independent cursor, encryption and HMAC keys;
   seed approved complete batches and retain decryption/HMAC continuity.
   Do not activate FCM/Web Push until Android/browser acceptance, provider origins,
   APNs limitation, legal content and retention controls are resolved.
5. Run customer notification work only after migration
   `0040_customer_notification_work.py` reaches Alembic head. A 300-second
   scheduled Job cannot automatically guarantee completion of a full enabled
   push batch with 22-second individual Web Push deadlines.
6. Validate routing projected-slot policy before orchestration activation;
   native route-planning binaries/artifacts remain out of scope.

**No main branch change, paid deployment, credential seeding or activation is
authorized by this draft.**
