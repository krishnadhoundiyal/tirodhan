# NONPROD operator key custody and authorization

These utilities complete the existing authorizer role. They add no workflow, Azure
resource, RBAC grant, alternate admission path or automatic enrollment. The committed
deployment policy remains held. Never issue credentials for a branch under review.
The separately approved [ENROLLMENT/RECOVERY purposes](NONPROD_RECOVERY_AUTHORIZATION.md)
retain exact-operation prerequisites and do not grant deployment authority.

## Trust boundaries

`release_capture` runs on an approved operator workstation with authenticated `gh` and
Azure CLI. It reads evidence without loading signing keys, changing settings or dispatching
workflows. `release_setup issue` works offline with encrypted local custody and verifies
generated JWTs through the existing validator. The deployment hold blocks DEPLOYMENT issuance
before unlock; only explicitly signed ENROLLMENT/RECOVERY purposes are independent of that flag.

GitHub authenticates REST/GraphQL reads but does not sign exported JSON. Offline signing
therefore relies on the existing trusted authorizer, controlled capture host, protected
transfer and separately recorded request digest. The bundle is not an independent GitHub
cryptographic attestation. Check its digest against the trusted collector's record; a file
and digest from the same untrusted source are insufficient. No new attestation authority is
introduced. A compromised collector or unlocked signing workstation remains a material risk.

Capture requires current main, clean committed source, a merged PR with an identical final
Git tree, successful final-head repository and lifecycle CI, an actual CodeRabbit bot review
and no unresolved threads. Merge/squash commits require full-tree equality with the reviewed
head. Changed trees need another reviewed PR. Arbitrary PASS flags, review URLs and wildcard
operations do not substitute for these records. Incomplete bounded histories block.

Metadata retains the existing 1 MiB response limit and one bounded GitHub collection budget.
Artifact binaries use a separate 40 MiB streaming limit; a native plan entry remains limited
to 32 MiB. Redirects require HTTPS and never forward the API Authorization header to another
origin. Existing CLI authentication is read internally, never printed. Oversized metadata,
artifacts or exhausted collection time block with controlled errors, never readiness.

Preparation conservatively uses the existing `maximum_evidence_age_seconds` for capture
freshness. Long reviews require fresh capture/reconfirmation. The operator explicitly chooses
expiry. Live deployment prerequisites still run after environment approval and before effects.

Tokens remain reusable within their exact authorized session, as required by existing
multi-command jobs. Cross-session/source/configuration replay fails. The exclusive local
issuance journal blocks duplicate/concurrent signing of one reviewed request, but is not a
distributed token-consumption ledger. Native Terraform locks/state fencing and migration
effect reconciliation remain authoritative. Deferred issues #98/#99 are not solved here.

## Encrypted custody and public enrollment

Use an approved dedicated operator account/workstation, encrypted disk and protected backups.
Never use an application container, Azure application resource or shared build runner.
The tool rejects runner markers and Git worktrees. Passphrases use hidden interactive prompts,
never arguments, environment variables, files or transcripts. Keep passphrase recovery in a
protected password manager separate from the encrypted backup. Python cannot guarantee
zeroization of every in-memory copy.

The approved compatible fallback is encrypted PKCS#8 Ed25519 with secure random generation
and cryptography's `BestAvailableEncryption`. No compatible non-exportable signer is enrolled
here; the existing SQL verifier consumes PEM. No HSM/service or hardware-isolation claim is
introduced. Such an integration would require separate approval.

```text
python -m tirodhan.deployment.release_setup keygen --directory <absolute-private-directory-outside-git> --verifier-principal <exact-V-object-id>
python -m tirodhan.deployment.release_setup policy-proposal --enrollment <private-directory>/public-enrollment.json --output <private-directory>/policy-proposal.json
```

