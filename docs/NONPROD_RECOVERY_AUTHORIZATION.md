# NONPROD enrollment and recovery authorization

Approved scope: Closure Batch 1, F03 enrollment and F02 exact-vault recovery. Source/tests/review
do not authorize external mutation, merge or dispatch. `release_enabled=false` is unchanged.
Log rotation, SQL verification, Terraform apply and runtime configuration remain outside this batch.

## Independent signed purposes

| Class | Audience | Operation | Executing identity |
|---|---|---|---|
| DEPLOYMENT | tirodhan-nonprod-provisioning | Existing provisioning profiles | Existing identities; release_enabled must be true |
| ENROLLMENT | tirodhan-nonprod-enrollment | enroll-recovery only | Approved operator on trusted local workstation |
| RECOVERY | tirodhan-nonprod-recovery | recover-vault only | Distinct R in protected main/nonprod-recovery reconciliation job |

All tokens require authorization_class, Ed25519 signature/pinned keys, issuer/audience,
expiry/nbf/iat, exact reviewed commit/source/configuration, repository, principal, session and
operation bindings. Mixed/unknown operations/classes/audiences fail. Every mandatory operation
prerequisite remains enforced; missing/unimplemented/unknown never passes. Changed source
hashes invalidate previous tokens. No temporary hold override is used to sign a credential.

Maintenance binds exact tenant/subscription/RG/location/suffix/vault, real recovery client/SP
and a stable logical operation ID. Enrollment additionally binds the real application OID,
verified operator ownership and enabled, single-tenant, password/certificate-free app/SP.
O/D/V/A cannot substitute for R. Identity creation remains a separately approved external
operation; this code expects actual existing IDs and does not create placeholder identities.

Recovery binds the observed deleted-vault ID, original vault ID/location, deletion date,
scheduled purge date, purge protection and retention interval. Its authority cannot outlive
the purge date or recover a later deletion generation. It grants no secret reads, state or
Terraform operations, vault creation/purge, permission repair or ownership transfer.

## Capture and issuance

After source review/explicit merge approval and approved actual identity/configuration enrollment:

```text
python -m tirodhan.deployment.release_capture enrollment --session <approved-uuid> --expires-at <approved-timezone-expiry> --review-pr <merged-PR> --output <private>/enrollment-request.json
python -m tirodhan.deployment.release_setup issue --request <private>/enrollment-request.json --reviewed-request-sha256 <capture-digest> --private-key <custody>/release.encrypted.pem --output <private>/enrollment.jwt
```

Capture is read-only and requires the exact final reviewed main source and existing CI/review
evidence. Existing encrypted custody/private-directory/interactive confirmation rules apply.
The operator-local enrollment JWT is never uploaded to GitHub. Supply it privately through
TIRODHAN_NONPROD_RELEASE_AUTHORIZATION for the separately approved local
`python -m tirodhan.deployment.foundation_reconcile enroll-recovery` operation. Every external
write still requires the owner's exact operation/target/permissions/effects approval.

After actual F03 identity, immutable federation, narrow RBAC, main-only protected recovery
environment and GitHub variables are verified, obtain separate exact recovery authority:

```text
python -m tirodhan.deployment.release_capture recovery --session <approved-uuid> --expires-at <approved-timezone-expiry> --review-pr <merged-PR> --output <private>/recovery-request.json
python -m tirodhan.deployment.release_setup issue --request <private>/recovery-request.json --reviewed-request-sha256 <capture-digest> --private-key <custody>/release.encrypted.pem --output <private>/recovery.jwt
```

Only separately approved secret enrollment may put the recovery JWT, through stdin, into the
existing repository NONPROD_RELEASE_AUTHORIZATION destination. Never overwrite a credential
without approval. This token cannot grant deployment. Workflow admission verifies RECOVERY
before Azure login; R operates only in protected nonprod-recovery. Privileged enroll-recovery
remains operator-local and is not added to the restricted D workflow.

## Faults, replay and resumption

Stable logical operation IDs exclude JWT jti/expiry and runner attempts. Explicit renewal
retains the same original target/session/context and cannot broaden actions.

Enrollment uses deterministic role/assignment IDs and unique immutable federation naming.
Exact existing objects are verified no-ops; conflicting roles/trust/grants block before
overwrite. Lost responses require exact readback. Fresh authorization/context and all live
prerequisites are checked before every write. Expiry/replacement leaves inspectable partial
enrollment; fresh authority safely converges without duplicate grants.

Recovery uses the existing nonprod-release concurrency group. Before a first start it checks
the exact trusted main run and complete bounded history since the signed vault deletion.
Prior runs (including earlier source revisions) or rerun attempts of the session are observation-only. Missing/ambiguous/incomplete
history blocks. Changing JWT jti/expiry or recapturing the session cannot reset that fence.

A first attempt sends at most one `az keyvault recover --no-wait`. Lost responses are observed,
never blindly reissued. Exact active/pending resources are polled to verified terminal success,
including ID/tenant/location/RBAC/purge/soft-delete/retention. CLI calls are capped at 30 seconds,
polling at 12 observations with no new poll started after its 300-second deadline, and the job
at 15 minutes. A final in-flight read can take up to its 30-second cap. Unknown/timeout fails.

A rerun can observe accepted recovery to completion. Crash before submission or unprovable
acceptance stays BLOCKED for that session. Operator investigation and explicitly approved
fresh authority/session for the still-verified deleted generation are needed before another
start. No automatic takeover or retry follows missing evidence. Independent out-of-band Azure
administrators are outside workflow serialization; their changes require reevaluation.

For a separately approved renewal of the same operation:

```text
python -m tirodhan.deployment.release_capture recovery --resume-request <private>/recovery-request.json --session <same-session> --expires-at <approved-new-expiry> --review-pr <same-reviewed-PR> --output <private>/renewed-request.json
```

The original deleted generation/operation time are preserved. Review/sign the new capture
digest. Changed principal/configuration/source/session blocks rather than broadening authority.

## Actual closure evidence

F03 requires live identity/ownership, immutable OIDC, exact role definitions/assignments and
protected environment/variable readback. F02 requires actual successful exact-vault recovery,
active security/accessibility, unchanged backend ETag/lineage/serial and original D grants
plus exactly approved R/V extensions. Teardown admission and ACA recovery are unchanged.

R has no secret DataActions. Existing authorized observers inspect secret names/versions,
enabled/expiry metadata; preservation claims require a trusted prior inventory. Otherwise
preservation stays UNVERIFIED. Protected consumers verify crypto/provider validity without
logging values. Report PASS/MISSING/BLOCKED separately and keep unrelated findings open.

[Azure CLI recovery reference](https://learn.microsoft.com/en-us/cli/azure/keyvault#az-keyvault-recover)
documents the existing recover operation's no-wait option.
