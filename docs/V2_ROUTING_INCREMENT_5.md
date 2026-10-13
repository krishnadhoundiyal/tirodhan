# V2 increment 5: continuous committed fleet

Base: dc8587cc889cf2f0d2fd3ac6e63213592595a50b (main, merged Increment 4).
Branch: codex/v2-continuous-fleet. Implementation/validation: 13–14 October 2026.

## Delivered boundary

The finite `tirodhan.workers.routing_fleet` entry point performs one due collective
optimization tick. A PostgreSQL singleton control row leases ownership of the bounded
committed fleet. Idle, active BUSY and loaded vehicles compete for the same pending
demand across cells and appointment slots. Active legacy assignments must drain;
there is no conversion of their historical groups or custody into new facts.

Both pinned VROOM and OR-Tools are evaluated against the same operational problem and
OSRM matrices. At least one healthy, independently validated engine result is required
before automatic dispatch. Failure of both retains existing commitments and creates
durable attention/retry records. There is no baseline-only automatic dispatch after
total engine failure. Engine output remains a recommendation until the existing
SERIALIZABLE Payment-first authority transaction commits an activation.

`TIRODHAN_ROUTING_FLEET_ENABLED` defaults false and requires the separately disabled
route-execution flag. No production setting, scheduler, deployment or infrastructure
has been changed. The manager policy API supplies explicit versioned calibration;
there are no implicit production prices or GPS thresholds. This is a replacement
increment, not an approval to maintain two permanent dispatch systems.

## Objective and reproducible comparisons

Candidate ranking is lexicographic: minimize unserved household lots, then protect
earlier outstanding pickup deadlines, then minimize generalized seconds, distance and
revision disruption. Generalized seconds are:

```
travel_seconds + waiting_weight * waiting_seconds
 + additional_vehicle_seconds * newly_activated_vehicles
 + disruption_seconds * incumbent_order_changes
 + window_risk_weight * sum(max(0, risk_buffer - service_window_slack))
```

Travel includes pickups, authorized whole-lot discharges and the explicit end journey.
Distance, empty travel, waiting, activation, disruption and minimum window slack are
reported separately. Empty travel is a diagnostic subset of total travel, not counted
twice. Newly activated means a used vehicle with neither incumbent work nor initial
cargo. Monetary cost, road traffic and customer-specific lateness prices are not inferred.
Waiting and risk weights can be zero, but must be explicitly supplied. Approval and
calibration of production coefficients remain release decisions.

Every candidate is measured through the independent feasibility validator. The local
search relocates whole pickup/discharge lots and can transfer an entire movable suffix
block; the latter avoids a local minimum where one transfer loses but consolidation
of several lots saves a second vehicle journey. Search is bounded, not optimal.
Native seeds, incumbent routes, collective insertion and a simple deadline/nearest
feasible baseline are measured on the same current matrix. Baselines are comparison
and validated refinement tools, not a second assignment authority. Raw engine proposals
remain immutable advisory audit; rebased/refined proposals have their own run identity.

Hard constraints remain physical capacity reservations, pickup/service windows,
nominal service time, shift/end obligations, current cargo deadlines, authorized
receiving points and their open windows, whole-lot custody, and explicit committed or
started task prefixes. Optional unallocated demand is debt for a later tick, never a
booking rejection or deletion. Deadline/coverage precedence does not relax feasibility.

The synthetic economic regression uses directed supplied matrices, two available
vehicles and two whole lots. Collective consolidation travels 600 seconds/600 metres
with one newly used vehicle, versus 1,100 seconds/1,100 metres with two for nearest
feasible insertion. Test-only activation cost 300 seconds, waiting weight zero and
disruption cost 10 produce scores 900 versus 1,700. This establishes a counterexample
to nearest selection and checks measured accounting; it is not a city saving forecast.
Real-road results and final validation are recorded below after execution.

Pinned Delhi car-profile road fixture observations (two synthetic households, followed
by a third booking after actual first collection; no production reservations/prices):

| Scenario/candidate | Unserved lots | Travel seconds | Distance m | Newly activated vehicles | Generalized seconds |
|---|---:|---:|---:|---:|---:|
| Initial, VROOM + validated refinement | 0 | 256 | 1,609 | 1 | 556 |
| Initial, OR-Tools + validated refinement | 0 | 256 | 1,609 | 1 | 556 |
| Initial, nearest feasible baseline | 0 | 327 | 2,085 | 1 | 627 |
| Loaded revision, VROOM + validated refinement | 0 | 101 | 635 | 0 | 101 |
| Loaded revision, OR-Tools + validated refinement | 0 | 101 | 635 | 0 | 101 |
| Loaded revision, nearest feasible baseline | 0 | 180 | 1,064 | 0 | 180 |
| Loaded incumbent with new demand unallocated | 1 | 91 | 540 | 0 | 91 |

