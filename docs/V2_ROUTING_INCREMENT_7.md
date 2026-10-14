# V2 routing increment 7 — operational orchestration

Implementation on `codex/v2-operational-orchestration`, based on merged main
`6ef42ca0d960246d13a566c0459b8d90b541ca5b`. Production activation remains disabled.
This report describes implemented mechanisms and separates measurements from pilot assumptions.

## Boundaries and authority

`routing_orchestration_enabled` defaults to false and requires both existing fleet and
dispatch flags. The transactional API remains the modular monolith. No Terraform resource,
production schedule, public routing server, paid matrix dependency, Redis or extra database
has been added. The disposable databases and containers used for verification are local.

The approved OSRM 6.0.0, VROOM 1.15.0 and OR-Tools 9.14.6206 portfolio remains intact.
Both native adapters feed the independent validator. Contract V1 inputs remain readable;
V2 adds vehicle breaks. Old command fingerprints omit newly defaulted fields so an upgrade
does not change exact replay identity. Pending evaluations whose source shape changed must
be evaluated again; an old proposal cannot bypass current activation checks.

Capacity certification, route assignment, receipt, voluntary acceptance, physical pickup
and physical custody transfer are different facts. A solver proposal establishes none of
the latter facts. Payment/refund provider behavior and the initiation-term migration are
unchanged. New-work cutover is opt-in: capacity-bound requests are excluded from legacy
discovery/freeze/dispatch. Historical V1 assignments continue to drain.

## Regional admission

`OperatingRegion`, H3 membership, vehicle membership and authorized receiving points are
relational configuration. A designated default warehouse must be in the authorized set.
Normal demand eligibility is restricted to the configured region. Moving, BUSY and loaded
committed vehicles are evaluated with idle committed vehicles. IDLE is not an empty-load
certificate. Explicitly transferred cargo follows its actual custodian across regional
boundaries; this does not authorize normal borrowing of another region's vehicle.

The customer requests a durable capacity evaluation, receives HTTP 202 and polls its own
result. Finite workers obtain road matrices and run the engines outside database locks.
All incumbent obligations and active capacity holds are required in an admission problem.
The prospective household uses the server-owned location, mass, volume, service durations,
customer window, shift, breaks, cargo and authorized eventual discharge. There is no H3
booking-count quota. The current default warehouse is the prospective admission destination;
existing kiosk obligations can be amended through authorized destination recovery.

A certificate commits in SERIALIZABLE isolation after locking its region, comparing the
generation and complete current source population, checking elapsed time and fencing its
worker lease. The generation increments and has a unique `(region_id, region_version)`
certificate. PostgreSQL SSI also protects changes across independently locked regions.
The final check includes the coordinator's bounded working set. Exceeding that technical
envelope is a partitioning/infrastructure failure, not a claim that a cell is physically full.

The booking transaction must bind an owned, matching, unexpired certificate with enough
remaining lifetime to cover the existing payment expiry. The certificate binds once.
PostgreSQL independently verifies customer, context, location and appointment identity;
new appointment consent is included in the deferred check. A certificate is not an assignment.
Acceptance replaces the pending-payment forecast with the actual accepted booking. Cancellation
or expiry removes demand from subsequent projections without reversing financial history.

Region configuration applies a 125% default declared mass/volume allowance and minimum
planning service estimates of 180 seconds for pickup and 60 seconds for discharge. These
are explicitly initial engineering estimates, not measured household service distributions,
charges or customer waiting SLAs. Operators can configure more conservative estimates.
Known item weights cannot exceed the bound reservation. Physical observations amend the
effective load while retaining the original manifest. Certification remains conditional on
accurate declarations and operational inputs; the pilot must calibrate these estimates.

Flexible capacity contributes zero confirmed supply until a particular rider accepts an
offer. Acceptance has a unique business commitment and a real two-rider race has one winner.
Certification still runs afterward; acceptance alone creates no route. Committed-only
construction is attempted first. Supplementary construction and independent economic ranking
retain coverage/deadline protection while preferring committed capacity over flexible work.
Managers receive attention when tested capacity/response opportunities are exhausted.

The implementation retains the last immutable regional certificate as an insertion seed.
Changed/missing stop identities or an infeasible insertion cause full dual-engine evaluation.
Every incremental result is independently checked against the entire required population.
Regional insertion is bounded by candidate count and a ten-second wall budget. Road matrices
are reused only for the exact immutable node set inside a finite job, with at most two entries
and a 30-second TTL. This adds no shared cache or recurring service.

