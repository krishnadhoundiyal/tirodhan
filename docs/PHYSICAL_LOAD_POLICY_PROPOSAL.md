# Physical load for Customer Mobile: proposed V1 policy

CM-P0-03 now approves configured customer item-weight bands as the mass source and
implements version-bound nonlinear pricing. The standard mass-envelope recommendation
below is historical and remains unimplemented. Independent physical volume is still an
open decision and blocks new weight-priced customer holds; no density or volume default
has been approved. See [the current contract](CM_P0_03_WEIGHT_PRICING.md).

Assessment: 22 October 2026, CM-P0-02. **Recommendation for product-owner approval;
not an approved business policy or implemented load-class feature.** No mass/volume
values, densities, customer size questions or extra charges are introduced here.

## Existing authoritative facts

| Evidence | Current behavior |
| --- | --- |
| `customer_reads/models.py: CatalogueCategory` and `business_admin/commands.py: CategoryEdit` | Presentation, handling hints and optional quantity/weight input; no physical volume, density or conservative mass/volume profile |
| `collection_requests/ports.py: DeclaredRequestItem` and request item model | Optional quantity and declared total weight in grams per line; no volume or dimensions. Binding sums supplied line weights, without multiplying by quantity |
| `routing_operations/admission.py: ReserveCapacity` / `slot_holds.py: HoldSlot` | Both mass and volume must be positive integers, currently supplied by the caller. No source/estimation policy is established by field validation |
| `slot_holds.py` | Applies regional `safety_percent` independently to both inputs and rounds up. Stores those reserved values, standard pickup/discharge times, destination and deadline in CapacityReservation |
| `PROJECTED_SLOT_ALLOWANCES.md`, ADR-004/006 and allowance estimator | Commercial allowance is estimated time/workload, not physical mass/volume feasibility. The published standard service class is service duration only |
| `admission.py: bind_reservation` | Known declared line weights cannot exceed reserved mass. Reservation values copy to one immutable RoutingManifest in the booking transaction |
| `routing_operations/models.py`, `commands.py` and `business_admin/commands.py: VehicleEdit` | Managers explicitly configure each vehicle's positive mass/volume capacity; rider `capacity_class_code` is only an optional label, not a calibrated numeric mapping |
| `V2_ROUTING_INCREMENT_3.md` | Explicitly forbids treating money, catalogue names, optional weights, class labels or group count as manifest measurements |
| `V2_ROUTING_INCREMENT_7.md` and `operational_commands.py: ObservePhysicalLoad` | Existing manager-authorized positive mass/volume observations reference evidence and append history; original manifest remains intact |
| `physical.py: effective_manifest`, `cargo_load`, `unresolved_load`; `commands.py: ClearVehicle` | Latest observation supplies effective operational load; LOAD_UNCERTAIN persists until sufficiently new correction. Clearing also checks actual cargo against both vehicle limits and does not implicitly resume routes |

The existing regional safety factor is a margin on an input, **not a source for an
unknown input**. Historical documentation describes initial engineering estimates;
it does not approve numerical customer load profiles. Neither catalogue configuration,
the flat pickup fee, a rider class label nor the time-only standard service class supplies
both required checkout quantities. No approved customer physical-load class or density
conversion was found in the relevant models, configuration commands or decisions.

## Recommended V1 decision

Approve **one centrally managed standard household collection envelope**, limited to a
clearly described eligible collection. It represents conservative planning reservations,
not claimed measurements of a particular household. Operations must first measure representative
collections and determine supportable mass and occupied transport volume independently.
Product must approve the customer-visible eligibility description, exclusion/escalation
path and the resulting two values before implementation or live submission.

This keeps one published class and avoids category conversion tables or technical customer
measurements. It is viable only if a single conservative envelope can cover the approved
V1 scope without unacceptably restricting operational vehicle utilization. If measurements
show that cannot work, choose the explicit size-band alternative below before activation.
Do not hide a second envelope or accept arbitrary loads under a flat default.

### Origin, approval and versioning

Operations supplies measured mass and occupied transport volume, packaging/handling basis,
representative scope and observed variation. Product and Operations approve a conservative
pair, eligibility/exclusions, effective date and review criteria. No ratio from mass to
volume is assumed. The proposal does not nominate numbers or a reliability percentage.

Use a conventional relational load-profile master with immutable versions and manager
actor/command/reason attribution, following the existing business administration pattern.
Select the active profile per operating region through explicit versioned configuration;
retain the original class/version, input pair and reservation result for every hold.
Missing, inactive or inapplicable configuration must fail closed for new holds.

