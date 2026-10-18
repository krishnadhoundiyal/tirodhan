# Projection-backed commercial booking

Base: `7f3b19a11e2e216e8a806a529a9418a1b5a81281`.
Branch: `codex/commercial-admission-scalability`.
Approved policy change: 19 October 2026. Production activation remains disabled.

## Customer boundary

Commercial acceptance based on per-booking routing search is retired. POST
`/v2/customer/capacity-admissions` returns 410. Its finite worker entry point returns
`COMMERCIAL_ACCEPTANCE_RETIRED_USE_SLOT_HOLDS` without loading configuration, a road graph,
or a native optimizer. Historical admission status remains readable; unresolved old intents
are shown as RETIRED, rather than asking a customer to wait for a retired worker.
Existing successful exact reservations retain their original binding and expiry rules.
The fleet tick also stops invoking the historical commercial-admission queue. Internal exact
helpers remain for historical verification; they have no customer API or production worker caller.

The new flow is slot projection -> atomic slot hold -> pending-payment booking -> payment.
GET `/v2/customer/slot-availability` publishes estimated allowance, generation, source/policy
versions, computation/expiry timestamps and the standard pickup/discharge service class.
POST `/v2/customer/slot-holds` accepts the selected generation and the existing reservation
inputs; GET `/v2/customer/slot-holds/{reservation_id}` returns owned hold/binding permission.
The existing booking API binds `capacity_reservation_id`. Neither hold acquisition, binding,
payment preparation nor authenticated capture invokes road acquisition or route feasibility.
Normal authorization, serviceability, booking cutoff, payment expiry and financial checks remain.

AVAILABLE and LIMITED permit holds; FULL has no remaining modelled allowance; UNKNOWN cannot
create new permission. A hold acquired earlier remains bindable when a projection becomes stale.
The API never calls a projection refresh synchronously as a hidden checkout fallback.

## Non-trained forecast

There is no ML training, trained artifact, model server, paid API or new runtime dependency.
`AllowancePolicy` requires explicit regional assumptions: local/clustered travel seconds,
facility travel seconds, stem seconds, clustering threshold, nominal pickup start offset and
LIMITED threshold. Optional non-overlapping Asia/Kolkata time bands override travel estimates.
Missing policy yields UNKNOWN. No purported Delhi traffic values or calibrated probabilities
are installed as production defaults. No second blanket reserve percentage is added.
Physical vehicle load does not determine projected allowances. Existing manifest load snapshots
and operational vehicle/custody validation remain intact.

For service-start slot [a,b], the forecast uses nominal start a+offset. Its work interval is
[a+offset-local_travel, a+offset+pickup+facility_travel+discharge]. Exact integer intersections
with successive 30-minute buckets form the workload debit vector. Thus travel can occupy a
preceding bucket and discharge can occupy following buckets. The sum of debits equals total
forecast duty seconds. This is a forecast footprint, not a physical route or a reassignment.

Existing accepted uncollected work and live holds contribute exactly once. Bound pending-payment
holds remain holds; paid bookings contribute as bookings. Latest customer-approved appointment
windows govern forecast placement without rewriting original transaction snapshots. Overdue
unplanned work rolls into current workload accounting, without changing its promised deadline.
Known pending/started route tasks contribute unioned travel/service intervals; they are excluded
from unassigned workload. A task in the afternoon no longer blocks the entire morning. Overdue
live tasks retain forecast work instead of disappearing. Unrouted loaded resources remain blocked;
natural shift expiry never clears custody or manufactures available capacity.

Known same-cell/same-slot concentrations may use configured clustered travel after the first
threshold-minus-one full travel charges. New claims always use dispersed travel. Stored projected
reservation debit vectors remain lower bounds when refreshed, preventing policy updates or
consolidation estimates from erasing already-reserved workload. Actual represented route work
supersedes estimated placement during refresh; original debit history remains immutable.

Interval union/subtraction handles breaks and current work. A separate pickup-start budget
screens contiguous service fragments, shift tails and default authorized facility openings and
restrictions. Pickup service may finish after its appointment slot. DUTY and PICKUP budgets are
separate necessary constraints; neither is represented as a sufficient routing proof.

This increment offers the existing region's standard pickup/discharge class. Requirements above
that published class return SLOT_SERVICE_CLASS_UNSUPPORTED; smaller client values cannot lower
server service minima. This prevents combining small free fragments to sell a longer indivisible
service. Supporting multiple published service classes is a future extension, not a hidden solve.

