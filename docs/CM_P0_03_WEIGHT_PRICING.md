# CM-P0-03: configured item weights and bounded pickup pricing

Approved business direction: the CM-P0-03 implementation request, 22 October 2026.
Base `bdf13021c8c1c19819a507926b25e7661e8b81e2` includes CM-P0-02 recovery.
Branch `codex/cm-p0-03-weight-pricing`. Backend only; no deployment or production activation.

This supersedes flat pricing for newly quoted work after explicit weight-policy activation.
Historical quotes, Payment/Attempt/Refund and collection commitments retain their authority.
The earlier CM-P0-02 standard mass-envelope recommendation remains an unimplemented proposal;
the approved mass source is now the customer's configured per-line weight declaration.
**There is still no approved source for physical volume. New weight-priced customer holds
are explicitly blocked until that separate policy and its provenance are implemented.**

## Manager configuration

Use existing `POST /v1/manager/business/commands`, authenticated live MANAGER role:

| Field | Contract |
| --- | --- |
| `operation` | `WEIGHT_PRICING_POLICY` |
| `client_command_id`, `resource_id` | UUID; existing command and policy identities |
| `expected_version` | Integer, zero to create; current immutable history revision to edit |
| `reason` | Existing bounded non-PII manager reason code |
| `first_band_upper_grams` | B, positive integer |
| `numerical_threshold_grams` | T, integer strictly greater than B |
| `first_band_label`, `middle_band_label`, `open_band_label` | Required customer-visible strings, 1–100 characters |
| `display_unit` | `kg`; canonical storage/calculation is integer grams |
| `minimum_amount_minor`, `maximum_amount_minor` | X and Y; integer INR paise, Y >= X |
| `included_weight_grams` | W0, nonnegative integer |
| `saturation_weight_grams` | Wmax, integer strictly greater than W0 |
| `curvature` | a, positive exact decimal **string**, at most six decimal places, numeric(18,6) range |
| `currency` | `INR` |
| `active` | Strict boolean |

No configuration value is installed by the migration. There are no production B/T,
rates, W0/Wmax, curvature or marketing-price defaults. X's 100-paise lower bound is the
existing Razorpay technical minimum, not a proposed business rate. Money is bounded by
the existing customer DTO exact-integer limit, 9,007,199,254,740,991 paise. Weight inputs,
thresholds and aggregate grams use the existing routing input limit 2,147,483,647;
these are representation bounds, not physical collection permissions. Quantities are
positive integers within the same signed 32-bit representation bound.

The existing SERIALIZABLE command transaction, actor/client fingerprint, expected revision,
live authorization, source update, immutable revision and identifier-only outbox are reused.
The named concrete master is `weight_pricing_policy`; its concrete revision relation is
`weight_pricing_policy_revision`. Existing manager current/version/compare APIs work with
that kind, including `/v1/manager/business/weight_pricing_policy/{id}/versions`.
Audit JSON is history; typed current columns are business authority.

At most one policy is active (partial unique index). An inactive draft does not select the
new pricing mode. First activation sets server-maintained `adopted=true`, which cannot be
reset or deleted through ordinary SQL/application commands. Subsequently, no active valid
policy means unavailable pricing, including after retirement; legacy flat charging cannot
silently resume. Revise the active policy in place to avoid an unnecessary transition gap.
Activating another identity requires retiring the old active one first. Flat-price masters,
history and replay remain retained for the transition; they do not override an adopted policy.

## Item declaration semantics

All item schemas use these fields:

```text
item_category_code: string
declared_quantity: positive integer | null = null
weight_band: "BELOW_B" | "B_TO_T" | "ABOVE_T" | null = null
weight_kg: decimal string | null = null
declared_weight_grams: positive integer | null = null
```

Legacy null selection fields preserve old command bodies. Under an adopted weight policy,
every line must select a band:

| Selection | Valid input | Authoritative planning grams |
| --- | --- | --- |
| `BELOW_B` | “Less than B kg”; no numerical entry | B |
| `B_TO_T` | “B–T kg”; no numerical entry | T |
| `ABOVE_T` | Numerical kg string strictly above T | Exactly converted grams |

A weight selection covers the **entire quantity on that line**. It is never multiplied
by quantity. Aggregate weight is the sum of server-normalized line grams. Finite bands
are conservative reservations, not measured household weights. Exactly B belongs to the
middle customer range; exactly T does not qualify for numerical open-band entry.

Numerical kg accepts unsigned ordinary decimal notation with at most three fractional
digits. One fractional thousandth kg is one gram; there is no weight rounding. Signs,
whitespace, exponents, commas, NaN/infinity, binary JSON floats, excess precision, zero,
out-of-range values and aggregate overflow are rejected. An optional supplied
`declared_weight_grams` is an assertion that must exactly match server resolution; it
cannot lower the reservation. Finite bands reject any `weight_kg`.

