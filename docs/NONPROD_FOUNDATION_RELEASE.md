# Authorized NONPROD infrastructure cycle — 5 November 2026

The sole authorized owner explicitly approved lifting the NONPROD deployment hold
to begin the infrastructure-provisioning cycle, retaining approval of the exact
saved Terraform plan before apply. The target is successfully provisioned and verified
Azure resources. Bootstrap and planning are intermediate stages.

`deploy/nonprod-release-policy.json` now has `release_enabled=true`. This enables
signature validation for authorized operations; it does not authorize a main push,
unsigned command, resource apply or application activation by itself.

## Execution scope and order

1. Review and merge the release-policy change through protected main, successful final-head
   repository/lifecycle CI and CodeRabbit review. Capture requires identical reviewed-main provenance.
2. Capture and sign the existing `bootstrap` purpose on the trusted operator workstation,
   with actual O/D identities, approved target, fresh source/configuration and explicit expiry.
   Preserve custody: hidden passphrase, exact digest confirmation, private local storage.
3. Run the existing signed `deploy/bootstrap/bootstrap.sh` as O. Reconcile the existing
   backend/vault and restricted grants; enroll D's approved two-action metadata discovery
   role. Preserve F02/F03, backend lineage/serial, vault protection and account-scoped access.
4. Verify actual bootstrap postconditions, then capture/sign the existing planning purpose
   for D and use the existing `nonprod-foundation.yml` automation to produce the saved plan.
   The existing planning profile contains setup and foundation/migration/runtime plan operations;
   it includes no apply or migration-start operation. This cycle dispatches foundation planning.
5. Present the actual saved plan, resource changes, plan hash and live backend fingerprint
   for owner approval. **Do not issue apply authority, approve a protected apply job or run
   Terraform apply before that approval.** Retain main-only execution, sole-owner manual
   approval, disabled administrator bypass, signed operation bindings and all prerequisites.
6. After exact-plan approval, the existing signed apply/verification automation creates the
   approved resources and verifies actual Azure resources and authoritative Terraform state.
   SQL/bootstrap, verifier, logging and workload evidence must be evaluated at their applicable stages.

Application activation, new providers/features, billing enablement and broader identity
permissions are outside this authorization. No identity/signing separation, workflow,
Terraform ownership, security or cost architecture is changed.

## Failure and recovery behavior

Missing/forged/stale authorization and unknown or missing mandatory prerequisites fail
closed, including when the release policy is enabled. A failure remains failure; record
actual postconditions before retrying. Existing deterministic bootstrap grants, native
Terraform locks, reviewed plan/state binding and recovery protocols remain authoritative.
No force unlock, raw state editing, replacement credentials or automatic apply approval.

Main pushes still require valid current-source signed authority before Azure login. Renew
short-lived planning credentials only for the reviewed current commit and approved context;
never reuse a token after a source/configuration change. A failed admission is expected when
that authority is absent. Teardown remains independently available through its existing gates.

## Saved-plan stage representation

The first authorized foundation planning run, `42019881023`, completed successfully
on reviewed main `de7f0d6d121f3b43d8c5d145918dae5bff6226a1`. Inspection found that
Terraform 1.13.5 retains `TF_VAR_deployment_stage=0` as string `"0"` in saved-plan
input metadata, while its evaluated number output is `0`. A direct integer-only
comparison would reject that plan before apply.

Saved-plan admission accepts the exact integer or canonical decimal-string stage
required by the operation. Wrong stages, booleans, floats and alternative numeric
spellings fail closed. Signature, saved-plan hash, current backend fingerprint,
lineage/serial, images, scope and destructive-action checks remain mandatory.
Native local Terraform regression verification plans synthetic resources without
Azure access or apply and verifies that the local state is unchanged.

The correction changes the signed source contract. After protected review and merge,
capture fresh planning authority and generate a new plan; the earlier run is evidence
of the defect, not approval to apply its superseded artifact. Exact-plan owner approval
and the protected manual gate remain required. No apply authority was issued for the
first plan.

## PostgreSQL initial-placement retry

Owner-approved apply run `42026962491`, attempt 2, passed signed admission and
created 27 of the 32 stage-zero instances. PostgreSQL creation returned
`ResourceOperationFailure` / `SkuNotAvailable` (correlation
`d3889777-de1f-b541-379b-db0d7b7dee9a`). Its server is absent from live Azure and
state; the five PostgreSQL instances remain missing. The preserved state lineage
advanced through native Terraform to serial 43. The other 27 instances and their
identity/scope bindings were independently verified; they must be preserved.