A slot's standard allowance is the minimum of pickup-budget headroom divided by pickup duration
and every affected duty-budget headroom divided by its footprint debit. Arithmetic is integer.
There is no per-slot independent fleet duplication, learned threshold or invented confidence level.

## Inventory and transactions

Migration 0032 adds immutable manager-attributed `regional_slot_policy`, shared
`regional_capacity_budget`, and immutable `slot_reservation_debit`. Existing projections gain
generation, policy version and remaining-booking fields. Existing reservations gain explicit
EXACT_CERTIFICATE / PROJECTED_ALLOWANCE basis, policy FK and estimated duty snapshot. Only
projection reservations have SQL NULL certificate; no fake route certificate is generated.
Old rows remain EXACT_CERTIFICATE. Upgrade invalidates old advisory slot statuses. Downgrade
refuses projected-reservation or estimator-policy history; empty-feature rollback preserves
financial history, PaymentAttempt defaults and all three initiation-term triggers.

REGION configuration remains retained history. Manager SLOT_ALLOWANCE_POLICY appends a policy
version under expected region version and existing command idempotency/live role authorization.
The version must increase, and a reservation references the exact retained policy version.

A hold uses existing actor/client UUID and fingerprint. Context ownership is locked, region and
source head are fenced, and a short transaction inserts reservation and all bucket debits.
PostgreSQL conditionally consumes each budget only when generation and available amount match;
a deferred constraint checks pickup and complete duty debits. A failure rolls back every debit,
reservation and command result. Exact replay returns the original hold without extending expiry.
Only the command's known source/hold deltas are reflected in the current projection generation;
other source drift still invalidates it. Neighbouring slot allowances are updated transactionally.

Publication computes a consistent snapshot, then locks head before writing budgets/projections,
matching hold lock order. Concurrent source drift aborts publication instead of overwriting a
successful hold. Rebuilds include current holds and accepted work once; they do not reset consumed
capacity. Expiry/cancellation/payment state changes use existing source events and bounded refresh.
`SlotCapacityHeld` is a durable domain outbox record; the existing publisher allowlist is unchanged.
Freshness is driven by transactional source-head/RegionalSlotRefresh changes and finite workers,
without depending on a new broker delivery path for that event.
Until reconciliation releases capacity, the estimator may conservatively withhold it. Existing
holds/accepted bookings are never revoked by projection expiry or a lower later estimate.

Shift close and idle regional transfer preserve existing exact allocations/cargo guards. They
also protect forecast-backed regional commitments: stale inventory blocks capacity removal, and
fresh budgets must retain headroom after conservatively subtracting the departing shift's nominal
contribution. This is regional capacity ownership protection, not rider assignment. One rider has
one authoritative capacity-owning region. Physical custody transfer remains its existing workflow.

## Operations, cost and limitations

Existing finite availability/regional workers refresh affected regions; periodic reconciliation
recovers delayed/interrupted execution. No Service Bus entity, Redis, database, GPU, Azure ML,
always-on service, cloud resource or production scheduler is provisioned. Scheduling of the
existing finite jobs and regional calibration are activation prerequisites.

Known-input aggregation is proportional to live regional data. Interval operations sort endpoints;
workload footprints touch bounded time buckets. The calendar has at most seven dates / 224 slots.
Hold acquisition reads that bounded inventory, not the region's household/road matrix, and performs
no native search. Budget rows reuse region/time/kind keys across refreshes; debit/policy history is
retained rather than replacing transactional facts with JSON or an unbounded snapshot per hold.

The model reserves estimated capacity. It does not mathematically prove every sold combination
has a feasible route, does not claim a measured reliability percentage and does not guarantee
actual traffic or rider availability. Accepted work remains a fulfilment commitment requiring
operational recovery if forecasts are wrong. OSRM/VROOM/OR-Tools and the independent validator
continue to govern operational planning/dispatch; their provider failures cannot undo payment or
silently discard an accepted request. The upstream OSRM negative-distance release gate remains.

Validation must measure false availability, unnecessary closures, throughput and queue/refresh
latency on representative scenarios. Synthetic tests establish arithmetic and transactional
invariants; they are not Delhi-wide prediction calibration. See the accompanying validation
record [PROJECTED_SLOT_ALLOWANCE_VALIDATION.md](PROJECTED_SLOT_ALLOWANCE_VALIDATION.md) for
actually executed tests and benchmarks.