New directories use owner-only POSIX modes (`0700`, files `0600`), or a verified protected
Windows DACL allowing the current owner and SYSTEM. Existing directories are not silently
re-permissioned. Repeated keygen validates/reuses a complete matching set; partial sets,
different principals or wrong passphrases fail without overwriting.

Outputs: independent `release.encrypted.pem`, `database.encrypted.pem` and public-only
`public-enrollment.json`, with fingerprint-derived IDs and encrypted-key readback validation.
Never share private files/passphrases in chat. The public-policy proposal remains held and
does not modify the repository. Matching entries are retained; conflicting IDs fail. SQL keys
must differ from release and other attester keys.

After separate approval, enroll only public entries through the existing reviewed policy PR.
Pin V's exact principal and `identity_kind: database_verification`. Release enablement requires
its own approval and final CI/review. No temporary hold override exists in the signer.

Initial Entra-admin SQL bootstrap retains its separate `database` attester boundary. V's key
must not impersonate that administrator. A new database still needs its existing separately
approved administrator signer/enrollment. Generating two keys does not prove SQL readiness.

## Inventory and readiness

CodeRabbit may complete a no-comment review using its PR conversation summary instead of
a formal `/reviews` submission. Capture supports that explicit format only when both the
authenticated CodeRabbit bot ID and its own GitHub App check bind the exact reviewed head.
The summary must contain one matching assessment-commit marker and the completed
no-actionable-comments block; the App check must be completed/success. Missing, pending,
failed, ambiguous, wrong-PR/head/provider or unresolved evidence blocks. A formal
CHANGES_REQUESTED review cannot fall back to a clean summary. Both required CI workflows,
merged-main tree provenance and all authorization/hold checks remain mandatory. Collected
comment excerpts retain a full-body digest and omit scoped share URLs; they are not invented
formal review submissions or caller PASS flags.

```text
python -m tirodhan.deployment.release_capture inventory
python -m tirodhan.deployment.release_setup readiness
```

Inventory reports names/presence and the nonsecret V key-ID variable. Denied reads remain
UNKNOWN. GitHub secret values cannot be read back: PRESENT does not mean valid. Never replace
an existing entry merely because its value is unreadable. Validate available local material
against pinned keys/context/expiry; uncertain enrollment needs investigation and approved
replacement. Readiness never emits `PREREQUISITES_SATISFIED` or grants provisioning authority.

Google project number 742429799693 and India billing were owner-supplied. Actual eligibility,
usage, quota controls and credentials remain unverified. Keep Google Geocoding under the
approved no-cost condition and explicit 2Factor selection. Kaleyra is not required for 2Factor.

## Capture, review and offline issue

Use the existing authoritative configuration environment from the deployment runbook.
Capture derives the exact SP object ID from its configured application through authenticated
Entra reads and checks the tenant. Client/application IDs and principal object IDs differ.
Signing also checks V against configuration and its independently pinned evidence key.

Choose a UUID session and explicit timezone-aware expiry; use the same session for its
planning, reviewed applies and V authorization. Substitute approved actual values:

```text
python -m tirodhan.deployment.release_capture release --session <session-uuid> --expires-at <ISO-8601-with-timezone> --review-pr <final-merged-PR> --output <private-directory>/release-request.json
python -m tirodhan.deployment.release_capture database --session <same-session> --expires-at <ISO-8601-with-timezone> --review-pr <final-merged-PR> --database-key-id <pinned-V-key-id> --output <private-directory>/database-request.json
python -m tirodhan.deployment.release_setup issue --request <private-directory>/release-request.json --reviewed-request-sha256 <trusted-capture-digest> --private-key <custody>/release.encrypted.pem --output <private-directory>/release.jwt
```

Record the emitted digest through the trusted operator channel; transfer only the protected
unsigned bundle. It contains no JWT or signing key. Apply bundles include private Terraform
state and must remain protected. Signing displays the nonsecret context, requires exact digest
confirmation, then prompts for the custody passphrase. It computes all claims and bindings,
verifies every operation through the existing validator, and writes the credential privately.
Only hashes/IDs enter the journal. Use `database-request.json` for V's authorization.

