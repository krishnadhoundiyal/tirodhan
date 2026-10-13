# Increment 6: Hypothesis fleet verification

Base main: bf539300fd43e45add9ec75a8d6bd26058b55f86.
Merged Increment 5 ancestor: b73d2527387e0e30abf81f323336ad9aa26793af.
Branch: codex/v2-hypothesis-verification. Date: 14 October 2026 (Asia/Kolkata).

## Implemented verification boundaries

Hypothesis 6.168.5 is pinned in the development extra only. No production dependency,
service, infrastructure, runtime activation or schema change is introduced. The one
production correction is bounded economic refinement in routing_operations/economics.py.
Existing OSRM 6.0.0, VROOM 1.15.0 and OR-Tools 9.14.6206 pins remain unchanged.

The default fleet-ci profile uses 20 property examples and 12 state-machine steps;
the database history test further bounds itself to five histories, and each race family
to three generated schedules. The extended profile uses 100 property examples,
40 histories of up to 40 steps, 12 schedules per race family and 20 native examples
per engine. Deadline disabling applies only to Hypothesis shrinking/generation;
transaction overlap and solver execution retain explicit time/budget bounds.
No production data enters generators or logs. Hypothesis caches are ignored, and the
read-only native runner places its cache in /tmp/hypothesis. Fixed seeds and the pinned
Hypothesis version provide replay; failure blobs and shrunk action sequences remain
available. The permanent counterexample is a versioned JSON fixture, not a transient cache.

## Stateful services and independent ledger

tests/integration/test_fleet_hypothesis_stateful.py runs a rule-based state machine
through real domain services and PostgreSQL. A dedicated event loop/NullPool harness
resets every generated example and clears adopted fleet facts on teardown. It does
not wrap the entire history in a rollback transaction: every successful command
actually commits, including idempotency, activation, attempts, custody and outbox facts.

The ledger starts with known synthetic paid-booking admission inputs, two committed
vehicles/riders and two independently authorized receiving points. It tracks expected booking state, owner, collection/vehicle attribution,
validated handover, trusted physical reservation, task start/completion/commitment,
command replay identities, immutable appointment windows and prior revision facts.
Inputs are hydrated before mutations; subsequent effects follow submitted commands and
their results. Proposal choices are checked independently, not predicted by copying
post-activation SQL ownership. After every generated rule, persisted state is compared
with the ledger. Protected destinations are checked directly against the current
revision's first unfinished relational task, not merely a retained historical task row.

Operations include immediate/advance admitted demand, global rolling optimization,
idle/BUSY/loaded movement, departure, appointment start/arrival, successful/unsuccessful
pickup, correct/rejected receiving geography, whole-lot handover, cancellation at
different stages, dated facility restrictions, all four vehicle issue reasons,
manager recovery, shift closure/reopening, solver timeout, cancelled worker/lease
takeover and exact command replay. The cancellation service receives the simulation
clock and errors escape the transaction before they are handled. New bookings are
synthetic paid admission fixtures; this increment does not generate a complete phone,
quotation, provider capture or commercial checkout journey.

Independent checks cover one live owner, stable household identity, conserved collection
attribution until validated handover, outstanding physical load in both capacity
dimensions, service-start appointment bounds, preserved protected/started destinations,
immutable completed tasks/revisions, no cancelled/disputed work silently reassigned,
attempt/handover counts under replay and durable visibility of unallocated obligations.
Complementary generated completion histories force the full pickup-to-handover path,
including rejected receiving proofs. Dedicated issue and equal-timestamp shift histories
prevent rare transitions being left solely to random rule selection.

## Genuine concurrent PostgreSQL schedules

tests/integration/test_fleet_hypothesis_concurrency.py generates initial loaded/idle
states, time deltas and competing launch orders. Its synchronization transaction holds
a real FleetControl, Payment or vehicle row. Each domain command starts in a separate
session. Before releasing the barrier, pg_stat_activity must show every competitor
with an active transaction and wait_event_type='Lock'. Thus sequential coroutine
callbacks cannot satisfy the test. Payment is the synchronization boundary for
cancellation races; resource locks and chained waits cover the other commands.

