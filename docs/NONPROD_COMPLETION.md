# NONPROD deployment substrate completion

Branch: `phase1/nonprod-azure-deployment-substrate`.
Base: `1d8560dcd580e5ba06fb9a0b7820045f82b15953`. No merge or new migration.

## Implementation

- Flat, pinned Terraform stack: Consumption-only ACA, API, four independently
  scaled MI-authenticated workers, five finite/manual/scheduled Jobs; sidecar
  resources included in every budget.
- Entra-only separate state/application storage, private media/log containers,
  restricted deployment permissions with an effective listKeys guard and safe
  state checker. Terraform stores Key Vault references, never seeded values.
- Public verified-TLS/Entra PostgreSQL B1ms, 32 GiB, 7-day backup, no HA/geo,
  explicit NONPROD Azure-origin firewall; controlled PostGIS/UAMI SQL bootstrap.
- Standard Service Bus, planning-only sessions, existing cell routing unchanged;
  queue-scoped Sender/Receiver, one runtime UAMI, no Data Owner.
- Private SHA-tagged GHCR, OIDC workflows and approval-gated migration-before-runtime
  deployment. Automated overlapping six-day user-delegation log SAS rotation.
- Shared-volume JSON logs, pinned Fluent Bit, redacted finite-command console
  capture, heartbeat/done markers, exact exit code and bounded final flush/stale
  shutdown. The workload drops root and uses its real home directory.
- Runbook: [NONPROD deployment](NONPROD_DEPLOYMENT.md).

## Executed verification

| Check | Result |
|---|---|
| Full pytest, Python 3.12 Linux + disposable PostgreSQL 17/PostGIS 3.5 | **683 passed**, 4 existing FCM deprecation warnings, 275.67 s |
| Focused deployment/bootstrap unit tests, Windows Python 3.10 | **35 passed**, 1 POSIX-only skip; that test passed in Linux full suite |
| Ruff check | PASS |
| Ruff format --check | PASS |
| mypy | PASS (124 source files) |
| Terraform fmt -check -recursive | PASS |
| Terraform init -backend=false -input=false -lockfile=readonly | PASS |
| Terraform validate | PASS, Terraform 1.13.5 / AzureRM 5.8.0 |
| Provider checksums | Official signed Linux AMD64 + Windows AMD64 lock entries |
| Both Docker builds | PASS |
| Actual pinned Fluent Bit local HTTP-stub probe | PASS: done + stale shutdown, compressed final JSON, write-only PUT requests |
| Bash bootstrap syntax | PASS |
| Final-image Alembic wrapper, fresh disposable DB | Exit 0, done marker 0, job_completed JSON; head 0017_fleet_dispatch_notification |
| Final-image API smoke | /health = ok, /ready = ready; workload PID 1 UID 999 |
| git diff --check | PASS |

The Linux full suite used the image's Python/dependencies with final repository
source mounted read-only; both final image builds and actual entrypoint/wrapper
smokes were also verified. No Azure resources were deployed or billed by this run.
The temporary API smoke container was stopped and removed.

An additional Fluent Bit 4.0.11 `--dry-run` printed successful syntax validation
but crashed in shutdown (SIGSEGV); it is **not** reported as passing. The real
plugin runtime/final-flush probes above passed. They also detected the required
`Upload_File_Size 1M` notation before final verification.

Seven pre-existing files received mechanical formatting, one redundant encode
argument was removed, the existing asyncpg untyped dependency was excluded from
external-library analysis, and one stale planning retry assertion gained the
already-established cell_id routing field. Business behavior is unchanged.

## Still requiring live Azure verification / supplied inputs

No unresolved architectural decisions. Actual subscription/tenant/region/admin,
federation, private-package access, secrets, runtime policies and retention inputs
remain operator supplied. Cloud plan/apply and repository Environment approval
protection have not been exercised here.

After authorized apply, execute the runbook's acceptance checklist: verified
Entra/PostGIS/migrations, effective no-listKeys permissions and key-free state,
private GHCR pulls, scale-from-zero and MI PeekLock/session behavior, media
user-delegation operations, Blob log writes, and independent **App versus new Job
execution** secret consumption after rotation. Validate actual cost, PITR recovery,
regional quota and resource allocation. No production defaults or fake pricing.

## Exact file manifest (59 files)

- `.dockerignore`
- `.gitattributes`
- `.github/actions/nonprod-setup/action.yml`
- `.github/workflows/nonprod-deploy.yml`
- `.github/workflows/pr-checks.yml`
- `.github/workflows/rotate-log-sas.yml`
- `.gitignore`
- `Dockerfile`
- `deploy/bootstrap/bootstrap.sh`
- `deploy/bootstrap/deployment-role.json`
- `deploy/fluent-bit/Dockerfile`
- `deploy/fluent-bit/parsers.conf`
- `deploy/fluent-bit/tirodhan.conf`
- `deploy/runtime-env.names`
- `docs/NONPROD_COMPLETION.md`
- `docs/NONPROD_DEPLOYMENT.md`
- `infra/terraform/nonprod/.terraform.lock.hcl`
- `infra/terraform/nonprod/backend.tf`
- `infra/terraform/nonprod/container_apps.tf`
- `infra/terraform/nonprod/container_apps_environment.tf`
- `infra/terraform/nonprod/database.tf`
- `infra/terraform/nonprod/identity.tf`
- `infra/terraform/nonprod/jobs.tf`
- `infra/terraform/nonprod/key_vault.tf`
- `infra/terraform/nonprod/locals.tf`
- `infra/terraform/nonprod/nonprod.tfvars.example`
- `infra/terraform/nonprod/outputs.tf`
- `infra/terraform/nonprod/providers.tf`
- `infra/terraform/nonprod/rbac.tf`
- `infra/terraform/nonprod/resource_group.tf`
- `infra/terraform/nonprod/service_bus.tf`
- `infra/terraform/nonprod/storage.tf`
- `infra/terraform/nonprod/variables.tf`
- `infra/terraform/nonprod/versions.tf`
- `pyproject.toml`
- `src/tirodhan/deployment/__init__.py`
- `src/tirodhan/deployment/azure_controls.py`
- `src/tirodhan/deployment/bootstrap_database.py`
- `src/tirodhan/deployment/check_state.py`
- `src/tirodhan/deployment/coordination.py`
- `src/tirodhan/deployment/entrypoint.py`
- `src/tirodhan/deployment/inputs.py`
- `src/tirodhan/deployment/job.py`
- `src/tirodhan/deployment/release.py`
- `src/tirodhan/deployment/rotate_log_sas.py`
- `src/tirodhan/deployment/seed_secrets.py`
- `src/tirodhan/deployment/sidecar.py`
- `src/tirodhan/modules/reliability/publisher.py`
- `src/tirodhan/workers/pending_payment_expiry.py`
- `src/tirodhan/workers/planning_scheduler.py`
- `src/tirodhan/workers/planning_worker.py`
- `tests/deployment/fluent_bit_probe.py`
- `tests/integration/test_planning_result.py`
- `tests/unit/test_database_bootstrap.py`
- `tests/unit/test_database_entra.py`
- `tests/unit/test_deployment_job.py`
- `tests/unit/test_jobs.py`
- `tests/unit/test_nonprod_substrate.py`
- `tests/unit/test_planning_worker_session.py`