Apply authority is issued later, after the protected workflow produces its saved plan.
Download the exact artifact through authenticated GitHub tooling into private storage and
review it using pinned Terraform 1.13.5. Capture checks native bytes against the active trusted
main run and successful plan job, and reads current authoritative backend state using
the operator's existing approved read access, recording that observer's actual OID.
This observer need not be D and receives no new grant. Entra lookup requires the
operator's existing access; no Graph permission is added to D. Live deployment identity
and account-scoped permission checks still run under D, never state-RG Reader.
Native lineage/serial must match;
ETag/full state produce the existing fingerprint. Missing outputs/empty inventory are not
readiness evidence. Exact plan-digest confirmation is mandatory.

```text
python -m tirodhan.deployment.release_capture apply --session <same-session> --expires-at <ISO-8601-with-timezone> --review-pr <final-merged-PR> --apply-operation migration-apply --artifact-id <actual-artifact-id> --plan <private-directory>/migration.tfplan --output <private-directory>/apply-request.json
python -m tirodhan.deployment.release_setup issue --request <private-directory>/apply-request.json --reviewed-request-sha256 <trusted-capture-digest> --private-key <custody>/release.encrypted.pem --plan <private-directory>/migration.tfplan --output <private-directory>/apply.jwt
python -m tirodhan.deployment.release_setup verify --request <private-directory>/apply-request.json --token-file <private-directory>/apply.jwt --plan <private-directory>/migration.tfplan
```

Repeat with a newly reviewed runtime/foundation plan for `runtime-apply`/`foundation-apply`.
Migration apply authority cannot authorize runtime. Bindings include exact plan SHA-256,
state fingerprint, lineage, serial and ETag. The existing workflow independently enforces
its actual release session and rechecks remote state/native plan and all prerequisites.
Offline signing cannot prove remote state has not changed after capture; live fences remain.

## Explicit secret enrollment — separate approval required

The existing capture/issue commands also support these exact profiles. All remain
DEPLOYMENT-class and blocked before signing-key unlock while release_enabled=false:

| Capture kind | Existing operation | Identity / destination |
|---|---|---|
| bootstrap | bootstrap with explicit enrollment/discovery bindings | O / operator-local |
| database-bootstrap | database-bootstrap with independently enrolled A evidence key | Configured A / operator-local |
| seed-local | seed-secrets, exact --secret-name batch | D / operator-local |
| seed-workflow | seed-secrets, ghcr-pull-pat only | D / repository NONPROD_RELEASE_AUTHORIZATION |
| log-local | rotate-log-sas | D / operator-local |
| log-manual | rotate-log-sas, protected nonprod-plan | D / repository NONPROD_RELEASE_AUTHORIZATION |
| log-scheduled | rotate-log-sas plus the exact committed workflow cron | D / repository NONPROD_LOG_SAS_AUTHORIZATION |

Use the same explicit session, expiry, reviewed-main provenance and private custody flow.
Supply --database-key-id for database-bootstrap; V cannot substitute for A. Capture
reads the actual operator OID/tenant for O/A and verifies deployment-app ownership for O.
Only the committed rotation cron can be issued for recurring authority. Seed requests
contain sorted unique approved names, never values; effect admission requires an exact
name match. Interactive signing displays the operation bindings for digest review.
These profiles neither authorize held enrollment nor execute any external change.

No setup utility automatically writes settings. After inventory and separate approval,
use GitHub CLI's supported stdin mechanism in a private terminal. Never use a token in
`--body`, shell tracing, HTTP debug logging, chat/clipboard or public CI artifacts. Do not
overwrite an existing entry without approved renewal/rotation. Serialize enrollment under
one designated operator: GitHub secret PUT is not an atomic create-only API, so inventory
alone cannot prevent a second operator racing an overwrite.

