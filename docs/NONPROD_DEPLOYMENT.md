# Azure NONPROD deployment substrate

This stack implements the accepted NONPROD deployment brief on top of main
`1d8560dcd580e5ba06fb9a0b7820045f82b15953`. It does not create PROD infrastructure,
alter business lifecycles, or add production pricing. `UnconfiguredPricingPort`
still prevents a fully fresh booking flow until pricing is supplied separately.

## Inputs and one-time bootstrap

Use Terraform **1.13.5**, AzureRM **5.8.0** and the committed provider lockfile.
The latter supports identity-based Azure Container Apps Service Bus scale rules;
4.66.0 did not support that schema. Do not change providers without revalidation.

An authorized operator runs `deploy/bootstrap/bootstrap.sh` from the repository
root using Bash, Python 3, Azure CLI and jq after `az login`. Required environment inputs:

- `AZURE_SUBSCRIPTION_ID`, `AZURE_TENANT_ID`, `AZURE_LOCATION`, `NONPROD_SUFFIX`;
- `DEPLOYMENT_OBJECT_ID` (federated service principal object ID),
  `FEDERATED_APP_OBJECT_ID` (application object ID), `OPERATOR_OBJECT_ID` (user);
- `GITHUB_REPOSITORY=krishnadhoundiyal/tirodhan`.

The operator needs provider-registration, resource creation, custom-role/RBAC and
application-federation authority. The script registers providers, creates the
NONPROD and separate state resource groups, an Entra-only state account and
private `tfstate` container, restricted deployment permissions, and federated
credentials for GitHub environments `nonprod-plan` and `nonprod`. It is rerunnable.
The operator is not the restricted Terraform identity. Never grant the deployment
identity Owner, Contributor, Storage Account Contributor, or other inherited
permissions that include `Microsoft.Storage/storageAccounts/listKeys/action`.

The custom control-plane role is scoped to the application resource group.
Data-plane roles supply state/application Blob access, delegation-key generation
and controlled secret seeding. Conditional RBAC Administrator delegation permits
only the five runtime data-role definitions, not escalation to Owner/Contributor.
One UAMI serves all workloads initially: queue-scoped Sender/Receiver (not Owner),
media-container Blob Data Contributor, account Blob Delegator and vault Secrets
User. Never grant the runtime UAMI PostgreSQL administrator/superuser privileges.

Before any cloud Terraform refresh/plan/apply, authenticate **as the restricted
deployment identity**, set `DEPLOYMENT_SCOPE` to the application resource-group
resource ID and run:

```bash
python -m tirodhan.deployment.azure_controls
cd infra/terraform/nonprod
terraform init -input=false -lockfile=readonly \
  -backend-config="storage_account_name=<separate-state-account>" \
  -backend-config="container_name=tfstate" \
  -backend-config="resource_group_name=<state-resource-group>"
```

Local backend authentication uses Azure CLI/Entra; CI uses OIDC with
`ARM_USE_OIDC=true` and `ARM_USE_AZUREAD=true`. State key is
`tirodhan/nonprod.tfstate`. No backend SAS, storage key or Azure client secret.
Verify effective permissions at both application and state storage-account scopes
once resources exist; inherited grants are additive, `NotActions` is not a deny.

Copy `nonprod.tfvars.example` into an ignored local `.tfvars` file. Supply actual
subscription/tenant/region, stable suffix/resource-group name, Entra PG admin
object/name/type, GHCR username and approved lifecycle/recovery thresholds. Null
retention examples are deliberate: they are required business inputs, not defaults.
`runtime_env` permits only the nonsecret names in `deploy/runtime-env.names`.
Workload stages require all listed values except optional log level. Lists are
JSON-encoded strings. Choose pool/duration/policy values explicitly; do not copy
synthetic test values into deployment. Images must be full commit-SHA GHCR tags.
No secrets belong in `.tfvars`, GitHub variables or Terraform resources/data sources.

## Staged first deployment

1. Apply a reviewed foundation plan at `deployment_stage=0`. This creates the
   database, extension allowlist, firewall, queues, storage, vault, UAMI, RBAC and
   Consumption-only ACA environment, **no live workloads**.
2. As the configured Entra PostgreSQL administrator, bootstrap PostGIS and the
   UAMI-backed SQL principal. Set `POSTGRES_HOST`, `POSTGRES_ADMIN_NAME`,
   `RUNTIME_DATABASE_ROLE`, `RUNTIME_PRINCIPAL_ID` from Terraform outputs, then
   run `python -m tirodhan.deployment.bootstrap_database` from the repository root.
   If connecting outside Azure, provide `postgres_bootstrap_ipv4` temporarily;
   remove it through Terraform afterward. Re-running checks the principal's OID.
   Grants are database CONNECT, public schema USAGE/CREATE and spatial-reference
   SELECT; the UAMI owns its migration-created tables. Admin pre-creates PostGIS.
   TLS certificates are verified, Entra tokens are obtained per connection, and
   Alembic retains its existing NullPool behavior. Password auth is disabled.
