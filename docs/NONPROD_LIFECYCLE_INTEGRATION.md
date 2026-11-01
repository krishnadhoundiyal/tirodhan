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
have no implicit authorization. Applies can require a new binding after plan review.
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

A verifies actual SQL principal OID/type, non-admin/elevated-role status, PostGIS, CONNECT,
schema USAGE/CREATE and spatial-reference SELECT before signing evidence with a separately
pinned A key. Missing signer configuration blocks before grants. `bootstrap_database
--verify-only` refreshes evidence without repeating grants. Evidence contains no Azure token
or SQL data and is bound to current server/UAMI/configuration/source and configured expiry.
D compares it against fresh ARM identities. Missing evidence cannot become PASS.

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

ACA recovery retains current main's exact original-plan-derived target, bounded retries and
direct resource-specific absence confirmation. Only the recognized single polling failure
can enter terminal reconciliation, which requires explicit authority and a zero-delete plan.
Final verification requires zero managed/disposable resources, the exact preserved backend,
private container and strict bootstrap RBAC. A retained vault is exempted only by exact ID
and freshly verified tenant/location/security. Every foreign vault remains blocking.

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