The loaded proposal serves the additional lot with 10 seconds/95 metres above the
incumbent remaining journey. Coverage is not comparable to the incomplete incumbent
without recording its unserved lot. Initial selected empty travel is 119 seconds,
versus 159 for nearest. Initial selected waiting is 5,149 seconds versus 5,048; the
explicit test waiting weight is zero, so this comparison chooses travel over 101 extra
waiting seconds. Loaded selected waiting is 4,376 versus nearest 4,237. Production
coefficients may change that trade-off. Both unavailable-engine cases reproduce the
same feasible travel/distance via the surviving engine. Collective insertion reaches
the same fixture result; no superiority over a well-designed heuristic or global
optimality is claimed. Local refinement evaluated 15 initial and 23 loaded candidates
per healthy engine; collective baseline evaluated nine and 16 respectively. Fixture
capacity, geographic extent and solver budgets do not establish city-scale performance.

## Movement, authority and revision concurrency

The authenticated rider position API checks the active shift, live rider role/profile,
and unexpired/unrevoked refresh session. It rejects future, stale, nonmonotonic,
insufficient-accuracy and implausibly fast samples. Accepted observations append
PostGIS history with recorder/session/accuracy provenance and advance the resource
version. This is authenticated device-session reporting, not hardware GPS attestation.

Business fingerprints include eligibility, physical manifests, ownership, cargo,
facilities, shifts and route/task facts; they exclude position-only changes. After
native solving, positions are captured again, road matrices rebuilt outside locks and
all candidates independently validated and compared on that current problem.
Publication still fences the exact affected positions and all business facts. Movement
during final acquisition causes a bounded rebase retry using the engine seeds, rather
than restarting both long solver searches. Business changes invalidate the cycle.
Unrelated quarantined vehicle GPS cannot block healthy fleet publication.

The authority transaction preserves Payment -> work-unit -> booking -> rider profile
-> availability -> live identity -> vehicle order, and locks receiving points before
facility eligibility/handover checks. It rechecks elapsed time, current feasibility,
selected economic winner and control version/lease/token. Activation and the terminal
cycle record commit together. Lost/expired owners cannot overwrite a successor.

Uncollected, uncommitted future household ownership can transfer atomically. Old items
are released and their tasks cancelled before new ownership is inserted. PickupExecution
identity, collection attempts, completed history and cargo survive. Empty donors close;
loaded donors retain vehicle binding and BUSY until actual discharge/recovery.

`DEPART` explicitly protects the next destination without asserting arrival or pickup.
`START` also commits it. Prefix protection never relies on geometric proximity.
Old-revision commands can progress the same committed next task when its membership
is proven; uncommitted stale tasks remain rejected. This supports suffix updates while
a rider proceeds on an already committed leg despite delayed route notifications.

Cadence is bounded by minimum/maximum intervals, actual business changes, displacement
from evaluated anchors and increasing straight-line distance from a committed
destination. The last is a divergence heuristic, not polyline deviation detection.
Periodic evaluation supplies time/feasibility review; GPS does not launch a solve for
every sample. Continuous business churn can still invalidate a bounded snapshot.

## Operational failure and custody

Rider issues BREAKDOWN, LOAD_UNCERTAIN, DISCHARGE_DELAY and SAFETY block fresh pickup
and hold the route for recovery. They do not release ownership or fabricate transfer,
measurement or discharge. Managers may explicitly recover ACTIVE or already held
routes: outstanding work is released, while collected cargo remains bound. A resource
clear does not itself resume a route. Resume requires validated authority.

LOAD_UNCERTAIN additionally records immutable household holds. Releasing assignment
ownership cannot make the old manifest trustworthy again, and a generic clear refuses
the dispute. Shadow/manual planning also rejects held manifests. Audited physical
manifest correction and hold resolution are deliberately still missing, so a dispute
requires that later workflow rather than silently recycling unsafe data.

Closed/full/unavailable receiving intervals are relational restrictions intersected
with authorized facility windows. Unsafe incumbents, stale telemetry, capacity/cargo
deadline conflicts and ended shifts produce controlled recovery holds and durable
exceptions. Static impossibility of completing discharge in any remaining authorized
window is checked before road acquisition, so a simultaneous OSRM outage cannot
conceal this known safety conflict. Full capacity is not inferred from approximate cargo, and no unauthorized
alternative receiving point or inter-vehicle custody transfer is selected. Whole-lot
handover uses the existing receiving/photo/custody mechanisms.