3. Allow RBAC propagation. Seed enabled Key Vault versions with the approved
   provider credentials, RSA key pair, encryption/HMAC material and read-only
   GHCR PAT. Set `KEY_VAULT_URL`, run
   `python -m tirodhan.deployment.seed_secrets deploy/operator-inputs/secrets.json`.
   The private JSON must contain exactly the names declared in that module;
   crypto key maps are JSON **strings**, PEM values retain their newlines.
   Do not commit this file or log values. Then set `LOG_ACCOUNT_NAME` and run
   `python -m tirodhan.deployment.rotate_log_sas` for the initial log SAS version.
4. Configure GitHub variables and protected environments below. Publish private
   GHCR images from reviewed main. Set/verify both packages' **private** visibility
   and repository package-access permission; GitHub defaults are not the authority.
5. The workflow plans/applies migration stage 1 and starts the manual Alembic Job.
   Its successful execution (including sidecar termination) gates runtime planning.
   Existing stages/images are preserved during migrations on subsequent releases.
6. Approve the runtime plan. Terraform dependencies deploy API (stage 2), workers
   (stage 3), then scheduled Jobs (stage 4). Verify acceptance below.

Azure-origin PostgreSQL firewall `0.0.0.0–0.0.0.0` is an explicitly accepted broad
**NONPROD-only** network exception. It is **not** a PROD recommendation. Public
TLS/Entra endpoints, no customer VNET/private endpoints, B1ms/32 GiB/no HA/no geo
backup/7-day backup, one UAMI and all other cost choices must not silently become
PROD defaults. Restore/PITR drills remain required operational work.

## GitHub delivery and rollback

Create `nonprod-plan` and `nonprod` environments restricted to **main**. Set
required reviewers and prevent self-approval on `nonprod`; without repository
environment protection, YAML alone cannot enforce human approval. Both migration
and runtime applies use that environment. Automatic rotation uses main-restricted
`nonprod-plan` so recurring refresh does not wait for deployment approval.

Repository variables: `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`,
`AZURE_SUBSCRIPTION_ID`, `NONPROD_RESOURCE_GROUP`, `NONPROD_STATE_ACCOUNT`,
`NONPROD_STATE_RESOURCE_GROUP`, `NONPROD_LOG_ACCOUNT_NAME`,
`NONPROD_KEY_VAULT_URL`, and `NONPROD_TFVARS_JSON` (nonsecret Terraform input object,
excluding stage/images). Federation must match the application/service principal.
Package pushing uses the job's ephemeral `GITHUB_TOKEN`; runtime pulling uses
Key Vault `ghcr-pull-pat`, scoped to `read:packages`. No Azure client password.

PR CI runs the full PostGIS suite, Ruff, mypy, Terraform fmt/init/validate without
Azure privileges. Cloud plans run only from protected main with environment-bound
OIDC, never `pull_request_target` or untrusted PR code. Saved plans have one-day
artifact retention; review them before approval. Serialized release execution
plus Terraform state locking prevent overlapping deployment pipelines. Terraform
owns images/configuration; there are no unmanaged `az containerapp update` calls.

For rollback, select a previously published immutable SHA through a reviewed
Terraform plan; retain the current stage. Do not automatically downgrade schema.
Check backward compatibility first, then apply/redeploy through approval. A failed
migration blocks rollout; retain its failed execution/Blob logs and fix forward.
Re-running the same workflow is safe; existing DB/domain idempotency stays intact.

## Logging, Jobs and rotation

All replicas include a budgeted Fluent Bit sidecar and shared writable EmptyDir.
The application entrypoint initializes ownership then drops root privileges before
executing the existing command. Hosted JSON uses `TIRODHAN_LOG_FILE_PATH`.
The image sets the application's actual home to `/app`, avoiding root-owned
credential-discovery paths after privilege drop.
No access logging/secret diagnostics, Azure Files, Log Analytics or App Insights.
The pinned Fluent Bit **4.0.11-debug** digest supplies Python for the bounded
supervisor. Its Azure Blob output uses SAS/TLS with certificate/hostname checks,
`auto_create_container off`, compressed block blobs, one-second flush, 1 MiB
buffer threshold, one-minute upload age and five-second HTTP timeout. Plugin exit
flushes remaining buffers; the supervisor allows 15 seconds then kills, so abrupt
termination/network failure can lose a small tail. Suppressed Fluent Bit console
diagnostics prevent accidental SAS-bearing URLs from reaching platform logs.

