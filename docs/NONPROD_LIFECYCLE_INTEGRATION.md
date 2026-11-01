# NONPROD lifecycle integration

Baseline: main `73cb349dd65065259168b7b066298a521915f07f` (successful destroy run 8).
Functionality source: PR 84 `24ac70d5ae9dad3084827277e6a31f0272de759d`.
The approved 1 November 2026 contract supersedes that branch's unguarded recovery,
state-RG read and imperative state-removal implementation details.

Implementation is **not release authorization**. The committed policy disables release
and contains no authorization/evidence keys. Caller-provided PASS Booleans do not grant access.

## Review and merge order

1. Review hold PR 96, including CodeRabbit and repository checks. Do not merge without
   explicit user approval. Its commit is independent of the lifecycle integration.
2. Review the integration stacked on that hold. Require final tests and CodeRabbit review.
3. After separate explicit approvals, merge the hold first, then retarget/revalidate the
   integration against main. Neither merge releases provisioning.
4. Separately approve and enroll authorization/evidence public keys and identity permissions.
   No bootstrap, recovery, ownership transfer, state initialization or deployment is
   authorized by preparing this source change.
5. Authorize the exact reviewed release/configuration/operation before Azure acceptance.

Protected destroy remains independent of release admission, so integration cannot strand
existing resources. There is no new paid service or production activation.

## Contract and authoritative mapping

`PROVISIONING_ALLOWED = RELEASE_AUTHORIZATION_VALID AND
PREREQUISITES_SATISFIED(current_operation_context)`.

`deploy/nonprod-prerequisites.json` maps every prerequisite to configuration authority,
identity, Azure scope, allowed operation, failure behavior and applicable operation profile.
Independent required sets in `prerequisites.py` reject omissions. Every mandatory result
must be exactly Boolean True. Missing, unimplemented, unknown, denied, expired or malformed
evidence blocks. Nonapplicability comes only from the reviewed operation profile, with an
explicit recorded reason. Admission emits the passed check IDs and context hashes.

No observation restores a vault, changes grants or transfers ownership. Establishment
operations have separate identity/authorization profiles. Their actual postconditions are
verified and dependent deployment prerequisites are evaluated again under D. No stored
caller PASS receipt or global readiness flag substitutes for observations.

## Signed release admission

Offline admission precedes Azure login. Existing PyJWT/cryptography verifies Ed25519
authorizations against public keys pinned in `deploy/nonprod-release-policy.json`.
Private authorization keys stay with the approved authorizer, outside Git and Azure jobs.

Required claims: issuer `tirodhan-nonprod-release`, audience
`tirodhan-nonprod-provisioning`, exp/iat/nbf, subject principal OID, authorization ID (jti),
repository, commit, release session, configuration/source hashes, operations, identity kind
and the particular operation's binding. Signed validation identifies the CI commit,
matching contract hash and CodeRabbit review evidence checked by the authorizer.
The source hash covers validator/application Python, migrations, deployment definitions and
locked Terraform configuration; generated tfvars/plans/backups are excluded.

Use masked GitHub secrets `NONPROD_RELEASE_AUTHORIZATION` and protected
`NONPROD_APPLY_AUTHORIZATION`; authorization tokens are not public dispatch inputs.
The dispatch `authorization_id` identifies the signed session. Main pushes and schedules
have no implicit authorization. Scheduled SAS rotation requires its own
`NONPROD_LOG_SAS_AUTHORIZATION` secret and `NONPROD_LOG_SAS_AUTHORIZATION_ID` session,
with an exact signed cron binding and `scheduled_execution_authorized: true`. The usual
expiry/commit/configuration/contract/identity checks still apply on every occurrence;
this is an explicitly authorized recurring session, never a wildcard release. Renew the
authorization after each main commit and before expiry. Missing/stale authority fails
the workflow visibly and must be acted on before the six-day log credential expires.
Applies can require a new binding after plan review.
Apply bindings include exact plan SHA-256 and remote state fingerprint, including lineage,
serial and ETag. Native Terraform locks/stale-plan rejection remain enabled. There is no
force-unlock, lock=false, raw state editing or `terraform state rm` implementation.

Actual trusted-main/environment protection is checked. Local execution requires an explicit
signed local-execution binding. Repository CI and required review remain independent gates;
the authorizer must verify their evidence before issuing a release authorization.

## Identity and permission boundaries

- O: authorized bootstrap operator. Verify exact inputs, resource/provider/RBAC authority,
  Entra application ownership and signed enrollment authority before creating anything.