Fourteen families are executable: two fleet workers, two evaluated proposals targeting
the same booking, revision/departure, revision/collection, cancellation/dispatch,
cancellation/collection, position/publication, cargo/new-booking assignment,
breakdown/reallocation, shift expiry while publication waits, handover/routing,
crashed worker/lease takeover, exact original/replay, and a three-command
publication/departure/position schedule. Loaded worker/assignment races retain existing
cargo and introduce actual new demand. Feasible injected proposals are constructed by
the independent enumerator; native engine correctness is tested separately below.

Assertions allow legitimate domain rejection and SQLSTATE 40001/40P01 rollback, while
requiring unique ownership, complete route/revision identity, preserved collected
facts, no collection/cancellation double winner, idempotent replay, current-time expiry
fencing and recoverable lease takeover. These are repeatable blocked-transaction
schedules, not an exhaustive enumeration of PostgreSQL execution histories. Arbitrary
lock interleavings, networking partitions and operating-system process death at every
instruction remain outside coverage.

## Independent routing oracle and differential testing

tests/fleet_verification/oracle.py imports the public immutable contract only; it never
calls production validation, economics, snapshot projection or adapter normalization.
It exhaustively enumerates omission/assignment of entire optional households and all
stop permutations for small instances. A separate event/cargo simulation checks stop
uniqueness, ownership/eligibility, pickup-before-discharge, initial cargo, dimensional
capacity, customer starts, receiving authorization/opening, service completion,
custody/shift deadlines, protected prefixes, final discharge and return travel.

The reference independently computes coverage/deadline precedence, travel, distance,
waiting, empty travel, added vehicles, incumbent disruption, window risk and generalized
seconds. Strategies vary up to two vehicles, three new households plus one cargo lot,
two capacity dimensions, BUSY/idle ownership, directed shortest-path matrices, service
windows/times, one/two separately authorized facilities, availability and protected work. Generated production proposals
are compared with independent results; arbitrary allocations additionally compare
production acceptance with independent feasibility. Invalid business allocations are
tested as rejected outcomes, never filtered away with assume/filter. Hypothesis's
structurally invalid generation attempts are not discarded domain failures.

Metamorphic checks rename all identifiers and reorder input collections without
changing a fixed proposal's score or feasibility. Native tests execute the actual
pinned VROOM and OR-Tools subprocess adapters on generated matrices, assess their output
with the independent oracle and then check common feasible refinement. Existing
real-road OSRM operational/custody tests remain in the expanded native runner.
No test asserts that a general heuristic always reaches the global optimum.

## Discovered production defect and permanent shrink

The initial adaptive extended audit observed generalized-cost gaps up to 95 test
seconds at equal coverage. Hypothesis find/shrinking reduced a representative defect
to two empty vehicles of capacity (1,1), two whole lots of load (1,1), zero service
duration, pickup windows [0,10], an open authorized facility, and activation cost one.
All other penalties are zero. The directed shortest-path matrix is:

| From / to | V0 start | V1 start | P0 | P1 | receiving |
|---|---:|---:|---:|---:|---:|
| V0 start | 0 | 1 | 1 | 2 | 1 |
| V1 start | 1 | 0 | 1 | 1 | 1 |
| P0 | 1 | 1 | 0 | 1 | 1 |
| P1 | 1 | 1 | 1 | 0 | 1 |
| receiving | 1 | 1 | 1 | 2 | 0 |

The old empty-seed search assigned P0/discharge to V0, then P1/discharge to V1.
Travel was four seconds/12 metres, with two activations and total score six.
The independently enumerated alternative uses V1 for P1/discharge/P0/discharge:
the same travel/distance, one activation and score five. Both meet every hard constraint.
Accounting was correct; allocation quality was inferior because the single insertion
sweep never revisited P0 after adding P1, and block consolidation had already run on
the empty seed. This is a focused refinement implementation defect, not an unsafe
validator result or a production price claim.

Refinement now repeats feasible block/pair sweeps until no strict improvement or the
existing total candidate budget is exhausted. It does not reset or enlarge that budget,
permit uphill moves, move protected/custodied work or substitute a second assignment
authority. The preserved fixture single_sweep_extra_vehicle.json and unit/native
regressions independently verify score five/one activation on the exact shrunk case.
The source change is confined to economics.py. Financial/custody/dispatch mechanisms
and migrations are unchanged.