Finite commands run through `tirodhan.deployment.job`: immediate/periodic
heartbeat, protected stdout/stderr JSON records, exact exit code, done marker.
Arbitrary console content is intentionally redacted, including Alembic/provider
tracebacks; application-controlled JSON still flows directly to the file. Sidecar
exits on done, stale heartbeat (30 s), missing startup heartbeat (60 s), or SIGTERM
after bounded final flush. All five Jobs use this contract; business code unchanged.

Schedules are UTC: outbox/planning/fleet each minute, payment expiry every five
minutes. Parallelism 1 applies to **one execution**, not a no-overlap guarantee.
DB idempotency and planning `session_id=cell_id` remain authoritative. Planning
alone enables queue sessions; all consumers retain PeekLock. Worker apps have no
ingress, min 0, MI-authenticated Azure Container Apps Service Bus scale rules.

The OIDC rotation workflow runs every third day (month boundaries may shorten the
gap). Each run creates a new HTTPS-only **user-delegation**, container-scoped
`app-logs` SAS with write permission only, six-day expiry and five-minute clock
skew allowance (<7 days). Old versions remain usable during overlap. The pinned
plugin skips container checks/creation with auto-create disabled, so no read/list
permission is needed. `media` retains MI and user-delegation upload authorization.

ACA references **versionless** Key Vault URIs. Apps automatically retrieve newer
versions and restart active env-var consumers within about 30 minutes. **Do not
assume Jobs share this lifecycle**: validate a new Job execution independently
after rotation; existing executions may retain their initial secret. No SAS value
is read into Terraform. Rotation failure is actionable before expiry, not a reason
to introduce a hosted rotator or account-key fallback.

## Acceptance / safe verification

Local validation is not Azure acceptance. After an authorized apply:

- Verify `/health` and `/ready` over HTTPS; API can cold-start from min 0.
- Verify PostgreSQL verified TLS/Entra connectivity, PostGIS and Alembic head;
  confirm password auth remains off and remove temporary operator firewall rule.
- Send approved identifier-only test messages through existing workflows. Check
  each worker scales from 0, renews PeekLock and settles correctly; planning uses
  per-cell sessions. Confirm bounded replica counts and no worker ingress.
- Verify GHCR private pulls, UAMI-only Azure access and Key Vault secret references
  without printing secrets. Validate current media registration/upload/finalization
  using an authorized synthetic NONPROD user; do not invent pricing or a download
  API. A storage-level user-delegation read SAS can be tested independently.
- Check API and worker JSON objects reach private `app-logs`. After SAS rotation,
  wait for App secret refresh, exercise/wake apps and confirm **new Blob writes**.
  Separately start the manual migration Job, confirm it ends Succeeded, and confirm
  its `job_completed` records reach Blob using the newly rotated secret. Record
  both outcomes and secret-version times (not values). Do not consider rotation
  accepted until **both** App and new-Job paths work. If Jobs retain a cached
  version, use an approved Terraform-controlled redeployment; do not assume or
  conceal a refresh guarantee the platform has not demonstrated.
- Exercise success, failure and killed/stale Job cases; verify no endless sidecar.
- As the restricted identity, verify listKeys is unavailable at application/state
  account scopes. After every plan/apply, safely inspect state:

```bash
terraform show -json | python -m tirodhan.deployment.check_state
```

The checker reports attribute paths only, never values; Terraform sensitivity and
unknown annotations are excluded. Do not print raw `terraform state pull`, enable
SDK wire logging, use `TF_LOG`, or run `az storage account keys list` for debugging.
Confirm both accounts have Shared Key off and both containers are private.

Track real NONPROD cost from execution duration, cold starts, aggregate resource
usage and Blob operations, **not invocation count alone**. API/planning total
0.75 vCPU/1.5 GiB; other workers and each finite Job 0.5/1 GiB (including sidecar),
valid Consumption allocation ratios. Scheduled invocations are accepted; do not
replace finite Jobs for convenience. PostgreSQL and Service Bus have baseline
charges even when apps scale to zero. Retention/cool-tier inputs require approval.

Reference details: [Fluent Bit Azure Blob](https://docs.fluentbit.io/manual/4.0/data-pipeline/outputs/azure_blob),
[pinned plugin final flush](https://github.com/fluent/fluent-bit/blob/v4.0.11/plugins/out_azure_blob/azure_blob.c),
[ACA Key Vault secret refresh](https://learn.microsoft.com/en-us/azure/container-apps/manage-secrets),
[PostgreSQL Entra principal functions](https://learn.microsoft.com/en-us/azure/postgresql/security/security-manage-entra-users).
