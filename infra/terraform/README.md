# Phase 1T serviceability resources

`serviceability/` is a reusable focused AzureRM module. A deployment root supplies an
AzureRM provider (subscription, workload federation), backend and reviewed inputs. Use
separate nonprod/prod state; review plan before apply. No infrastructure is applied by
application startup or tests.

```sh
terraform -chdir=infra/terraform/serviceability init -backend=false
terraform -chdir=infra/terraform/serviceability fmt -check
terraform -chdir=infra/terraform/serviceability validate
```

Inputs are intentionally explicit: existing ACA environment/resource group, accessible
release-pinned GHCR image, existing RBAC Key Vault ID and secret URIs (PostgreSQL asyncpg
TLS URL, Google API key, independent address AES keyring), active key ID, queue lock/TTL/
DLQ delivery limits, provider/network timeouts, lock-renewal bound, max replicas, bounded
publisher batch size, finite Job timeout and UTC cron schedule. Set lock-renewal duration
above the bounded provider/DB processing budget. No production values are frozen here.

An existing **Standard** namespace can be supplied by ID and matching name; otherwise
the module creates Standard with local auth disabled. It creates only the serviceability
queue (no sessions), separate worker/publisher identities with queue-scoped Receiver/Sender,
and individual secret-scoped Key Vault Secrets User grants. Google and AES keys are not
passed as Terraform values; no secret value data sources or secret resources are created.
Secret values and operational API restrictions are populated separately through the
existing secret-management workflow. RBAC propagation may require staged deployment.

The consumer has no ingress, uses a managed-identity Service Bus scale rule and min=0.
The scheduled finite publisher Job is a provisional deployment mechanism (ADR-015),
with retry_limit=0, parallelism=1. Overlapping schedules
are safe; outbox rows remain authoritative and duplicate sends are expected. Existing
PostgreSQL, API deployment, image registry, vault and ACA environment are not recreated.
Configure the existing API deployment with the same Google/address secret references and
timeout/alias settings for checkout fallback. API needs no Service Bus permissions.

No new Log Analytics, Application Insights, Redis or diagnostic pipeline is provisioned.
The host estate remains responsible for the ADR-009 shared-volume/Fluent Bit log pipeline,
network access, PostgreSQL database grants, image access and migration orchestration.
Secrets must never enter tfvars or state. Do not reuse production data in nonprod.