Microsoft's response suggests another zone, SKU or region. Its example
`az postgres flexible-server update` requires an existing server and is not an
execution path for this absent, Terraform-owned target. PR #110 selected
initial zone `2` for a bounded creation retry in **Central India**, retaining
**B_Standard_B1ms, PostgreSQL 17, 32 GiB, seven-day backup, no HA and no geo backup**.
The existing placement-drift lifecycle remains: the preference affects initial
creation and does not force existing servers to move. No SKU/region fallback,
new workflow, identity, grant, GitHub variable or application activation is added.

Both capability API generations and the actual CLI catalog advertised B1ms in
zones 1/2/3 after the allocation failure. Admission now requires the selected
zone on the exact SKU record; another SKU's zones, missing/malformed zone support,
restrictions and disabled/unknown status cannot satisfy that check. This is
advertised support, **not a capacity reservation or successful-allocation proof**.
Microsoft has not confirmed actual zone-2 capacity. A further provider failure
remains failure; never rotate through zones/SKUs or destroy partial state automatically.

After required source review/CI, capture fresh authority for the reviewed main and
generate an incremental plan from actual partial state. Verify that the 27 committed
instances remain unchanged and that only the missing PostgreSQL instances are created.
Obtain owner approval of the exact new plan before signing/enrolling apply authority
and entering the existing human gate. The former 32-create artifact and its retired
apply token cannot be reused against serial 43. Full native apply and exact live
foundation verification remain required before reporting foundation readiness.

Reference: [Microsoft's PostgreSQL capacity guidance](https://learn.microsoft.com/en-us/azure/postgresql/troubleshoot/how-to-resolve-capacity-errors).

### Owner-requested zone-1 retry

Zone-2 run `42043233166`, attempt 1, passed all 13 apply prerequisites but Azure
again returned `ResourceOperationFailure` / `SkuNotAvailable` (correlation
`004ae5ad-c378-d0c6-3e1f-0b0c9cd133c9`). Azure removed the failed server;
an exact ARM read confirmed `ResourceNotFound`. All 27 original instances,
identity/scope bindings, vault protections and backend safeguards were verified
preserved. Native Terraform advanced the existing lineage to serial 44. The
failed-run apply token was retired. No application was activated.

The owner explicitly requested a further initial-creation attempt in zone `1`.
That was the committed placement preference. The exact-SKU zone/status
admission, cost settings and existing placement lifecycle remain enforced.
This is one owner-selected attempt, not automatic zone cycling. The catalog
advertises zone 1, but actual capacity remains unproven. The workflow/jobs,
SKU, region, authentication, storage, backups and identity permissions are unchanged.

Use the existing protected source-review and signed planning/apply cycle.
Reconcile actual serial-44 state first, preserve all 27 existing instance IDs
and require a fresh incremental plan with only the five PostgreSQL creates.
Fresh source/state-bound authority and exact-plan owner approval remain mandatory.
The zone-2 plan and token cannot be reused. A further allocation failure must
remain failure and be reconciled without an automatic fallback or cleanup.

### Owner-requested zone-3 retry

The 7 November zone-1 apply in run `42344775597` passed all 13 prerequisites.
Azure returned `SkuNotAvailable` during PostgreSQL creation (correlation
`cbe5d607-3845-612b-4739-d748b956811c`). The server was absent after the
failure, the 27 existing instances were verified, and the apply token was retired.
Native Terraform advanced the preserved state lineage to serial 46.

The owner selected zone `3` for one B1ms creation attempt in Central India.
Azure lists the exact SKU in that zone and accepted a validate-only request.
Allocation remains unproven. Use the existing protected review and signed
planning and apply cycle. Reconcile current state and require a fresh plan with
only the five missing PostgreSQL instances. Preserve all existing instances.
Exact-plan approval and the protected GitHub approval are mandatory.

### Approved PostgreSQL-only placement in India South Central

The zone-3 attempt in run `42351121930` failed at Azure allocation with
`SkuNotAvailable` (correlation `b0a784d7-b05d-c4d9-1039-c6a25247ad8c`).
Azure removed the failed server. All 27 existing resource IDs were verified
and the backend preserved its lineage at serial 47.

The owner approved India South Central zone 3 for the five PostgreSQL
resources, including cross-region database traffic and its transfer charges.
Keep B1ms, PostgreSQL 17, 32 GiB Premium SSD, Entra-only authentication,
seven-day backups, and disabled HA and geo backup. The database uses the
new `-pg-isc` name in the same resource group and backend. The rest of the
foundation stays in Central India. The SKU gate reads the database location
from committed Terraform and checks the selected SKU and zone there.

Require a fresh foundation plan with only the five PostgreSQL creates and no
updates, deletes or replacements. Verify the 27 existing IDs before and after
apply. Signed authority and exact-plan human approval remain mandatory.
After apply, verify the actual server region, SKU and zone. Before application
activation, verify TLS and Entra connectivity and measure representative SQL
latency from Central India through the existing protected verification path.