Unallocated work remains queryable through a bounded UUID-cursor backlog and cycle
exceptions. Disputed manifests are explicitly marked `load_uncertain` and not ready
even after ownership release. New capacity can recover other debt on a later tick. Solver/acquisition failure,
publication contention and abandoned leases leave explicit durable outcomes.
Route/attention/recovery events use identifier-only transactional outbox records.
Existing polling and protected old-revision execution handle notification delay;
delivery of these new events is not yet wired into a production notification consumer.

| Scenario | Implemented safe outcome | Remaining operational capability |
|---|---|---|
| Incorrect reservation / LOAD_UNCERTAIN | Immutable household hold, fresh pickup blocked, ownership release cannot recycle the disputed manifest | Audited physical measurement/correction and explicit hold resolution |
| Capacity reduced below current cargo | Recovery hold; custody, observed collection and BUSY retained | Safe physical unloading/transfer resolution |
| Full/closed/deactivated receiving point | Static impossible-window hold even during matrix outage; road/time gate checks other conflicts; discharge rechecks actual facility | Authorized alternative destination selection and facility-capacity measurement |
| Discharge leg before a future facility opening | Current DEPART conservatively uses receiving-readiness checks, so it rejects departure while that point is presently closed/restricted | Separate travel-departure eligibility from actual receiving admission; a feasible future window alone currently does not authorize that early leg |
| New demand with loaded vehicle | Both engines/current matrices, suffix reoptimization, vehicle-bound cargo retained | Larger representative fleet calibration |
| Breakdown/safety issue | Resource block and controlled hold; manager can release uncollected work | Audited collected-cargo transfer, repair/continuation workflow |
| Shift end / custody deadline / late discharge | No unsafe normal execution; durable hold and unreleased cargo | Audited emergency/late discharge authority or approved shift extension |
| Failed receiving proof / media delay | Existing rejected-attempt history and custody rules remain; media storage failure does not undo performed work | New fleet consumer/mobile exception UI integration |
| Route notification delayed/absent | Outbox fact retained, polling available, same committed next task progresses from its prior revision | Production publisher/consumer delivery, retries and mobile synchronization |
| No feasible capacity | Unserved debt and reasons retained, later capacity resumes allocation | Operator response/SLA and scale sizing |
| Solver/matrix failure or lost owner | Existing authority retained; explicit failed/stale cycle and successor retry | Scheduling and monitored operational escalation deployment |

## Persistence, compatibility and idempotency

Migration 0026 adds FleetControl, FleetCycle, FleetPositionAnchor, FleetException and
FacilityRestriction, plus vehicle block reason, position accuracy/session, task
commitment and advisory business fingerprint. Core facts use real FKs. Typed policy
and measurement JSON are configuration/audit, not generic entity storage. Positions,
physical manifests, restrictions and exceptions are append-only. SQL protects cycle
identity and terminal history, and task commitment is write-once.

Policy commands use actor/client UUID fingerprinted idempotency plus expected version.
Positions/issues use actor/client UUID, live authorization and transactional uniqueness.
Fleet ticks use the singleton version, unique cycle lease token, expiry and durable
terminal outcome. Activation retains existing run uniqueness and command replay.
Crash before commit rolls back; crash after activation leaves one activation/cycle.
Expired RUNNING cycles are marked stale by a subsequent owner. There is no external
solver/road call while holding operational assignment locks.

Empty-adoption downgrade/upgrade retains populated prior financial and V1 history.
Once new policy, cycle, exception, restriction, committed task or device-position facts
exist, downgrade refuses before destructive operations. Existing schema comparison
uses PostgreSQL extension ownership, keeps the two explicitly checked pre-existing
application-index differences, and detects genuine new application drift.

V1 identifiers, financial business logic, identity rules, cancellation/refund atomicity,
evidence semantics and custody ledger are retained. Default production API dependency
profile does not install OR-Tools. Solver dependencies remain in the optional bounded
worker/test profile.

## Infrastructure, bounds and remaining work