Post-fix extended exploration with seed 61006 checked 100 small instances and observed
no economic/coverage gap in the feasible instances checked. This is adaptive exploration,
not a claim that the pre/post runs visited identical cohorts or that larger search is
optimal. In the seed-61008 native sample, each engine checked 20 examples: 17 feasible
and three independently infeasible. At equal coverage/deadline priority, maximum raw
VROOM gap was zero and OR-Tools gap six; common refinement reduced both to zero.
Those raw engine differences are optimization-quality limitations of duration-oriented
construction under the supplied calibration, not accounting or safety defects.

A further post-fix seed (61010) observed a maximum generalized-cost gap of 28 in its
100-example campaign. Its separately shrunk strict_local_minimum.json contains one
vehicle and three new whole lots: bounded refinement uses six travel seconds/18 metres,
while the independent feasible alternative uses five seconds/15 metres. Both serve all
lots and wait seven seconds; all penalties are zero. The local route has one second
minimum window slack, versus zero for the optimum. This is a strict-neighborhood quality
limitation requiring coordinated order changes, not an accounting or safety defect.
Its permanent regression accepts improvements, checks independent feasibility/cost,
and rejects deterioration beyond the observed baseline. The reference additionally
enumerates every single whole-lot relocate around the final one-vehicle route and proves
none improves its rank. Both native engines construct a seed that reaches score five
on this fixture, illustrating why the architectural combination can escape a poor
standalone refinement seed. No broader search or production calibration is silently introduced.

## Verification failures corrected in the test infrastructure

The strict overlap assertion caught cancellation finishing before a vehicle lock;
cancellation races now synchronize at Payment before checking eligibility. A sample
proposal stub ignored protected receiving/ownership, so race proposals now use the
independent feasible enumerator. Equal created_at timestamps made the history driver
select a closed shift over the open identity; the driver now selects open state first
with a stable ID tie-break, with a permanent repeated-shift regression. Rejected
cancellation is handled after transaction rollback. Example teardown originally left
adopted fleet facts, and the old migration test correctly refused downgrade; cleanup
and a permanent isolation test fix that harness error without weakening history guards.
Arbitrary allocation shrinking also found an oracle representation error: an empty
list slice was compared directly with a tuple protected prefix. Sequence normalization
and a permanent list/tuple equivalence regression fix the reference; production
correctly accepted the empty optional route. This was not a production validator defect.
An initial second-database setup lacked PostGIS; its setup errors were resolved by
installing the required extensions. These are not claimed as production defects.

## Reproduction and executed validation

Default CI (disposable PostgreSQL/PostGIS, Python 3.12, default API/dev dependency profile):

```powershell
python -m pip install -e ".[dev]"
pytest -q --hypothesis-seed=61011
ruff check .
ruff format --check .
mypy
```

Extended histories/races (same disposable database, run serially because examples reset data):

```powershell
$env:TIRODHAN_HYPOTHESIS_PROFILE = 'fleet-extended'
pytest -q -s tests/integration/test_fleet_hypothesis_stateful.py --hypothesis-seed=61009
pytest -q -s tests/integration/test_fleet_hypothesis_concurrency.py --hypothesis-seed=61013
pytest -q tests/unit/test_fleet_differential.py --hypothesis-seed=61012 --hypothesis-show-statistics
```

Native/road/operational reproduction (fresh dependency image required for Increment 6):

```powershell
powershell -NoProfile -File tests/routing/run-operational.ps1
```

The runner uses image increment-6, exact source mounted read-only, seed 61010 and the
unchanged fixture/engine pins. -SkipBuild is valid only after building the Increment 6
dependency image with Hypothesis. It creates/removes its database/server/graph volume
without host ports. Optional native generated exploration uses
TIRODHAN_ROUTING_ENGINE_TESTS=1, the extended profile, seed 61008 and the same pins.