## Operational clocks and initial T−M

Commercial booking eligibility, provider capture/payment expiry and cancellation still use
their approved existing rules. Capacity-token expiry, operational planning readiness,
allocation assurance, acknowledgement and ETA freshness are independent clocks. A new
appointment records customer consent without rewriting the original commercial slot,
quotation, currency, payment or cancellation cutoff. It does not grant new refund rights.

The existing prohibition on ordinary customer cancellation after a pickup attempt is
preserved, including unsuccessful attempts. New appointment consent does not reopen that
right. Changing this no-show cancellation/refund policy requires separate owner approval;
operational reattempt eligibility itself does not imply such a change.

The initial **pilot estimate is M = 1,275 seconds (21 minutes 15 seconds)**:

| Component | Seconds | Basis |
|---|---:|---|
| Bounded worker/compute and backlog allowance | 300 | Observed maximum-budget cycle 59.21 s, admission 50.76 s; finite admission work budget 120 s plus publication/cold-start headroom |
| Scheduled wake/delivery of work to a finite job | 60 | Proposed one-minute lightweight fallback check; not measured Azure scheduling latency |
| Conflict/retry allowance | 180 | Three observed 59.21 s large cycles, rounded up |
| Rider receipt | 60 | Initial polling/receipt requirement; pilot assumption |
| Rider readiness | 120 | Explicit unmeasured operational assumption |
| Approach travel | 180 | Conservative pilot allowance for the small fixture; full-region roads/vehicle profiles must recalibrate it |
| Operational recovery allowance | 120 | Technical recovery allowance, not a physical-breakdown resolution SLA |
| Safety margin | 25% | Applied to the 1,020-second sum |

`planning_lead_seconds` is configuration, not a hardcoded T−15/T−30 cutoff. Use T as the
service-window lower bound for early planning readiness; allocation assurance uses the
inclusive upper bound minus M. Receipt timeout is separately configurable and constrained
by predicted arrival and the readiness budget. `dispatch_readiness_seconds` should initially
be 180 (receipt plus readiness). Neither setting changes commercial customer rights.

This is an engineering estimate for a bounded pilot, not a production service guarantee.
Human readiness, full Delhi graph loading, traffic and real cart/van profiles were not measured.
Do not enable production on the strength of the clipped car-profile fixture. Record actual
percentiles and revise the components before cutover. A breakdown can require customer consent
for a new appointment; this budget does not invent a guaranteed physical recovery time.

Customer service start is allowed at the exact upper boundary; one microsecond after it is
rejected. Breaks use `[start, end)` and duty starts are within `[shift_start, effective_end)`.
Token expiry is exclusive; exact payment-lifetime coverage is sufficient for binding. A late
receipt is recorded as receipt, never as a veto. No engine can start cancelled work.

## Reattempt, custody and recovery

Unsuccessful V2 pickup records `CUSTOMER_UNAVAILABLE`, `UNCONTACTABLE` or `ACCESS_BLOCKED`,
an immutable attempt, captured evidence and a reattempt eligibility time. It releases only
that household and preserves stable `PickupExecution` and unaffected route progress.
NOT_COLLECTED does not add cargo, complete the booking or initiate cancellation.

The technical cooldown is at most 60 seconds and no more than half the remaining window.
The rider need not wait at the household. Two automatic reattempts after an unsuccessful
first attempt are permitted before operational review; a manager can explicitly authorize
another attempt. This is a workload/review bound, not a terminal retry policy or customer
charge. Binding retry/refund/compensation limits remain an owner policy decision.
Reattempts can use another eligible route or a subsequent route of the original vehicle.
Released assignment membership is not resurrected. A new promised window requires a new
feasible capacity certificate and explicit customer consent. An unassigned accepted booking
may also use this consent workflow; already started/owned work remains fenced.

A manager prepares cargo recovery against the actual current custodian and receiving shift.
Both vehicles are fenced during the pending handoff, including before the final confirmation.
Active service or another transfer must finish first. A receiver cannot fill capacity during
this interval and a sender cannot hand over the same lot elsewhere. Expiry never silently
releases this physical hold. Manager abort requires an attributable PHOTO inspection affirming
the material remains with its current custodian. The intent resolves once and cannot rewrite
its source, destination or authorization history.

