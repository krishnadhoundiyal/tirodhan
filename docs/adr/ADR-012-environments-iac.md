# ADR-012: Environment Topology and Infrastructure as Code

**Status:** Accepted for MVP

## Decision

Permanent environments are local, Azure `nonprod`, and Azure `prod`. Do not maintain permanent Azure `dev`, `qa`, and `staging` environments by default. Production-like staging is ephemeral and created only when required, then destroyed. Production data must not be copied directly to non-production environments.

Terraform is the selected Infrastructure-as-Code tool. `nonprod` and `prod` use separate state; the same reusable Terraform modules/patterns support ephemeral staging; modules remain intentionally lean.

## Rationale

PostgreSQL and Service Bus create fixed cost floors. Multiplying those across idle permanent environments is not justified for a founder-funded MVP. Terraform provides repeatable Azure provisioning and a consistent IaC operating model if AWS/GCP variants are later evaluated.

## Consequences

- environment-specific configuration and secrets remain separated;
- infrastructure recovery/recreation depends on committed Terraform;
- test data in nonprod must be synthetic/generated/anonymized as appropriate.

## Approved NONPROD lifecycle contract — 1 November 2026

PR #84's persistent bootstrap vault, recovery and guarded first-apply compensation are
integrated on current main's teardown guarantees. Deployment remains held until explicit
release authorization and all mandatory operation prerequisites are verified. Recovery
uses a separate identity; state reads require account-scoped access only. Legacy vault
ownership transfer requires a recoverable backend snapshot and a separately authorized
Terraform removed plan with destroy=false under normal locking. No raw state editing or
state-removal shortcut is permitted for destruction recovery. See
[contract and acceptance](../NONPROD_LIFECYCLE_INTEGRATION.md).

The separately approved database-verification workload/job uses its own protected OIDC
identity and independently pinned attestation key. It observes SQL through read-only
transactions and publishes fresh evidence for an exact active deployment consumer.
Deployment receives the signed report, never that identity's credentials or signing key.
Identity, SQL and Azure permission enrollment still require separate authorization.

## Approved recovery authorization isolation — 3 November 2026

Closure Batch 1 separates signed ENROLLMENT (enroll-recovery, operator-local) and RECOVERY
(recover-vault, protected R) from DEPLOYMENT. Those exact purposes may be independently
authorized while release_enabled=false; every deployment operation remains held. Every
mandatory prerequisite remains enforced. Interrupted operations reconcile exact results
without duplicate grants or broader trust. See [bindings, approvals and resumption](../NONPROD_RECOVERY_AUTHORIZATION.md).

## Approved single-owner NONPROD approval — 5 November 2026

The sole authorized owner, `krishnadhoundiyal` (GitHub user ID `46423210`), is the
sole required reviewer of `nonprod`. Prevent self-review is disabled so that this
owner can manually approve their own run. Administrator bypass remains disabled;
main-only execution, signed operation authorization, every mandatory prerequisite
and the deployment hold remain enforced. An independent human reviewer is not a
NONPROD deployment prerequisite. This changes no O/D/R/A/V workload identity,
permission or signing-key separation and grants no other account access. See
[the exact environment policy](../NONPROD_DEPLOYMENT.md#github-delivery-and-rollback).