The offline quality audit is explicitly invoked with PYTHONPATH including src and tests:
python -m fleet_verification.quality_audit <output.json>. It searches/shrinks up to
500 examples, exporting a witness only when one is found, otherwise reporting no
witness in that bounded search. The versioned pre-fix fixture remains reproducible.
Framework API: [Hypothesis state machines](https://hypothesis.readthedocs.io/en/latest/stateful.html).

Executed intermediate checks: extended original state histories passed three tests
in 577.65 seconds; 14 extended race families passed in 463.71 seconds; post-fix
native generated tests/regressions passed four tests in 35.34 seconds; 100-example
accounting/search plus arbitrary-allocation comparisons passed three tests in 14.05
seconds. The final history/isolation suite and subsequent legacy migration passed
nine tests in 97.73 seconds. An earlier complete native/operational run passed 166
tests in 483.28 seconds. Final-source complete/native results are recorded below before commit.

Final-source pinned road/native/operational verification (seed 61010): **176 passed,
no skips/warnings, 611.23 seconds**. This includes all six native generated/regression
cases, both facilities, corrected oracle, permanent minimized examples, generated
PostgreSQL histories and all 14 lock-confirmed race families with explicit loaded examples.

Final-source extended two-facility history campaign (seed 61019): **8 passed in
713.93 seconds**. The rule machine explored 40 histories with up to 40 steps, and the
same suite completed the four issue reasons, whole-cargo completion, timestamp identity
and teardown regressions. No ownership, custody, scheduling or protected-prefix mismatch
was found in this final campaign.

Final-source complete repository suite (Python 3.12.15, seed 61011): **1,313 passed,
32 native opt-in skips, four existing Firebase MulticastMessage.tokens deprecation
warnings, 875.53 seconds**. The separate 176-test native run covers all opted-out
native cases; counts overlap and must not be summed as unique tests. Disposable
PostgreSQL 17.5/PostGIS 3.5.2 includes topology/TIGER/fuzzystrmatch. Migration upgrade/
downgrade and financial-trigger/concurrency tests pass. Strict schema comparison retains
only the two separately checked pre-existing migration-0007 application indexes omitted
from ORM metadata; no new application-schema drift or schema change is introduced.
Ruff check, format check (364 Python files), strict mypy (178 source files), pip check
and git diff --check pass. OR-Tools is absent from the default API profile.

The final history campaign recorded 82 activated cycles, 86 starts, 61 pickup
submissions, 29 discharge submissions, 204 simulated worker crashes, 126 exact replays,
99 new admitted-booking inputs and all four issue reasons. These are executed operation
counts in that campaign, not unique requests or guarantees of uniform random coverage.
Examples clean up their application data; task-owned containers/graphs/databases are
removed after validation, with existing user services preserved. Production flags remain
disabled. No push, PR, merge, deployment or subsequent increment is part of this delivery.

## Limits and remaining work

The independent oracle is deliberately exponential and limited to small whole-lot
instances with one/two receiving facilities per generated problem, two dimensions and no
physical cargo transfer. It does not verify arbitrary split-discharge contracts,
many road profiles, large-city performance or global heuristic optimality. Live traffic,
GPS hardware attestation, generated authorization/session revocation permutations,
provider cash flows, push transport failures and actual process/container power loss
are not fully generated here; existing deterministic protection remains in place.
Booking admission is synthetic paid SQL input and position history uses authorized
manager reports; a full mobile/customer/provider simulator is not claimed.

The eight-test final multi-facility history suite passed in 66.33 seconds; the expanded
two-facility accounting/search/arbitrary-allocation suite checked 100 examples per
property and passed five tests in 8.66 seconds. The final extended native generated
suite and both counterexamples passed six tests in 17.08 seconds. These focused counts
overlap the complete suites; they are not unique additional tests.

History and race suites reset a disposable database and cannot run concurrently against
that same database. Use separate disposable databases for parallel invocations. Profiles
and seeds bound work but are evidence, not exhaustive proof of liveness or all schedules.
Deeper shrinking of database histories can be expensive. Long campaigns and larger
scope/calibration benchmarks remain local/ephemeral operational decisions, not new
recurring infrastructure. The approved Increment 5 recovery, scheduling, notification,
flexible supplementation and V1 retirement gaps remain; none is silently enabled here.