Final transfer needs donor/receiver PHOTO attestations, scanned identity, current custody,
unhanded material and receiving physical capacity. A manager may explicitly witness the donor
side when the original rider cannot act; the actual witness is recorded without impersonating
the donor. The receiver still signs personally. Confirmation records the original authorizer
and the confirming manager separately. Original successful pickup and cargo observation are
retained; a monotonic custody sequence determines the current holder. Neither preparation,
software reassignment nor a PHOTO upload implies validated delivery.

Receipt capture can be recorded after an intent's technical timeout while the physical hold
remains unresolved. It must still follow preparation and cannot be future-dated. Fresh manager
completion rechecks the current receiving duty, actual capacity, authority and custody.
Rejecting every late attestation would otherwise strand an evidenced physical handoff behind
its unreleased hold. Expiry grants neither automatic transfer nor automatic release.

Pickup, transfer and discharge arbitrate Payment first, then booking/ownership and sorted
rider/vehicle resources. Shift transitions use the same resource order. Shift close and
route completion consider effective custody, including cargo received through recovery;
an empty task list cannot return a loaded rider to IDLE. Safety generation is separate from
GPS generation: movement does not erase a resume authorization, but an incident/load change
invalidates it. Clear, observation, resume permission and validated reactivation are separate.

Breaks preserve cargo and are included in both native engines and independent simulation.
Shift extension requires fresh rider availability consent and manager safety authorization.
It can authorize an expired shift to resume, remains bounded by the one-day technical routing
horizon, and does not decide employment law or unlimited working hours.

Closed kiosks are evaluated against authorized alternatives, with the expressly designated
warehouse last. Shared closed-facility obligations are tested collectively. Destination
amendments are audited and do not rewrite original manifests. A current, independently valid
route must activate before new pickup work resumes. Routine late discharge can amend its
scheduling horizon; actual authorized handover within a valid duty/opening window is allowed
after the old scheduling deadline. There is no fixed discharge SLA tied to a pickup slot.
Custody remains until a validated, correctly attributed, evidenced handover.

## Cancellation, synchronization and finite execution

For regional V2 work the successful cancellation transaction remains authoritative and final.
It records its existing refund obligation and a durable wake, without waiting for route
cleanup. Execution checks the booking under Payment-first locks. Finite reconciliation
releases cancelled ownership/tasks in bounded batches, leaves unaffected work intact and
never restores the booking when replanning/refund/notification fails.

Financial facts are consumed through a separate inbox consumer without taking over their
original outbox publication. Wakeups coalesce monotonically; completion acknowledges only
the generation captured by that cycle. A later wake cannot be lost. Elapsed planning, break
and delivery boundaries also trigger work. Existing single-owner token/expiry takeover and
transactional activation fences remain the authority boundary.

`routing_fleet` prioritizes fleet work before pending admissions. Admission processing has
a 120-second finite work budget and fenced retry. `routing_probe` is a read-only preflight
that compares real source/deadline/movement changes without calling a matrix service or
solver. Use existing outbox wake hints plus a lightweight scheduled fallback; avoid loading
a graph for a no-op check. No cloud trigger or recurring schedule has been provisioned.

Committed riders poll the authoritative revision and acknowledge receipt. Existing departed
destinations remain protected; uncommitted work requires the current receipt. Delivery
attempts and timeout attention are durable, with bounded identifier-only redelivery hints.
Push delivery is optional: these endpoints function through polling without a new service.
Frontend transport/notification integration is in the separate repository.

Customer ETA is advisory, revision-bound and based on the executable stop order and road
estimates. Recommendations do not overwrite the current revision's ETA order. Material
changes of at least 60 seconds or a new revision emit an identifier-only update. Stale/held
routes show UPDATING. Unassigned, review-required and in-transit states are distinct. Customer
responses expose no live positions or continuous map. See [frontend contract](V2_ORCHESTRATION_API.md).

## Measured performance and costs

Reproducible scripts and aggregate data are in `tests/routing/benchmark_orchestration.py`,
`tests/integration/test_orchestration_benchmark.py` and `docs/benchmarks/increment_7_*.json`.
They use the public clipped Delhi OSM fixture (3,715 edge-based nodes / 13,630 edges), four
validated public road points, two CPU allocations and isolated PostgreSQL/PostGIS 17/3.5.
This is dense synthetic demand, not full-city geography or real customer observations.