These are **mutating commands**, for separately approved execution only (Bash):

```bash
set +x
set -euo pipefail
unset GH_DEBUG
trap 'unset sql_pem' EXIT
gh secret set NONPROD_RELEASE_AUTHORIZATION --repo krishnadhoundiyal/tirodhan < "$PRIVATE/release.jwt"
gh secret set NONPROD_APPLY_AUTHORIZATION --repo krishnadhoundiyal/tirodhan --env nonprod < "$PRIVATE/apply.jwt"
gh secret set NONPROD_DATABASE_VERIFY_AUTHORIZATION --repo krishnadhoundiyal/tirodhan --env nonprod-database < "$PRIVATE/database.jwt"
if ! sql_pem=$(openssl pkey -in "$CUSTODY/database.encrypted.pem"); then exit 1; fi
if [ -z "$sql_pem" ]; then exit 1; fi
printf '%s\n' "$sql_pem" | gh secret set NONPROD_DATABASE_EVIDENCE_PRIVATE_KEY_PEM --repo krishnadhoundiyal/tirodhan --env nonprod-database
unset sql_pem
gh variable set NONPROD_DATABASE_EVIDENCE_KEY_ID --repo krishnadhoundiyal/tirodhan --env nonprod-database --body "$PINNED_V_KEY_ID"
```

Use trusted local OpenSSL supporting Ed25519. Check decryption success before starting the
secret write: a direct OpenSSL-to-GitHub pipeline could start an empty write after decryption
failed. SQL PEM is briefly held in shell memory and fed through stdin to the protected
environment, never a plaintext file or deployment job. Keep shell tracing disabled and
clear the variable afterward, including on failure; the EXIT trap handles aborted commands.
Release private material is never uploaded. In PowerShell pipe `Get-Content -Raw -LiteralPath
<private-token-file>` directly into the matching `gh secret set`; never evaluate it alone.
Preserve environment/OIDC/SQL permission boundaries. V's PEM belongs only in
`nonprod-database`; offline release admission requires the repository release secret.
No Azure client password, SQL administrator password or storage key is used.

Retain nonsecret receipts and verify metadata after approved enrollment. Actual validity needs
the existing protected admission/fresh SQL verification, when separately authorized. These
tools dispatch no verification job; secret presence/local self-tests cannot replace SQL evidence.

## Rotation, revocation and recovery

1. Generate replacement custody in a new private directory. Never overwrite old keys.
   Back up encrypted keys, journals and public metadata under the same access controls,
   with passphrase recovery separate. Test decryption/public matching without printing secrets.
2. Prepare/review a held public proposal adding new keys. After separate approval enroll V's
   PEM and key-ID variable together. Exact principal, role and public key must match. Retain
   old public keys only for explicitly approved overlap, and obtain fresh V evidence.
3. Re-enable release only with approval of the final reviewed tree, then capture and sign fresh
   exact contexts. Apply credentials are renewed per reviewed plan; expired tokens are not revived.
4. On compromise, hold release through the existing policy process and revoke the key ID.
   `policy-proposal --revoke <key-id> --output <private-file>` prepares the public change only.
   Separately remove/replace affected GitHub secrets. New policy rejects old signatures and
   changed contract hashes. An already admitted in-flight process cannot be stopped retroactively
   by editing a future checkout: coordinate approved cancellation/containment and inspect effects.
5. Failed issuance retains its exclusive claim. Reconcile any protected token file's digest
   with the journal. Do not delete claims to force reissue; uncertain outcomes need investigation
   and a newly approved request/session. The journal is local, not shared across restored copies:
   maintain one active authorizer and restore backups through its designated custodian.

Nothing here runs recovery, mutates Terraform state, changes Azure RBAC, disables native
locking, merges a PR or authorizes a deployment as a setup side effect.