**Recommended margin interpretation for approval:** profile mass/volume are pre-margin
planning inputs, and the existing regional safety percentage is applied exactly once.
Persist the applied percentage as hold provenance as well as the final reserved pair.
If operations instead calibrates already-inclusive envelope limits, that must be explicitly
approved and the margin composition specified before coding; never silently compound or
remove the current safety rule. Historical holds keep their original values/version.

### Customer information

No new numeric mass/volume entry is recommended. Show the approved eligibility guidance
and a simple confirmation that the collection fits it, plus an Operations/support path for
uncertainty or oversized work. This is a proposed product interaction requiring approval.
Existing optional weight declarations remain declarations, not verified physical truth.
A declaration above the approved envelope must be identified before selling the hold;
do not silently clip it or inflate a one-class reservation without an approved policy.

### Hold and manifest association

In the follow-up implementation, Customer Mobile sends the selected published profile
version/eligibility confirmation, and the backend resolves authoritative mass and volume.
It must not trust caller-chosen measurements for this standard customer class. Retain the
same projected-slot -> atomic hold -> booking bind -> pending payment -> authenticated
capture flow. Do not add synchronous road optimization or make physical load part of the
projected time allowance.

The hold atomically pins profile identity, input values, safety provenance and final
positive reserved grams/millilitres. Booking still binds that hold once and creates the
manifest from its stored values; propagate the profile FK/provenance to the manifest or
an immutable, real-FK provenance relation. A later manager profile update affects fresh
holds only. Exact old command replay must retain its original effective profile and values,
even if the current profile is retired. An explicit new client fingerprint field/version
must preserve historical fingerprints for older commands.

### Underestimation, oversize and observed-load reconciliation

Before pickup, an identified out-of-envelope collection requires Operations review and a
current independently validated feasible operational continuation. After acceptance it
remains a fulfilment commitment; a low estimate cannot silently drop it, force a new payment
or cancel it. Do not allow collection/loading that would breach either real vehicle limit.
Use existing incidents, load uncertainty/vehicle holds and manager recovery for unsafe or
uncertain work. Specific customer messaging, reappointment consent, handling/refund decisions
and oversized exceptions require product approval; no detailed mismatch policy is inferred.

Use the existing evidence-backed `ObservePhysicalLoad` command for independently obtained
observed mass and volume. It appends `PhysicalLoadObservation` with actor/capture/time;
`effective_manifest` and cargo projection use the latest observation without editing the
reservation, booking items, quote, original manifest, pickup history or custody. Observations
are operational evidence, not self-validating scale/container measurements. Approve the
measurement SOP and evidence quality before relying on them.

For collected cargo, preserve actual custodian and completed work. An observation cannot
move material or clear a safety hold by itself. Existing correction freshness, both vehicle
capacity checks, validated recovery/route activation and evidenced transfer/handover still
apply. Media upload failure does not undo capture, pickup or custody facts. Compare observed
values against retained profile versions for later calibration; a revised profile is a new
version, not a rewrite or retroactive repricing of old bookings. No new analytics service is needed.

## Viable alternatives and tradeoffs

All options below require business approval and calibration. The same immutable hold/manifest
snapshot and existing evidenced correction/safety/custody rules above apply to each.

| Approach | Origin and approval/versioning | Customer change | Hold / manifest | Underestimate / oversize | Observation and history | Source / schema / manager changes |
| --- | --- | --- | --- | --- | --- | --- |
| **One standard envelope (recommended)** | Independently measured mass and occupied volume; one approved conservative pair per retained profile version | Eligibility guidance/confirmation; optional existing weights retained | Server pins explicit class/version, once-applied margin and positive pair; booking copies that reservation | Out-of-scope work goes to Operations; accepted work retains commitment and hard vehicle checks | Append existing physical observation; retain profile/reservation/manifest, revise only future profile versions | One concrete versioned profile master, regional selection, immutable provenance FKs, manager commands and customer profile contract |
| **Customer-facing size bands** | Operations measures each product-defined band independently; product approves tangible descriptions and each versioned pair | Customer selects a comprehensible illustrated band; no grams/ml required; unknown/oversize path needed | Pin selected band and version using the same server resolution and immutable snapshots | Misclassification enters existing review/recovery; no hidden load-limit bypass or automatic surcharge | Same append-only observation; compare by original band, never reclassify historical booking facts | Same profile model with several bands, manager calibration for each, catalogue-independent size-selection UI and API |
| **Category-based conservative profiles** | Operations measures mass and volume per countable unit for every supported category; version mappings and aggregation rule after explicit approval | Complete quantities/material/size distinctions must become required where the model needs them; current optional fields are insufficient | Server derives totals from complete declared items and pinned category profile versions; item body must be tied to hold fingerprint before binding | Unknown/mixed items and exceptional dimensions fail model eligibility; existing review/recovery for accepted mismatch | Append physical observations, retain per-line/profile derivation and original reservation; revise future versions only | Versioned category physical profiles and composition policy, complete declarations at hold, cross-command item matching, schema and manager/UI changes; highest V1 maintenance burden |
| **Operations-assessed collection** | Trained operator independently measures/assesses both values under an approved SOP; attributed, versioned assessment record | Customer needs review/appointment before self-service submission; no technical measurement entry | Hold pins the approved assessment and its values; manifest copies reservation | Existing safety/recovery, with explicit service envelope and manual oversize decision | Append observed corrections; retain original assessment/provenance and booking | Concrete assessment entity and authorization/idempotency, manager assessment workflow and mobile pending-review state; viable exception/pilot path but poor default scale |