- D: existing deployment SP. Backend reads use exact account/container/blob access, never
  state-resource-group Reader. Runtime UAMI/data-plane roles are unchanged.
- R: distinct recovery SP under protected `nonprod-recovery`, for exact-name recovery only.
  Azure vaults/write is broader than a recover-only permission; code bounds the target/action.
- A: configured PostgreSQL administrator. D receives no database administrator authority;
  runtime UAMI remains non-admin.
- V: separately approved verifier service principal, distinct from D/R/A/runtime. Protected
  `nonprod-database` OIDC and its own pinned attestation key; no enrollment/grants in its job.

Source-only enrollment changes require separate approval: D gets a custom subscription
metadata role with only `Microsoft.Resources/subscriptions/providers/read` and
`Microsoft.DBforPostgreSQL/locations/capabilities/read`, with no RG Reader, writes or data
actions. O also needs signed `bootstrap_discovery_authorized`. These are the documented
[management](https://learn.microsoft.com/en-us/azure/role-based-access-control/permissions/management-and-governance)
and [database](https://learn.microsoft.com/en-us/azure/role-based-access-control/permissions/databases) operations.
R's RG role allows vaults/read, vaults/write and its own permissions/read; subscription deleted-
vault metadata discovery is separate. Role definitions are checked, not just display names.
All five original direct D grants and their delegation condition remain mandatory. Only an
explicitly configured/verified R grant is additionally permitted. Unknown grants are not filtered.
No live RBAC has been modified.

If a vault is already soft-deleted before R was enrolled, O can use the separately signed
`foundation_reconcile enroll-recovery` operation. It requires `recovery_enrollment_authorized`,
the exact `recovery_principal_id` and `recovery_app_object_id`, actual role-definition and
assignment authority, and verified application ownership. It never reads/writes the vault
or Terraform state. Deterministic role/assignment IDs and the unique federation name make
replay converge; incompatible existing roles/trust block rather than being rebound.
This operator-only path is intentionally absent from the restricted D workflow. Afterwards,
R must obtain its own separately approved recovery authorization, recover the exact vault,
and D must reevaluate actual prerequisites. Enrollment does not establish readiness.

## Authoritative state cases

| State | Consequence |
|---|---|
| BACKEND_ABSENT | Verified account absence; O bootstrap required |
| STATE_ABSENT | Secure account/container exists, state object absent; explicit initialization required |
| CLEAN_INITIALIZED | Valid remote lineage/serial, zero managed resources and verified bootstrap-only inventory |
| PARTIAL_OWNED | Owned state differs from actual inventory; reviewed resume, never automatic compensation |
| ESTABLISHED_OWNED | Disposable inventory belongs to authoritative state; stage checks still required |
| CONFLICT/UNKNOWN | Foreign/untracked resources, denied reads, malformed observations or ambiguity block |

Empty outputs or an empty RG listing never establish readiness. Initialization uses an
explicitly authorized refresh-only zero-managed-change saved plan, then rereads the real
backend. No synthetic state is uploaded and no existing state is overwritten.

The persistent vault is operator-bootstrap-owned and a Terraform data source. Transfer:
verify exact legacy binding/security, plan a targeted `removed { destroy = false }`, take
an ETag-protected backend snapshot, download/read back that immutable backup, and apply
only the explicitly authorized single-forget saved plan under native locking. State changes,
unknown backups or any extra managed action block. Replay verifies already-transferred
state against the original backup; no second mutation is necessary.

## Database, material and activation

Foundation readiness requires every stage-zero resource instance, including all queues,
role assignments, identities, storage children and database configuration. The reviewed
`nonprod-foundation-inventory.json` is checked against the complete Terraform source file
set and hashes on every evaluation; CI independently parses HCL to prove resource and
expansion coverage. Any source drift blocks until that inventory is reviewed again.
Each required binding must exist both in remote state and in a fresh exact-ID ARM read.
The conditional bootstrap firewall follows the authorized input, never an empty output.

A bootstrap and V verification observe actual SQL principal OID/type, non-admin/elevated-role status, PostGIS, CONNECT,
schema USAGE/CREATE and spatial-reference SELECT before signing evidence with a separately
pinned A key. Missing signer configuration blocks before grants. `bootstrap_database
--verify-only` refreshes evidence without repeating grants. Evidence contains no Azure token
or SQL data and is bound to current server/UAMI/configuration/source and configured expiry.
D compares it against fresh ARM identities. Missing evidence cannot become PASS.

The automated release no longer consumes a repository-stored SQL report. Immediately
before each database-gated controller evaluation, after that D job's environment approval,
D dispatches `nonprod-database-verify.yml` using the ephemeral repository `GITHUB_TOKEN`.
GitHub explicitly supports [workflow_dispatch with that token](https://github.blog/changelog/2022-09-08-github-actions-use-github_token-with-workflow_dispatch-and-repository_dispatch/).
Only trusted D jobs receive `actions: write` for this request; PR checks remain read-only.
This GitHub permission is repository-wide, so workflow protection and signed operation
authorization remain necessary. It grants no Azure or SQL privilege to D.

V's protected job rechecks signed `database-verify` authority and the actual requesting
main run/attempt/job before Azure login and again before observing SQL. Authorization must
include `database_verification_authorized: true` and explicit `consumer_operations`.
The request and resulting signature bind consumer operation, release session, authorization
ID, commit, configuration/source hashes and requesting principal. D rejects a report for
another consumer even if its signature and timestamp are valid. The verifier has a separate
concurrency group, avoiding deadlock with the waiting release's global group.

Before dispatch, a composite action verifies D's signed authority and uploads an immutable,
nonsecret request bundle in that exact consumer run. V checks the bundle's bytes against
the request before Azure login. A caller with only workflow-dispatch permission cannot
invent a request absent from the trusted run's artifacts. The bundle contains identifiers
and hashes, not authorization JWTs, OAuth tokens or private keys. Artifact names include
job and attempt; contents include the exact per-operation request digest.

V uses `READ ONLY` transactions, bounded connection/statement timeouts and no DML/DDL/grants.
It verifies its own service-principal OID and non-admin status, rejects elevated/data-wide or
runtime-role membership, then checks the runtime role's exact OID/privileges/PostGIS facts.
Unknown role flags do not imply non-admin. Freshness starts at the earliest observation,
not at signing: an overlong verification cannot issue a newly dated stale report.

The GitHub exchange has a 15-minute caller budget and a 10-minute verifier runner budget.
One digest-bound request per run/attempt/job/operation admits one dispatch. Lost responses
are reconciled by exact run identity; no blind redispatch occurs. Duplicate/ambiguous runs,
inactive consumers, failed jobs, missing/oversized artifacts, bad signatures or expired facts
block. Artifacts contain only signed nonsecret facts and expire after one day. Private key
files exist only in V's private temporary directory and are removed on exit; only the JWT
is uploaded. D validates the signature and fresh ARM target after download. Approval that
outlasts the caller budget fails closed and requires a new consumer job attempt.

### Separate verifier enrollment (not performed by this implementation)

Configure nonsecret `AZURE_NONPROD_DATABASE_VERIFY_CLIENT_ID` and
`AZURE_NONPROD_DATABASE_VERIFY_PRINCIPAL_ID` consistently in trusted workflow configuration.
Protect `nonprod-database` with main-only access and explicit signed verifier authorization.
As a read-only attester it does not add a mandatory human approval for every request;
any environment reviewers configured by the owner are still honored, including the timeout.
Mutating deployment/recovery approval requirements remain unchanged. Enroll V's OIDC trust
for that environment only. Store `NONPROD_DATABASE_VERIFY_AUTHORIZATION` and
`NONPROD_DATABASE_EVIDENCE_PRIVATE_KEY_PEM` only in that protected environment; set its
`NONPROD_DATABASE_EVIDENCE_KEY_ID`. Public policy entries require `identity_kind:
database_verification`, the exact V principal and a public key whose underlying key material
differs from every release or other attester key. The policy remains disabled and empty here.

V needs app-RG Reader, exact state-account Reader and read-only blob access at the state
container. No state-RG Reader is required. Its effective actions must be read-only and its
only permitted storage data action is blob read; a role adding user-delegation-key generation
or writes is rejected. Use a separately reviewed minimal data-reader custom role if needed.
V needs no application storage, Service Bus, Key Vault secret, Graph or Terraform write access.
Strict bootstrap RBAC additionally accepts exactly V's configured built-in Reader grant
at the application RG. It verifies the principal, scope and
[Reader role ID](https://learn.microsoft.com/en-us/azure/role-based-access-control/built-in-roles/general#reader);
the five original D grants and their delegation condition remain mandatory. Unknown grants
are still rejected. Rotation and secret seeding share the release/compensation concurrency
group so their shared-vault writes cannot overlap cleanup.

After separately approved SQL enrollment by A, V's exact Entra role must be
`tirodhan-np-<suffix>-database-verifier`: non-admin service principal, CONNECT on `postgres`
and `tirodhan`, and permission to execute the documented
[`pgaadauth_list_principals(boolean)` metadata function](https://learn.microsoft.com/en-us/azure/postgresql/security/security-manage-entra-users).
It receives no runtime-role membership, schema CREATE or application data grants. Restricted-
identity Azure acceptance must verify the metadata function is available with those rights;
if the managed provider refuses it, verification remains blocked. There is no fallback that
grants `azure_pg_admin` or copies A/D credentials into the job. These enrollment operations
are not executed by the verification workflow or by implementing/merging this PR.

Secret versions must be enabled/unexpired. Runtime material is validated in memory by actual
RSA/AES/customer/cursor consumers; developer `.env` or ambient secrets cannot supply missing
proposed values. Private GHCR artifact access and the exact successful migration Job/image/
execution gate runtime. Requested FCM/Web Push/native routing activations with unresolved
external/native/client gates remain explicitly blocked. This infrastructure contract does
not declare all product/business acceptance gates complete.

## Compensation and teardown

A conditionally created baseline records real clean state/inventory, original approved plan
hash and exact create addresses. Partial/reused state never qualifies. Automatic compensation
requires separately signed prepare/compensate operation authority, original plan hash,
clean-start fingerprint, `compensation_authorized` and
`automatic_owned_initial_apply_compensation`. Only currently owned IDs at the original create
addresses and a destroy-only plan qualify. Otherwise cleanup requires a separately authorized
reviewed plan/hash/current-state binding. Initial apply approval alone never implies cleanup.
Original deployment failure remains failure even if compensation succeeds.
Automatic cleanup is limited to the trusted foundation workflow, whose `nonprod-release`
concurrency group spans both apply and cleanup. A native Terraform lease alone cannot
serialize the interval between those operations. Local or independent attempts retain
their partial state for separately reviewed cleanup. A pre-existing baseline blob never
gets overwritten or implicitly authorizes another apply; after a crash, inspect actual
state and obtain fresh resume/cleanup authorization.

ACA recovery retains current main's exact original-plan-derived target, bounded retries and
direct resource-specific absence confirmation. Only the recognized single polling failure
can enter terminal reconciliation, which requires explicit authority and a zero-delete plan.
Final verification requires zero managed/disposable resources, the exact preserved backend,
private container and strict bootstrap RBAC. A retained vault is exempted only by exact ID
and freshly verified tenant/location/security. Every foreign vault remains blocking.
The protected plan job records whether the exact vault existed before destruction. A
required vault must be verified even when the list inventory is empty. The legacy run 8
case is separately admitted only from actual initialized, zero-managed, zero-application
state with verified vault absence; a missing job expectation never defaults to that case.

[Destroy run 8](https://github.com/krishnadhoundiyal/tirodhan/actions/runs/41215047550) is the
regression baseline: zero deletes, empty state and preserved backend/RBAC. It did not exercise
active ACA recovery; fault tests cover that state machine. Snapshots/baselines are retained
pending approved retention, never silently purged.

## Validation limits

Local evidence: the focused controller/compensation/inventory/architecture/database suite
has 91 passes; strict mypy passes for 280 source modules. The full Linux Python 3.13 unit
run before the inventory addition had 855 passes, one skipped pinned-Terraform fixture
and one failure in the untouched OTP traceback test. That exact failure also reproduces
on baseline main in an isolated checkout; the repository targets Python 3.12 in CI.
It is reported, not suppressed or converted to success. Final PR checks remain required.
Pinned Terraform 1.13.5/AzureRM 5.8.0 backend-disabled init/validate and Terraform formatting
passed locally, with no Azure authentication; the native forget-plan fixture also passed.

Tests use no Azure credentials or Azure connectivity. They exercise controller checks,
forged/stale/missing evidence, mandatory omissions, scope/identity errors, real teardown shell
branches, backup readback and synchronized competing saved-plan writers. A separate pinned
Terraform plan/show fixture confirms exact non-destructive forgetting and unchanged input
state bytes; it performs no apply. Full CI, strict typing/lint and Terraform offline validation
are required. CodeRabbit review is required before proposing merge.

Separate authorization is still needed for actual identity enrollment, restricted-identity
acceptance, recovery/transfer and cloud deployment/teardown. SKU advertisement does not reserve
capacity. Point-in-time evidence does not turn Azure into a distributed atomic transaction;
unknown outcomes stay blocked and are reconciled under explicit authority.