Catalogue categories/groups must be active. Existing category quantity availability is
still enforced. Under the weight policy, the mandatory band contract supersedes the old
catalogue's optional raw-weight input hint; it does not change catalogue taxonomy. Customer
Mobile must fetch the global weight policy alongside the existing catalogue rather than
interpret the old `weight_grams=OPTIONAL/NONE` hint as permission to omit a weight band.

## Mathematical price specification

Let w be the normalized aggregate declared grams and D = Wmax - W0:

```text
t = clamp((w - W0) / D, 0, 1)
f = ((1 + a) * t) / (1 + a * t)
P = X + (Y - X) * f
amount_minor = floor(P + 1/2)
```

The implementation uses exact `Fraction` arithmetic, with the configured decimal converted
to its exact rational value. No binary floating-point amount enters calculation or Payment.
Final nonnegative money rounds **half up to one minor unit**, without marketing rounding.

For w <= W0 the result is exactly X; for w >= Wmax it is exactly Y. Within the variable
interval, the first derivative is `(Y-X)*(1+a)/(D*(1+a*t)^2)` and the second derivative is
`-2*a*(Y-X)*(1+a)/(D^2*(1+a*t)^3)`. Thus the underlying curve is monotonic and concave,
with diminishing marginal charges (constant if X=Y). Minor-unit quantization retains
monotonicity and bounds; individual one-gram rounded increments can alternate at rounding
boundaries. Strict concavity of every integer rounded step is not mathematically implied.

Boundary examples independent of numerical policy:

| Normalized position | Unrounded amount |
| --- | --- |
| At/below W0 | X |
| t = 1/4 | `X + (Y-X)*(1+a)/(4+a)` |
| t = 1/2 | `X + (Y-X)*(1+a)/(2+a)` |
| At/above Wmax | Y |

The arithmetic test's explicitly synthetic tie example uses X=100 paise, Y=101 paise,
W0=0g, Wmax=3g, a=1 and w=1g: P=100.5 paise rounds to 101. This tests rounding;
these values are not production configuration. Wmax never authorizes unlimited collection.

As in existing flat pricing, the first line carries the one pickup fee and additional
lines carry zero. Their immutable amounts sum to the server total. Each line records
`pricing_rule_version = WEIGHT:<policy UUID>:<revision>`; historic FLAT rules remain intact.

## Customer policy and quote APIs

Both endpoints require an authenticated current CUSTOMER. Responses are private/no-store.

`GET /v1/customer/weight-pricing-policy` returns all `WeightPolicyFields` above, plus:

```text
policy_id: UUID
policy_revision: positive integer
confirmation_freshness: "CURRENT_POLICY_REVISION"
live_customer_holds_available: false
hold_blocker: "PHYSICAL_VOLUME_POLICY_REQUIRED"
```

`POST /v1/customer/collection-quotes` accepts only
`{policy_id: UUID, policy_revision: positive integer, items: ItemDeclaration[]}` with at
least one item. The input policy/version identifies the bands the customer selected;
`409 WEIGHT_POLICY_CHANGED_REVIEW_DECLARATIONS` requires refreshing those bands if it changed.
It calls the existing PricingPort contract through ConfiguredPickupPricing
and returns:

```text
items: ValidatedItem[]                 # band required; declared_weight_grams resolved
total_weight_grams: integer
minimum_amount_minor: integer          # X
maximum_amount_minor: integer          # Y
total_amount_minor: integer            # computed, rounded server total
currency: "INR"
confirmation: {
  policy_id: UUID,
  policy_revision: positive integer,
  quote_fingerprint: 64 lowercase hexadecimal characters
}
confirmation_freshness: "CURRENT_POLICY_REVISION"
rounding: "HALF_UP_MINOR_UNIT"
live_customer_holds_available: false
hold_blocker: "PHYSICAL_VOLUME_POLICY_REQUIRED"
```

This POST is a pure query: no CollectionRequest, Payment, hold, route task, outbox or
idempotency record is created. Repeating it on the same version/declarations returns the
same content. There is no external provider call. A stateless version-bound confirmation
avoids a second durable Quote lifecycle or arbitrary quote TTL.

Missing/inactive/invalid weight configuration returns 503. Malformed schema/precision or
unknown fields returns 422. Semantic declaration conflicts return 409 with a controlled
string detail, including `WEIGHT_BAND_REQUIRED`, `NUMERICAL_WEIGHT_MUST_EXCEED_T`,
`DECLARED_WEIGHT_MISMATCH` and `AGGREGATE_WEIGHT_OUT_OF_RANGE`. Neither endpoint accepts
a caller-supplied total or price override.

Customer Mobile sequence: load the catalogue and current policy; collect one band per line
and a kg string only for ABOVE_T; submit policy identity/revision plus the declarations to
the quote endpoint; display the returned normalized weights, aggregate and total before
confirmation. Preserve the exact validated declaration strings and confirmation. Any policy
or declaration conflict requires refreshing/reviewing the quote. While
`live_customer_holds_available=false`, show the explicit availability blocker and do not
manufacture a volume or submit checkout. Existing committed bookings remain recoverable/payable
through their established financial eligibility rules. No frontend change is included here.