| Vehicles / requests | Samples | Dual-engine/refinement median | Observed max | Matrix acquisition |
|---|---:|---:|---:|---:|
| 4 / 8 | 10 | 1.824 s | 1.864 s | 0.104 s |
| 8 / 32 | 5 | 2.423 s | 2.629 s | 0.068 s |
| 16 / 64 | 3 | 4.266 s | 4.386 s | 0.092 s |
| 32 / 128 | 3 | 10.905 s | 14.350 s | 0.208 s |

Search is one second in that table; breaks, initial cargo, adjacent slots and discharge are
included. VROOM passed independent validation at all four sizes. OR-Tools returned
NO_FEASIBLE_PROPOSAL in all three largest one-second cases. This is not proof of infeasibility;
the VROOM witnesses are feasible. Neither engine has been removed. The separate maximum
30-second-budget PostgreSQL campaign passed at 32 vehicles and 128 total demand records.
Its final-source cycle was 59.21 s and certification 50.76 s. This rerun shared the local
PostgreSQL instance with repository validation; earlier isolated values were 52.30 s and
43.10 s. The conservative lead estimate uses the higher observation. Small-sample maxima are reported;
there is no statistically defensible P99 claim from this campaign.

| Vehicles / existing demand | Fleet cycle | Admission of one additional booking | Cycle queries / admission queries |
|---|---:|---:|---:|
| 4 / 8 | 2.560 s | 1.790 s | 296 / 105 |
| 8 / 32 | 3.353 s | 1.986 s | 736 / 105 |
| 16 / 64 | 8.096 s | 3.850 s | 1,356 / 105 |
| 32 / 127 | 59.211 s | 50.764 s | 2,536 / 105 |

Seven snapshots per configuration issued 30 statements per snapshot. Observed near-limit
snapshot durations were 72–256 ms in the shared-instance rerun. Strict activation still has
substantial per-stop SQL cost; the current bounded envelope is not evidence of economical
unbounded scaling. SQL batching, larger regional partitioning and full-city sizing require
additional measured engineering before increasing the envelope.

The separate real-PostgreSQL batching fixture verifies one and two authoritative routes.
Its matrix and seed engines are synthetic, explicitly labelled in
`docs/benchmarks/increment_7_batch.json`. Two customer cancellation transactions remained
separate (29–65 ms each). One-route reconciliation cost 88.9 ms in two worker transactions
versus 60.6 ms in one; two-route reconciliation cost 68.1 ms versus 63.4 ms. These are
single samples per configuration, not a throughput or percentile claim. The four subsequent
admissions took 139–258 ms individually; after the first dual-engine seed, all three used
validated incremental insertion with no further engine invocations. Incremental insertion
therefore saved solver calls here, but did not consistently reduce measured wall time.
Each certificate still commits independently: batching optimization never batches or delays
the authoritative capacity claim or customer cancellation. Pending additions are included
collectively in the next fleet cycle; the near-limit native campaign measures that full path.
Simultaneous batch-admission throughput remains a pilot metric.

512-node Table acquisition took 1.276 s for one request, 1.830 s for two simultaneous
requests and 5.270 s for four. Exact finite-job cache hits took 12–48 microseconds.
Parent peak RSS was 153,152 KiB and cumulative native-child peak 135,252 KiB. The tiny OSRM
graph server used about 8.4 MiB in the sampled Docker measurement. These are not full Delhi
graph sizing numbers. OSRM must use `--max-table-size 512`; its default is 100. Use
`--verbosity WARNING` (or equivalent suppression) to keep private coordinate URLs out of logs.
Close synthetic nodes also produced a native -0.4 m distance; the provider correctly rejected
that matrix. No negative-value clamping or paid/public-demo fallback was introduced.

The selected topology is finite native work with a private job-local OSRM process/sidecar
and an immutable graph from existing storage, preceded by a cheap probe. The local harness
demonstrates that topology. A production graph/image/startup mechanism and full-city profile
sizing still require measured release review; they are not silently provisioned here.
Production packaging must supervise and stop the road process when native work ends; an
indefinitely running sidecar would prevent a finite job from completing. This increment's
Docker harness cleans up its own server/database/graph volume, while native adapters bound
and terminate their child processes. It is not a production sidecar deployment.