Direct technical customer measurement would require product approval, reliable measuring
instructions/equipment and volume definition; it is not recommended for this V1. Item-name
guessing, placeholders and unapproved density inference are not viable policies.

## Changes required after approval

These are proposed future changes; none is implemented by CM-P0-02.

- **Backend/schema:** relational versioned profile and region selection, typed published
  profile read, additive customer selection/provenance contract, server resolution at hold,
  immutable profile/margin provenance on reservation/manifest, pre-hold declaration bounds
  and exact hold/item consistency rules. Keep current positive constraints, operational
  validator and existing idempotent booking/binding transactions.
- **Manager:** attributed profile creation/version activation/retirement, operational
  measurement basis and approved eligibility text; optimistic concurrency and exact command
  replay. Reuse existing immutable business history and region source invalidation patterns.
  No config inside free-form catalogue handling hints or generic JSON business authority.
- **Customer Mobile:** fetch the published profile, display approved eligibility guidance,
  persist selected version and exact command before POST, send profile selection rather than
  inventing mass/volume, handle missing/stale/oversize configuration, and apply the recovery
  contract in [CM_P0_02_BOOKING_RECOVERY.md](CM_P0_02_BOOKING_RECOVERY.md).
- **Operations:** approve measurement and mismatch SOP, eligible vehicles, calibration data,
  support ownership and review triggers. No cloud resource, queue, cache, paid provider or
  runtime AI is required; recurring infrastructure cost need not increase.

## Approval decisions and acceptance gates

The product owner must approve scope/eligibility, the standard-envelope vs size-band choice,
the actual mass and occupied-volume pair, measurement basis, margin interpretation,
customer confirmation/copy, optional declaration handling, and oversized/mismatch resolution.
Operations must attest that eligible vehicle limits and the declared service-duration class
support the offered collection. Numeric values remain deliberately unspecified here.

Before enabling live hold submission, test:

1. Missing/unapproved/retired profile cannot create a new hold; valid published selection
   resolves both positive values server-side. Caller tampering cannot lower either value.
2. Version/source races choose a consistent approved profile or reject; exactly one margin
   application and integer round-up. No money/item-name/density inference.
3. Hold exact replay after configuration changes preserves original pair/version/expiry;
   changed selection or declarations under the same command ID conflict. Concurrent final
   allowance and partial-write rollback retain existing protections.
4. Declarations above limits, changed item sets between hold and booking and out-of-scope
   work follow the approved policy; no silent clipping or under-reservation.
5. Binding creates one manifest with the exact retained values/provenance. Profile edits,
   optional weights and financial replay never rewrite historical snapshots.
6. Real routing validation rejects both mass and volume overflow, independently and with
   existing cargo, receiving windows and shifts. Forecast availability never overrides it.
7. Duplicate/concurrent physical observations, old evidence and crash/retry preserve existing
   command identity and append-only history; new observations affect effective load only.
8. Uncertain/oversized collection cannot clear a safety hold or lose an accepted request.
   Collected work and custody survive recovery; clearing requires current corrections and
   both capacity checks, and route continuation still requires normal authority.
9. Owner-scoped hold/booking restart recovery, payment-first acceptance, cancellation/refund
   independence and media-upload independence continue to pass.
10. Representative field measurements and observed outcomes support the proposed envelope,
    service duration and vehicle compatibility; synthetic tests alone cannot calibrate them.