## Booking confirmation and durable recovery

Existing `POST /v1/collection-requests` adds optional `confirmed_quote` with the exact
confirmation schema above, and adds band/numerical fields to its existing `items`.
A new weight-priced booking requires confirmation. Prices/aggregate fields are not accepted
as command overrides. Existing context, slot, item, ownership and cutoff checks still apply.

The quote fingerprint binds policy ID/revision, normalized ordered item declarations and
the computed INR amount. The backend recomputes it; a changed item/quantity/band/numerical
entry or stale policy confirmation cannot silently charge another amount. Normalized item
facts populate booking items, not a frontend aggregate.

A shared PostgreSQL transaction advisory lock fences confirmation and creation against
the exclusive manager/policy-write lock, including first activation. The booking transaction
checks the current active revision after acquiring that lock. If a policy change wins,
booking rejects with `409` / `detail.code=QUOTE_CONFIRMATION_STALE_OR_MISMATCHED`; Mobile
must obtain and display a new quote before confirmation. If booking wins, its old amount
commits before the policy update affects later quotes. An unavailable active policy fails
closed; it never substitutes a flat rate.

The same transaction persists items, request, logical Payment, immutable BookingWeightQuote,
reservation binding/manifest when required, and the original command result. BookingWeightQuote
references the concrete policy revision with a real composite FK. SQL forbids editing its
provenance, weight-priced item declarations/line amounts, request quote and Payment amount,
including before payment initiation. Deferred SQL verifies the aggregate, line totals and Payment.
Existing initiation-term guards remain for legacy financial facts.

Creation responses add `weight_band` / `weight_kg` to items. Owner detail at
`GET /v1/customer/collection-requests/{id}` additionally exposes optional `weight_quote`
with policy ID/revision, original total grams, X/Y and fingerprint, plus each item's original
pricing rule version. Historical/legacy detail uses null `weight_quote`. The original quote
and Payment amounts remain the existing authoritative money fields.

Persist owner-scoped hold and booking IDs and the exact confirmed command before POST.
CM-P0-02 recovery remains: GET the original hold, follow `bound_request_id`, then load detail.
Null binding is still a snapshot, not proof that an in-flight transaction failed. Exact
client_request_id replay returns the committed booking before new pricing/expiry validation,
even after policy edits/retirement, payment or cancellation. New optional fields are omitted
from legacy fingerprints and preserve the old item representation. Changed-body replay conflicts.

## Hold/manifest consistency and the volume block

HoldSlot adds optional `items` and `confirmed_quote`. The transport-neutral primitive
validates the same declarations/quote and checks supplied mass equals their raw aggregate.
It pins raw declared mass, declaration fingerprint and quote fingerprint on the reservation.
Binding compares all three against committed booking facts, detecting even quantity/category
changes with unchanged aggregate grams. The once-reserved mass/volume copy to RoutingManifest.
The regional safety percentage is applied exactly once to each independent raw physical input
using integer round-up; pricing always uses the unpadded customer-declared weight.

The existing independent positive volume input is retained by that operational primitive;
it is never inferred from mass or item names. Tests exercise it only with explicitly synthetic
operational volume. **New HTTP customer holds under adopted weight pricing return
409 PHYSICAL_VOLUME_POLICY_REQUIRED, even if a caller supplies positive volume.** This gate
runs after completed hold replay, preserving durable recovery. No production bypass flag is added.
Confirmed weight-priced HTTP booking also always requires a matching reservation, including
when orchestration registration is disabled, so the legacy booking route cannot bypass this gate.

The outstanding product/Operations decision is the independent source of occupied transport
volume: customer size declaration, calibrated collection/category envelope, or measured operational
assessment; its numeric values, eligibility, versioning/provenance and mismatch handling must be
approved and implemented before removing the gate. A density conversion or placeholder is not
authorized. Vehicle mass/volume limits and independent operational route validation are unchanged.

Later ObservePhysicalLoad appends evidence for effective operational mass/volume. It cannot
rewrite original band/declared grams, quote, Payment or refund, and never authorizes an extra
debit. Existing safety holds, manager recovery, validated custody and media independence remain.

## Schema, rollout and evidence

Migration `0037_weight_pricing` follows `0036_recovery_work_requests`: three concrete tables
(policy, policy revision, booking provenance), nullable item band/numerical fields and nullable
reservation provenance. No historical measurements, new quotes or financial amounts are backfilled.
New code requires migration to head first. Empty-feature downgrade preserves historical data;
any retained policy (including drafts) or weight provenance refuses lossy downgrade.
Disablement does not erase debt or custody.

Source and tests are listed in [CM_P0_03_VALIDATION.md](CM_P0_03_VALIDATION.md). No Azure
resources, queues, cache, services, runtime AI, frontend changes or production flag activation.