Azure Retail Prices API, Central India, INR, retrieved 14 October 2026: active CPU
₹0.0023/vCPU-second and memory ₹0.0003/GiB-second. Jobs bill allocated resources while running;
they do not bill an idle execution. [Published billing](https://learn.microsoft.com/en-us/azure/container-apps/billing)
and [retail-price API](https://learn.microsoft.com/en-us/rest/api/cost-management/retail-prices/azure-retail-prices).

At 2 vCPU / 4 GiB the gross rate is ₹0.0058/second. Forecast examples (30-day month,
excluding tax/discounts, Blob operations, egress and existing PostgreSQL costs):

| Assumed usage | Gross incremental compute |
|---|---:|
| 600 native jobs × 180 s | ₹626.40/month |
| 5,760 native jobs × 60 s | ₹2,004.48/month |
| Busy 8-hour/day operation: 14,400 jobs × 60 s | ₹5,011.20/month |
| Continuous minute-level native work: 43,200 jobs × 60 s | ₹15,033.60/month |
| 43,200 lightweight checks × 10 s at 0.25 vCPU / 0.5 GiB | ₹313.20/month |

The ten-second check duration is an unmeasured Azure cold-start assumption, not local SQL
runtime. Subscription-shared free grants may reduce bills; they are not assumed to remain
available. A permanently warm 1-vCPU/2-GiB service is approximately ₹2,332.80/month at pure
idle rates or ₹7,516.80/month at active rates before grants. That would be a new recurring
commitment and has **not** been enabled. Full-city memory, multiple profiles and graph loading
can change the topology/cost materially. Decide from measurements before any production action.
The high-frequency scenarios make the cost risk explicit: scale-to-zero does not make
continuously necessary computation free. Deadline/coalescing/movement controls and measured
regional partitioning must keep unnecessary invocations out of the production design.

## Retention and remaining release decisions

`routing_retention` provides a finite read-only inventory: closed routes older than 90 days,
inactive diagnostic runs older than 30 days, unanchored/non-latest positions older than seven
days and the twelve-month metrics window. Live route runs and last/anchored observations are
protected. Existing records remain queryable; no irreversible purge or unverified legal
retention rule is implemented. Financial, customer, evidence and custody records retain
their separate policies. PostgreSQL PITR/RPO/RTO are unchanged. Reviewed archival, aggregate
metric export and compliant destruction remain separate release work.

Pilot prerequisites include configured regions/authorized warehouses and receiving identities,
opening windows, truthful physical declarations, realistic service-time distributions, declared
shifts/availability, fresh positions and suitable road profiles. The existing one-open-shift
resource model remains; wider durable duty calendars and coordinator partitioning beyond the
bounded global envelope need a subsequent reviewed increment. Road estimates are static-profile
estimates, not live traffic. Customer-facing rider names/contact disclosure and frontend polling,
PHOTO/QR capture and push consumption need their separate implementation/privacy review.

Do not infer a terminal no-show policy, new charge/refund entitlement, unlimited shift hours,
legal deletion permission or a warm production road service from this increment.

## Validation record

The first complete repository run reported **1,321 passed, 34 skipped, four warnings and one
failure**: the existing webhook broker-entry test exceeded its one-second test wait during
concurrent benchmarking. Its isolated rerun passed. Financial code/test timeouts were not
changed. Focused operational and populated-history migration regressions passed after correcting
fixture setup and replacing the draft blanket intent trigger with a write-once resolution guard.
Results from the final campaigns follow.

Final quiet full repository campaign on the corrected implementation, seed **71015**:
**1,341 passed, 42 explicitly opt-in skips, four dependency warnings in 1,380.32 seconds**.
This rerun includes the corrected crash fixture, coalescing, preflight, late recovery and
custody/rescheduling tests. A supplementary explicit receiving-point-closure/discharge
overlap was added afterward and is included in the final focused campaign record below;
no application code changed after this full run began.

An earlier quiet full run passed **1,333 tests, 42 skips and four warnings in 1,460.41 s**.
The expanded seed-71015 run then reported **1,337 passed, one failed, 42 skips and four
warnings in 1,724.59 s**. Its crash/takeover fixture assumed a particular contender won
the lease and therefore asserted a simulated crash even when the healthy contender won.
The correction makes both simultaneous contenders crash after ownership, independently
asserts exactly one crashed owner and one gated contender, and checks lease-expiry takeover.
Its exact-seed focused replay passed in 37.51 s. Domain lease/authority logic was not changed.
The other corrected observer checks two live database waiters without assuming both wait
directly on the same vehicle: resource acquisition is rider-first, then vehicle.

Final recovery/preflight checks also caught an ambiguous delivery/acknowledgment join and
premature graph-start hints before cycle readiness/while owned. The explicit revision join,
deadline/lease checks and non-destructive retention assertions passed. New wake generations
start a fresh coalescing window only after prior acknowledgment. Late physical transfer
attestations remain recordable behind their unresolved safety hold, subject to fresh final
manager confirmation. All were included in the final increment-specific rerun below.

The retained schema helper excludes only catalog-proven extension-owned tables and their
indexes. Its application comparison still requires exactly the two pre-existing migration
0007 index omissions (`ix_collection_request_pickup_location_gist` and
`ix_collection_request_planning_batch_id`) and independently checks their columns, methods
and options. `spatial_ref_sys` is extension-owned and is excluded, not a new application
difference. No new table/type/FK/unique/index differences were accepted. PaymentAttempt
type/default comparison remains empty and all three initiation-term triggers are retained.

Final increment-specific PostgreSQL campaign: **29 passed in 64.95 seconds**. This includes
the empty-adoption populated-financial-history downgrade/upgrade, refusal of destructive
populated-new-history downgrade, strict schema comparison, late witnessed/personal transfer
receipts, receiver-attributed handover, lease/deadline preflight and read-only retention.
Real database overlap tests cover scarce capacity, flexible acceptance, START versus
break/cancellation, transfer versus active service, receiving-point closure versus discharge,
consent versus cancellation and revision
fencing. Existing generated families retain cancellation/dispatch/collection, ownership,
handover, shift, replay, multi-operation and crash/takeover coverage.

Expanded pinned native/PostgreSQL campaign, seed **61010**: **203 passed in 679.44 seconds**.
Extended independent unit oracle, seed **71014**: **5 passed in 9.76 seconds**, with 100
examples per generated property. Extended native oracle, seed **71016**: **6 passed in
34.13 seconds**, including 20 examples per engine. Infeasible generated inputs remain
explicitly tested outcomes. Real PostgreSQL/native benchmark: **4 passed in 168.53 seconds**;
synthetic-route PostgreSQL batching benchmark: **4 passed in 14.83 seconds**.

Ruff, formatting (394 Python files), strict mypy (197 source files), `pip check` and
`git diff --check` passed. No financial-provider logic or financial test timeouts changed.

## Reproduction and next increment

Never target an operational database: integration fixtures truncate application tables.
Use a disposable PostgreSQL/PostGIS 17/3.5 database with PostGIS bootstrapped before Alembic.
The full default-dependency run used Python 3.12; the pinned native image uses Python 3.13.

```powershell
# Set TIRODHAN_TEST_DATABASE_URL to your disposable, bootstrapped PostGIS database.
pytest -q --hypothesis-seed=71015 -p no:cacheprovider
ruff check .
ruff format . --check
mypy
pip check

# Self-contained local native/PostGIS harness; builds the pinned image by default.
powershell -NoProfile -File tests/routing/run-operational.ps1 -Seed 61010
# Expensive modes are explicit; cached images may use -SkipBuild.
powershell -NoProfile -File tests/routing/run-operational.ps1 -SkipBuild -Benchmark -Seed 71015
powershell -NoProfile -File tests/routing/run-operational.ps1 -SkipBuild -Extended -Seed 71016

# Standalone native/512-node matrix measurements use the same pinned private OSRM fixture.
python tests/routing/benchmark_orchestration.py
```

The last command needs `TIRODHAN_OSRM_TEST_URL` and `TIRODHAN_OSRM_GRAPH_VERSION` and both
native binaries in its isolated environment. It emits aggregate JSON; PostgreSQL benchmark
tests label their aggregate rows separately. The harness extended mode is broader than the
two separately recorded unit/native oracle campaigns and was not claimed as executed here.

Recommended next increment: pilot release readiness and measured regional scalability.
Calibrate full-region cart/van graphs, service/readiness/receipt distributions and cold-start
cost; reduce measured activation SQL cost and introduce reviewed coordinator partitioning
before increasing limits. Add audited region-maintenance/durable duty-calendar operations
and independent generated histories for the new admission/custody workflows. Complete
separate frontend/push consumption and controlled V1 drain/cutover after review. Review
post-no-show financial policy and legally compliant archival separately. No such subsequent
implementation or production activation is included in this commit.

No PR, merge, push, deployment or production activation is authorized by this delivery.
The four task-owned disposable containers, their anonymous data volumes and the task graph
volume were removed after validation. Existing user containers were preserved; dependency
images remain cached locally for reproduction.