No Redis, database, service, paid road API or cloud resource was added. The finite worker
needs an approved schedule and accessible OSRM profile graph / pinned VROOM binary /
optional OR-Tools dependency when deployed. Hosting, graph refresh, scale-to-zero startup
and schedule frequency remain infrastructure decisions; no recurring cost was assumed.
Matrix storage/acquisition is quadratic per profile. Search is bounded by candidate
evaluations (maximum 20,000), native budgets and a fenced lease. Current ceilings are
128 requests, 32 vehicles, 512 nodes and eight profiles per city-wide scope. This is
not evidence of production capacity at those ceilings. Scope overflow and high churn
need measured sizing/partitioning; cell fragmentation is not silently reintroduced.
Cycle/exception/position history grows with configured cadence; retention, hold-lookup
query sizing and large-history performance need operational benchmarking before rollout.

Recommended next increment: audited exception resolution and release integration,
including physical manifest correction/hold closure, authorized alternate discharge
and custody transfer where approved, durable notification delivery/mobile route sync,
discharge departure versus receiving-admission timing, and measured worker scheduling/cutover controls. Production coefficients, trustworthy
physical reservations, GPS thresholds/retention, end-arrival verification, late-discharge
policy, road profiles and any hosting commitment require release approval. Flexible
acceptance integration and final global V1 admission/drain/retirement remain gaps;
turning off APIs is not a cargo rollback. Production activation remains disabled.

## Validation

Permanent Increment 5 regressions cover collective cross-cell/slot allocation, both
operating/idle winners, whole-block future ownership transfer, actual cargo retention,
movement during solving and final publication, protected departure/old-revision
execution, live session authorization and telemetry quality, one-owner concurrency,
lease takeover, progress/cancellation/refund races, rollback before activation and
crash after its atomic commit, total/partial solver failure, timeout, invalid proposals,
matrix outage, physical capacity/deadline/shift conflicts, load disputes surviving
release, facility outage combined with matrix failure, delayed notification and backlog
recovery. The final focused PostgreSQL run passed 30 tests and skipped three native
opt-in cases in 70.62 seconds. SQL probes also reject manifest/position mutation,
cycle identity rewrite, terminal outcome rewrite and cycle deletion. Empty-adoption
migration round trip and history-preserving downgrade refusal pass.

Validation uses disposable PostgreSQL 17.5/PostGIS 3.5.2 including topology, TIGER
geocoder and fuzzystrmatch extensions. The strict catalog-filtered schema comparator
reports only the two known migration-0007 application-index omissions from ORM
metadata; those indexes are separately inspected for actual correct definition.
There is no new application-schema drift. Prior populated financial-history round
trips and initiation-term trigger/concurrency regressions remain in the full suite.

The complete Python 3.12.15 API/dev profile initially passed 1,282 tests, with 26 native
opt-in skips and four existing Firebase MulticastMessage.tokens deprecation warnings,
in 542.50 seconds. Subsequent strengthened regressions and the static facility safety
correction receive a fresh complete run, whose result is recorded below. `pip check`
passes; OR-Tools is absent from the default API environment.

An intermediate repeat encountered the unchanged
`test_fresh_booking_policy_failure_inactive_category_and_immutable_display_snapshot`
inside the Asia/Kolkata midnight cutoff: it selects tomorrow's first (00:00) slot but
configures a four-minute planning lead, so the API correctly returned
PLANNING_CUTOFF_REACHED instead of the test's expected 201. Its test, booking service,
scheduling and planning-policy files match base main. The same test passed after
midnight in four seconds. No unrelated customer/financial code or assertion was changed.
That superseded run was stopped to test the final facility correction; it is not
reported as a complete passing run.

The real-engine environment uses OSRM 6.0.0 and VROOM 1.15.0 at the existing digest
pins, OR-Tools 9.14.6206 and the public fixture hash documented in tests/routing/README.
The complete native operational runner mounts current source read-only; dependency
image reuse (`-SkipBuild`) is valid because manifests/pins did not change. New tests
execute both real engines and then separately each engine unavailable, with actual
position history updates, activation, collection and loaded revision. Retained native
tests complete receiving proof/custody/evidence to customer completion. The final
native operational run passed all 143 tests without warnings/skips in 266.19 seconds
(4 minutes 26 seconds). This includes the final static facility check and its combined
road-outage regression. The final complete Python 3.12.15 repository run passed
1,286 tests, skipped 26 native opt-in cases and produced the same four Firebase SDK
deprecation warnings in 501.73 seconds (8 minutes 21 seconds), with no failures.
That run includes the final static safety, backlog, failure and SQL history assertions.
The separate native runner covers all the opted-out cases; its count overlaps the
standard suite and must not be added as unique additional tests.

Ruff check and format check pass across the repository (351 Python files formatted);
strict mypy passes for 178 source files, and git diff --check passes. No Terraform or
production deployment change is part of this increment. Test containers, graphs and
databases are disposable; existing user containers are left untouched.
