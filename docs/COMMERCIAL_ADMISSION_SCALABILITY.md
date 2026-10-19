# Commercial admission scalability implementation

Base: `a090f0fa3b450e71e0311023af33c32e45767219`.
Branch: `codex/commercial-admission-scalability`.
Scope: the approved commercial-admission workstreams A–I. No production flag,
deployment, paid provider, database service, broker or compute platform is added.
The OSRM negative-distance defect remains outside this assignment; negative output
still fails closed. This report supersedes the population-limit discussion in
`ROUTING_RELIABILITY_AND_ADMISSION_SCALABILITY.md` for commercial preparation.

## Implemented pipeline

The existing authenticated, idempotent admission intent is the logical command.
One finite regional writer prepares several intents under a durable PostgreSQL
lease. Work outside transactions uses an MVCC regional read model, strict directed
road acquisition, exact fixed-order pair insertion and, when needed, both pinned
native engines over complete-route neighbourhoods. The independent validator
certifies the witness. A short SERIALIZABLE transaction fences the region, source
head, intent lease and regional lease, rechecks live customer/serviceability terms,
and publishes the reservation and immutable route certificates atomically.

Checkout, authenticated payment capture and existing cancellation/custody/dispatch
services retain their authority. An admission allocation is a capacity witness,
not a rider assignment. The finite `routing_admission` entry point does not call
dispatch or require the dispatch feature flag to be enabled.

| Workstream | Actual implementation |
|---|---|
| A: pair insertion | `planning/routing/scheduling.py`; earliest prefixes, physical loads, exact calendar inverses, latest suffix bounds and resumable `(vehicle, pickup gap, discharge gap)` |
| B: validator | `planning/routing/validation.py` and `certificates.py`; compiled lookups, constant-dimension load deltas, independent per-route proofs and global coverage |
| C: road acquisition | `routing_operations/admission_roads.py`, OSRM provenance tokens, wrapper forwarding; sparse directed V3 matrices and bounded local dense neighbourhoods |
| D: worker | `admission_jobs.py`, `admission_leases.py`, `workers/routing_admission.py`; finite regional ownership across intents, FIFO eligible claims, durable yields/backoff and lease takeover |
| E: state/history | `admission_state.py`, `admission_store.py`, `admission_models.py`; reusable source supersets, leaf deltas, immutable routes, temporal memberships and stable obligation aliases |
| F: native models | `preparation.py`, `adapters.py`, `ortools_worker.py`, `admission_native.py`; physical dimensions, explicit lot ownership/precedence, bounded complete-route repair |
| G: database | migration 0030; regional due index, indexed relational certificates, transactional/coalesced source notices, source-head fencing and array identity parameters |
| H: numerical limits | commercial projections use actual population sizes; old 128/32/512/8 limits remain only for legacy bounded modes; execution bytes/time/native-format limits yield UNKNOWN |
| I: failures | `admission_failures.py`; invalid commands, no declared resource, unknown search and source/infrastructure failures are distinct |

Module paths in the table are relative to `src/tirodhan/modules/`, except `workers/`.

## Mathematical model and exact insertion

Let a route contain `n` existing stops, with immutable protected prefix length `k`.
`p` is one new household pickup and `d` its complete discharge. A legal insertion
chooses gaps `k <= i <= j <= n` and preserves every old stop's relative order.
Physical load dimension count is `D`; household identities remain separate from
the physical vectors. For load component `r`, the new carried quantity is `q[r]`.

For an action lasting `u`, a break `[a,b)` excludes integer action starts
`[a-u+1,b-1]`, matching the existing `avoid_breaks` predicate including zero-duration
actions. Service-start domains intersect appointment windows, facility openings,
and discharge deadlines, then subtract those break exclusions. Sorted disjoint
intervals support exact `next(t)` and `previous(t)` by binary search. Waiting is
permitted; a road leg or service cannot pause inside a break.

For an old prefix ending at node `x` at time `E`, forward propagation to stop `s` is:

```
departure = next_allowed_travel_start(E, travel[x,s])
arrival   = departure + travel[x,s]
start     = next_allowed_service_start(s, arrival)
E_next    = start + service[s]
L_next[r] = L[r] + signed_quantity[s] * unit_load[lot(s),r]
```

Reverse propagation applies the exact inverse domains to find the latest feasible
arrival at each old suffix entry, including the mandatory end journey. At pickup
gap `i`, insert `p`, then scan discharge gap `j` forward. The carried old prefix is
propagated once for that `i`, rather than replaying it separately for every pair.
Check `0 <= old_prefix_load[r] + q[r] <= capacity[r]` while `p` remains onboard.
After trying `d`, its arrival at the unchanged old suffix must be no later than the
independently computed suffix bound.

If the carried prefix misses a window or exceeds capacity at an old stop, every
later `j` for that same `i` contains that same invalid carried prefix. Pruning it is
therefore sufficient. Failure at one pickup gap never prunes later pickup gaps.
No triangle inequality, distance symmetry, geometric proximity or monotonic
insertion-cost assumption is used. Directed nonmetric matrices are covered by
exhaustive-comparison tests. A search witness still passes the independent gate;
search summaries are never accepted as proof.

The cursor records the next untested pair. A quantum yield is UNKNOWN and retains
the cursor durably on that admission intent. Two yielded intents cannot overwrite
each other; regional ownership is a separate row. Resume reconstructs the carried prefix; it does not trust
serialized scheduling arrays. Intent creation time anchors the projection across
retries while actual availability advances. Source/generation/graph/horizon
changes invalidate the cursor. Backoff lets another eligible intent run. This is
not a starvation guarantee under continuously changing source state.

### Independent certificates

The validator compiles stop/lot/facility/matrix/node indexes once and updates loads
by signed `D`-vectors. It retains per-lot balances, pickup-before-discharge,
authorization, owner, prefix and complete-discharge checks. Initial-cargo ownership
and complete input structure are checked globally.

`compose` checks the full problem and exact global stop coverage/uniqueness, then
reuses an unchanged route proof only if its canonical hard terms and used directed
arcs have the same SHA-256 fingerprint and current availability is no later than
its independently calculated latest valid entry. SHA-256 equality has the same
cryptographic collision assumption as existing command fingerprints. The proof's
old assessment is historical evidence, not a live departure schedule. Changed
routes are revalidated independently. The backward proof implementation does not
call the insertion calendar implementation.

The product composition is valid under the approved **unlimited commercial
unloading-throughput assumption**: there is no shared facility concurrency quota
coupling otherwise feasible routes. Facility authorization, availability, opening
windows, service duration and custody deadlines still apply. This assumption is
not a silent change to operational handover authority.

## Road acquisition and native repair

`DYNAMIC_COLLECTION_ROUTING_V3` adds sorted sparse `RoadArc` tuples. A missing arc
is unknown/unacquired, never an invented unreachable or zero-cost arc. V1/V2 dense
contracts remain supported. The admission projection requests incumbent adjacency
arcs, newly connected cancellation legs, and both directions between new stop
nodes and each eligible route's nodes. Different profiles remain separate.

The finite directed cache uses exact coordinate pairs, graph/profile/routing-
profile/endpoint/snap-policy provenance, opaque hashes of source/destination OSRM
snapping attestations, packed signed 64-bit values, and bounded LRU state. The
internal `-1` encoding represents a provider null; it is never returned as a road
measurement. Oversized integer values are not cached. Unattested providers are
queried afresh. Endpoint graphs must remain immutable for their attested version,
as in the existing provider contract.

Uncached pairs form a directed graph. A degree-priority heap groups incident
pairs into square queries of at most 32 nodes (or the provider's smaller bound).
All requested directed cells in a block are retained. This batches both inbound
and outbound stars; it does not maintain a dense regional table. Every raw square
response still passes existing full matrix checks before any cell is retained.
Negative distances anywhere in a response, bad snapping, provenance changes,
unreachable arcs and timeouts cannot manufacture a certificate.

Native repair retains whole incumbent routes and their cargo/prefix closure.
Dense neighbourhood growth is governed by bytes, not a regional stop count. Both
VROOM 1.15.0 and OR-Tools 9.14.6206 are tried in each explored neighbourhood. A valid
local witness is combined with unchanged outside routes and independently checked
for the entire commercial obligation set. A cold legacy population without a
complete seed requires joint coverage; it is not silently split into independently
successful partial certificates.
Cold commercial preparation also retains live operational incumbent orders in
its seed. The full-suite appointment-retry regressions exposed that an empty
seed could omit required routes outside a local repair neighbourhood. Those
incumbents now remain in both the repair closure and the composed outside set;
all existing coverage, prefix and independent validation checks still apply.

Native payloads contain `D` physical load dimensions rather than `D + lots`.
VROOM uses paired shipments and vehicle-specific skills for initial cargo.
OR-Tools uses explicit same-vehicle/activity constraints, pickup/delivery pairing,
and a strict visit-rank dimension; zero travel/service times therefore cannot
permit discharge before pickup. Initial cargo remains bound to its real owner.
The 30-pair zero-travel/zero-service regression exposed a feasible model for which
OR-Tools' insertion constructor returned no witness. Its standard
PARALLEL_CHEAPEST_INSERTION constructor now has a GLOBAL_CHEAPEST_ARC fallback
when time remains, sharing the original total search budget and all constraints.
This improves witness construction; it does not turn missing output into an
infeasibility proof or change the independent acceptance gate.
VROOM's existing unsupported split-new-lot restriction is unchanged. Every output
still passes the independent validator. Composite admission provenance identifies
insertion or the native contributor; it never falsely labels insertion as a
native-engine execution.

## State, transactions and recovery

The source cache retains a 48-hour **read superset** around a stable UTC anchor,
then projects the original per-command routing horizon. The routing problem still
spans at most 24 hours and retains each booking's original deadline. This removes
cache thrashing between appointment slots without changing business terms.
Unchanged sources require no full rebuild. Request/reservation/manifest leaf
changes requery those identities and relational aliases. Leaf delta capture still refreshes the regional resource/custody projection;
it avoids rereading the complete request population. Other changes rebuild the
full closure. Expiry boundaries force reconstruction. Cache eviction,
process death and lease takeover all have the same cold reconstruction path.

After transactional mutations on 45 source tables, database triggers resolve FK-based regional
dependencies, update a separate `admission_source_head`, and upsert an identifier-
only invalidation. Repeated telemetry changes coalesce by `(region, table, key)`;
this is a dirty set, not an audit history. Unresolved/global dependencies invalidate
conservatively. Normal known resource and execution dependencies remain regional.
The head is separate from immutable region configuration and from operational
assignment locks.

Capture and its notice-set fingerprint share one REPEATABLE READ snapshot. Final
publication locks the source head in SERIALIZABLE isolation and compares the exact
visible notice identities plus region generation. A source transaction using READ
COMMITTED still updates the locked head: if it committed after the publisher's
snapshot, the publisher cannot lock the changed row successfully; if publication
already holds the head, the source writer waits. Tests exercise both orders using
two actual PostgreSQL sessions. Consuming exactly the visible dirty set avoids
sequence/UUID allocation-order watermarks and lost late commits.

The regional preparation lease spans successive intents; the existing partial
unique RUNNING-intent index remains a second protection. Publication verifies both
unexpired tokens. Durable cursors contain identifiers, versions, positions and
fingerprints only. No database transaction is held across OSRM/native work.
Source locks, certificate history and the reservation commit together. Checkpoint writes fence both intent and regional tokens, with a nonblocking intent
lock to avoid a claim/lease lock inversion. Terminal/expired commands clear their
checkpoint; yielding commands retain it across regional owner release. A crash
after commit replays the same certificate; a lost lease cannot publish later.

### Schema 0030

Nine relational tables are added; `capacity_admission_request.preparation_cursor`
adds per-command checkpoint metadata to the existing durable intent:

1. `admission_plan_revision`: immutable regional revision and source proof stamp.
2. `admission_route_certificate`: immutable per-vehicle/shift route proof.
3. `admission_route_membership`: temporal membership; one current route per vehicle.
4. `admission_obligation`: stable reservation/request identity with bind-once aliases.
5. `admission_allocation`: current advisory obligation-to-vehicle witness.
6. `admission_proof_obligation`: immutable proof-to-obligation FK coverage.
7. `admission_preparation_lease`: durable regional token and expiry.
8. `admission_source_notice`: coalesced identifier-only dirty set.
9. `admission_source_head`: transactional source-generation lock/fence.

Real FKs, temporal checks, unique active membership, composite proof-owner FKs,
immutable-history triggers and bind-once obligation checks protect the representation.
`ix_admission_regional_due` adds the region prefix to the existing intent access
path. Large identifier predicates bind one PostgreSQL array rather than exceeding
asyncpg's expanding-parameter limit. No financial table/terms trigger is changed.

Legacy full-proposal certificates remain readable. New reservations contain a
small versioned envelope; route memberships reconstruct the exact historical
revision. Only changed route proofs/memberships are written. Old workers must drain
before upgrade; migration refuses RUNNING preparation. Empty-new-history downgrade
preserves populated legacy financial/admission data. Populated new proof history
refuses destructive downgrade, consistent with existing retained-history migrations;
rollback after such publication requires retaining/exporting that history and an
explicit compatible rollback design, not deleting commercial evidence.

## Bounds and complexity

The old pair enumerator ran a whole-proposal gate inside each pickup/discharge
pair. If that gate costs `G`, its search cost was `O(sum(n_v²) * G)`, including
unchanged routes. Repeated whole-cargo summation could contribute `O(S L D)`
for `L` lots; tuple node-membership checks could contribute `O(E N)` for `N`
nodes. Projection also repeated vehicle searches inside eligibility construction
(`O(S V²)`). Compiled indexes, incremental loads, shared eligibility construction
and the route-local scheduling kernel remove those repeated traversals.

Regional dense road acquisition used `O(P N²)` retained cells per problem;
sparse acquisition retains only requested directed arcs and bounded reusable cells.
Native physical vectors previously grew with `D + L`; they now use `D`, with
separate linear identity/pairing constraints. Full proposal JSON duplicated the
fleet order in each reservation. Shared route proofs avoid that duplication when
canonical hard terms remain equal. This is conditional sharing: epoch/horizon or
other hard-term changes can invalidate multiple proofs. Worst-case proof-history
growth is not claimed linear in bookings. Regional input/coverage and source
fingerprints still require the work shown below.

Let `S` be all stops, `V` resources, `P` profiles, `A` acquired directed arcs,
`E = sum(stop eligibility sizes)`, `B` breaks per route, `W` service windows and
`H` the maximum disjoint calendar interval count. `n_v` is stops on route `v`.

| Stage | Cost and qualification |
|---|---|
| Global input indexes/structure | `O(S + V + A + E)` expected hash/set work, plus canonical sorting; matrix-node membership no longer scans node tuples for every eligibility |
| Calendar construction | Per distinct action domain `O(B(W+B) + (W+B) log(W+B))`; departure domains are memoized by duration |
| Fixed-order pair search | `O(sum(n_v²) * (D + log A + log H))` after calendar/index preprocessing; first feasible witness may stop early |
| Resume | Rebuild route summaries and at most one carried prefix; no claim of constant-time resumption |
| Independent route/load validation | Linear in visited stops times `D`, plus break/window scans and sparse arc lookups; no repeated whole-cargo summation at every stop |
| Proof composition | Global structure/coverage remains proportional to represented inputs; route hashing/used-arc comparisons still touch route content; changed routes receive full independent scheduling |
| New-stop road stars | `O(N)` requested pairs per profile/route closure; incident batching uses heap work `O(A log N)` and approximately `O(N b)` returned cells for fixed block width `b`; arbitrary missing-pair patterns need not be stars |
| Dense native neighbourhood | `O(P_local m²)` matrix memory and model construction plus constrained routing search; no polynomial optimality claim |
| Publication | Shared route writes proportional to changed routes/obligations; reading current membership/coverage and proof hashing still costs regional linear work |
| Cold recovery | Complete MVCC closure and witness reconstruction are more expensive than warm leaf maintenance; accepted commitments cannot be omitted to improve timings |

Road retention is at most 32 MB across configured profiles, with at most 4 MB per
profile. The read-cache conservative charge is capped at 64 MB per database engine.
A sparse acquisition scratch estimate above 128 MB yields
`UNKNOWN_MATRIX_MEMORY_BUDGET`; a dense local model exceeding its byte budget yields
`UNKNOWN_MODEL_MEMORY_BUDGET`. These are hardware/execution safeguards, not booking
limits. Charges are conservative accounting estimates, not exact Python RSS limits;
native processes, graph memory and temporary projections also consume memory.
Cursor quanta bound search effort; finite jobs bound intent count/wall time and
continue remaining work in later invocations. Native integer/response-size/time
limits remain explicit. Regional memory and global hashing are not constant-time.

No commercial code rejects a region because it has 129 bookings, 33 vehicles,
513 nodes or 9 profiles. Legacy bounded execution still has its previous guards.
Removing those regional business ceilings does not imply unlimited hardware capacity.

For one regional serialized writer with mean full service time `s`, sustained
arrival rate must satisfy `lambda * s < 1`; bursts queue durably. Scaling regions
adds independent lanes. Extra same-region speculative writers do not increase the
authoritative publication rate. Backoff, cold starts and native repair increase
`s`; p95 latency includes queueing. Benchmark throughput is not a production SLA. The measured fleet uses future
declared shifts and steady positions. Sustained live telemetry/cargo churn at
production scale is not benchmarked: strict source fencing may invalidate
preparation repeatedly. Functional tests cover those races; they do not establish
a live-fleet throughput guarantee. No fencing or customer promise was weakened
to improve these measurements.

## Failure semantics

Malformed/expired commands, invalid live authorization and changed customer context
are terminal command failures. An empty declared resource set is distinguished
from search failure and can activate existing voluntary flexible-capacity offers.
Neither exact insertion exhaustion, neighbourhood failure nor a native solver's
missing witness proves global physical infeasibility. Those remain UNKNOWN/PENDING.
Provider errors, invalid proposals, configuration/source readiness and transaction
conflicts retry durably with bounded backoff until technical intent/slot expiry.
There is no new promise without a valid final witness, and an older accepted
booking is never erased because a later candidate cannot be admitted.

## Validation and benchmark evidence

Aggregate campaign results are in
`benchmarks/COMMERCIAL_ADMISSION_SCALABILITY_RESULTS.json`.
The benchmark invokes the real authorized/idempotent admission service and durable
worker, then real checkout/payment services with authenticated synthetic capture
events. It does not bulk seed accepted bookings or reservations. It uses public
clipped OSM fixtures, declared synthetic future shifts and varied mass/volume,
70% first-two-slot concentration with the remainder spread over later slots.
Larger capacity-scaled populations explicitly report the number of vehicles.
The public fixture has 64 road sites, reused by distinct synthetic household
identities in larger populations. It is a concentrated regional demand test,
not evidence for 512 distinct city-wide road sites or a production Delhi graph.
Road/solver dependency startup and fixture creation precede timed admission;
the first commercial certificate is cold, subsequent certificates reuse the
same finite process. Production graph cold-start and larger-area costs need
environment-specific calibration.

CPU uses container cgroup deltas divided by the declared two-core allocation;
database counters may lag PostgreSQL's asynchronous statistics flush. Database
memory is end-of-run container usage, including cache. Application peak RSS is
the Python test process, excluding graph/solver child RSS; application container
CPU includes those children. SQL timing includes polls and checkout/capture.
Rollback counters include ordinary read-session rollback and are not failure
counts. The harness submits customer intents sequentially, then runs four concurrent
consumers; simultaneous commercial mutations are covered separately by database
race tests. Decision quantiles include enqueue-to-observed-terminal queueing;
throughput includes successful checkout/capture. These are single-run local
measurements, without confidence intervals or an imposed production arrival rate.

Final validation on the completed checkpoint schema:

| Campaign | Actual result |
|---|---|
| Complete repository, Python 3.12 CI dependency image, fresh PostgreSQL/PostGIS | **1,395 passed, 53 skipped, 5 warnings; 770.95 s** |
| Expanded pinned routing/admission/operational suite, both profile graphs, seed 61010 | **226 passed, no skips, 2 warnings; 216.97 s** |
| Targeted checkpoint/admission/orchestration/custody suite | 50 passed, 1 native-only skip, 1 warning; 93.14 s |
| Ruff / formatting / strict mypy | Passed; 436 files formatted, 214 source files type checked |

The standard run skips explicitly gated native/benchmark tests and modules requiring
optional routing dependencies. Relevant native and routing-extra skips were executed
in the expanded campaign, including regional activation and populated migrations.
Unrelated orchestration performance campaigns remain skipped. The commercial
benchmark is executed separately below. Warnings are the existing SQLAlchemy
single-row cartesian location comparison and Firebase multicast deprecations.

No application drift was introduced by 0030. Populated downgrade/upgrade and strict
catalog-filtered metadata comparison passed. The only application differences remain
the two pre-existing migration-0007 collection-request indexes, each explicitly checked
for columns, method, uniqueness and predicate. Extension-owned relations are excluded
by the existing catalog filter. PaymentAttempt defaults/schema and all three
initiation-term triggers remain explicitly verified; no new differences were ignored.

Development failures were corrected rather than omitted. The zero-time 30-pair
OR-Tools witness failed until the bounded standard-constructor fallback was added.
A partial full run exposed four failures: two obsolete native-call/error expectations
and two appointment/cancellation cases caused by an empty cold incumbent seed.
The completed full run passes them. A subsequent two-intent regression failed
reproducibly when B replaced A's shared checkpoint. Per-intent checkpoints now pass
that regression, cold recovery and owner fencing. Earlier completed runs (1,394
standard / 118 focused tests) predated that last correction and are superseded here.
One missing list annotation and an async filesystem probe were corrected before
final static checks. Initial benchmark instrumentation failed before timing because
Docker Desktop uses cgroup v1; the opt-in harness now supports v1 and v2 counters.
Interrupted development runs are not counted as completed full-suite passes.

## Final benchmark results

All nine explicit benchmark tests passed. The five corrected-backend campaigns
require every booking to succeed: **1,024/1,024 workflows accepted across isolated
runs**, with zero final failures/pending work. The old 256/40 run intentionally
records its rejected workload (EXPECT_ALL=0); zero accepted throughput is not
reported as successful admission performance.

| Backend/workload | Bookings | Vehicles | Accepted | End-to-end s | Accepted/s | Decision P50/P95 s |
|---|---:|---:|---:|---:|---:|---:|
| Base | 64 | 20 | 64 | 40.91 | 1.56 | 9.69 / 30.28 |
| Final | 64 | 20 | 64 | 18.84 | 3.40 | 5.98 / 12.29 |
| Base | 128 | 20 | 128 | 118.72 | 1.08 | 37.37 / 97.52 |
| Final | 128 | 20 | 128 | 34.72 | 3.69 | 13.21 / 22.78 |
| Base | 256 | 40 | 0 | 13.36 | 0.00 | 6.54 / 9.29 |
| Final | 256 | 40 | 256 | 103.66 | 2.47 | 37.18 / 78.49 |
| Final | 512 | 80 | 512 | 285.11 | 1.80 | 98.77 / 227.20 |
| Base interleaved | 64 | 20 | 64 | 40.13 | 1.59 | 12.57 / 34.16 |
| Final interleaved | 64 | 20 | 64 | 25.45 | 2.51 | 10.34 / 21.39 |

The old 256/40 workload failed all intents with VEHICLE_SCOPE_LIMIT. It does
not establish physical infeasibility. The final backend accepts that declared
capacity and the 512/80 workload, crossing the former request, vehicle and global
node limits. Nine-profile runtime scaling was not benchmarked; that count guard
is removed, with active-profile byte budgets retained.

| Backend/workload | App CPU s | DB CPU s | DB average % of 2 cores | SQL calls | Python peak MiB |
|---|---:|---:|---:|---:|---:|
| Base 64 | 34.98 | 2.81 | 3.43 | 10,645 | 200.45 |
| Final 64 | 12.06 | 2.77 | 7.34 | 12,569 | 196.82 |
| Base 128 | 106.88 | 6.24 | 2.63 | 21,490 | 206.95 |
| Final 128 | 23.61 | 5.57 | 8.03 | 25,780 | 199.98 |
| Base 256 | 8.13 | 2.99 | 11.18 | 7,017 | 188.38 |
| Final 256 | 67.61 | 28.08 | 13.55 | 71,788 | 202.20 |
| Final 512 | 222.63 | 53.73 | 9.42 | 222,133 | 203.59 |
| Base interleaved 64 | 34.07 | 3.40 | 4.23 | 11,276 | 203.79 |
| Final interleaved 64 | 15.17 | 3.74 | 7.34 | 15,509 | 203.58 |

Matched throughput improves **2.17x at 64**, **3.42x at 128**, and **1.58x with interleaved checkout**.
At 128, application CPU falls from 106.88 to 23.61 seconds and DB CPU from
6.24 to 5.57 seconds. SQL calls increase from 21,490 to 25,780 with relational
history/fencing/polling, while measured SQL time falls from 28.82 to 14.93 seconds.
There were no measured deadlocks or temporary-byte spills in any final run.
DB end-of-run cgroup memory ranges roughly 0.75–1 GiB and includes reclaimable
cache; this is not PostgreSQL process RSS or evidence of a memory leak.

Throughput declines at 256/512 as regional inputs, eligibility, hashing and
publication work grow. The 512 result is **1.80/s**, not a claim of unlimited
regional throughput. Its certification-attempt P50/P95 is 0.48/0.72 seconds;
enqueue-to-decision P95 is 227.20 seconds because the burst queues behind one
regional writer. The dataset does not establish a sustained arrival-rate SLA.

Primary final runs use one full source read and 63/127/255/511 warm hits.
All reuse the 64 public road sites: 103 square queries, 59,941 provider cells,
1,742,000 charged road bytes. At 512 the retained read charge is 3,425,672 bytes.
The interleaved case instead performs 74 leaf delta reads, one full read and
four warm hits, demonstrating the paid-binding/invalidation path. It makes 112
road queries / 56,098 cells. Terminal failure/pending fractions are zero;
transient retries are included in elapsed time but not separately counted as
attempt failures. The aggregate JSON retains all timings, counter deltas and
intermediate development runs with their distinct instrumentation caveats.


## Reproduction

Use the existing pinned routing dependency image/graph build described in
`V2_ROUTING_INCREMENT_8.md`; mount current repository sources rather than image
source snapshots. Supply an isolated PostgreSQL 17/PostGIS 3.5 URL. No production
database or copied customer data is permitted.

```
python -m pytest -q -p no:cacheprovider
TIRODHAN_ROUTING_ENGINE_TESTS=1 python tests/routing/regional_entry.py -q tests/routing tests/integration/test_admission_scalability.py -p no:cacheprovider
TIRODHAN_ROUTING_ENGINE_TESTS=1 python tests/routing/regional_entry.py -q tests/integration/test_fleet_hypothesis_native.py tests/integration/test_routing_operations.py tests/integration/test_routing_dispatch.py tests/integration/test_routing_fleet.py -m routing_engine --hypothesis-seed=61010 -p no:cacheprovider
TIRODHAN_ADMISSION_BENCHMARK=1 TIRODHAN_ADMISSION_COUNT=128 TIRODHAN_ADMISSION_CONCURRENCY=4 TIRODHAN_ADMISSION_ADOPTED=1 python tests/routing/regional_entry.py -q -s tests/integration/test_commercial_admission_benchmark.py -p no:cacheprovider
ruff check --no-cache .
ruff format --check --no-cache .
mypy src/tirodhan
```

Environment assignments above use shell notation; use `$env:NAME=...` in PowerShell.
Set `TIRODHAN_HYPOTHESIS_DATABASE=/tmp/hypothesis` on read-only container mounts,
and `TIRODHAN_REGIONAL_GRAPH_DIR=/graphs` for profile/diagnostic native fixtures.
`TIRODHAN_ADMISSION_VEHICLES`, `TIRODHAN_ADMISSION_QUANTUM`,
`TIRODHAN_ADMISSION_INTERLEAVED` and `TIRODHAN_ADMISSION_BUDGET` vary the declared
workload/execution budget; they do not weaken constraints. `EXPECT_ALL=0` is reserved
for measuring the old implementation's explicit failures; default benchmark tests
require every submitted booking to be accepted.

The new finite worker command is:

```
python -m tirodhan.workers.routing_admission REGION_UUID profiles.json --max-intents 256 --max-wall-seconds 120
```

This is an explicit invocation using existing local profile/graph configuration.
It creates no schedule or service, loads a graph only on road demand, supervises
children to exit, and leaves unprocessed intents durable for a subsequent job.
Production invocation/cadence/activation remain subject to existing release controls.

## Algorithm references and boundaries

- Savelsbergh, 1992, *The Vehicle Routing Problem with Time Windows: Minimizing Route Duration*,
  [DOI 10.1287/ijoc.4.2.146](https://pubsonline.informs.org/doi/10.1287/ijoc.4.2.146).
  The implementation adapts forward/backward feasibility reuse, not a claim of
  constant-time arbitrary PDPTW pair evaluation.
- Campbell and Savelsbergh, 2004, *Efficient Insertion Heuristics for Vehicle Routing
  and Scheduling Problems*,
  [DOI 10.1287/trsc.1030.0046](https://pubsonline.informs.org/doi/10.1287/trsc.1030.0046).
  Prefix/suffix preprocessing and carried-prefix reuse remove redundant route scans;
  the calendar-aware paired adaptation and its exact complexity are stated above.
- Gupta, Mumick and Subrahmanian, 1993, *Maintaining Views Incrementally*,
  [SIGMOD Record](https://sigmodrecord.org/1993/06/03/maintaining-views-incrementally/).
  The application uses identity-keyed delete/rederive leaf maintenance and full
  invalidation for wider dependencies; it does not implement a general SQL/Datalog
  view-maintenance engine or claim the paper's optimal counting algorithm.
- [PostgreSQL 17 isolation](https://www.postgresql.org/docs/17/transaction-iso.html)
  and [locking](https://www.postgresql.org/docs/17/explicit-locking.html): MVCC,
  serialization retry, short publication locks and durable fencing are database
  correctness mechanisms, not a distributed consensus service.
- [OR-Tools pickup/delivery](https://developers.google.com/optimization/routing/pickup_delivery)
  and [standard search strategies](https://developers.google.com/optimization/routing/routing_options)
  and [VROOM 1.15 API](https://github.com/VROOM-Project/vroom/blob/v1.15.0/docs/API.md):
  established pairing/ownership constraints replace surrogate identity dimensions.
- [OSRM 6 API](https://project-osrm.org/docs/v6.0.0/api/) and
  [Python OrderedDict](https://docs.python.org/3/library/collections.html#collections.OrderedDict):
  strict table acquisition and bounded memoization. The degree-priority incident
  batching is a work heuristic without a minimum-query or set-cover approximation
  guarantee. It changes acquisition efficiency, never road values or feasibility.

Global mixed-integer optimization, a new distributed writer architecture and an
always-on routing service were not substituted for the approved modular monolith.

## Changed-file manifest

### Documentation

- `docs/ARCHITECTURE.md`
- `docs/COMMERCIAL_ADMISSION_SCALABILITY.md`
- `docs/DATA_PROTECTION.md`
- `docs/DOMAIN_MODEL.md`
- `docs/ER_DIAGRAM.md`
- `docs/IDEMPOTENCY.md`
- `docs/PROJECT_CONTEXT.md`
- `docs/SCHEMA_DESIGN.md`
- `docs/adr/ADR-003-persistence.md`
- `docs/adr/ADR-004-planning-compaction.md`
- `docs/adr/ADR-006-rider-assignment.md`
- `docs/adr/ADR-014-idempotency-reliability.md`
- `docs/benchmarks/COMMERCIAL_ADMISSION_SCALABILITY_RESULTS.json`

### Migrations

- `migrations/env.py`
- `migrations/versions/0030_admission_certificates.py`

### Application modules

- `src/tirodhan/modules/planning/routing/adapters.py`
- `src/tirodhan/modules/planning/routing/certificates.py`
- `src/tirodhan/modules/planning/routing/contracts.py`
- `src/tirodhan/modules/planning/routing/ortools_worker.py`
- `src/tirodhan/modules/planning/routing/osrm.py`
- `src/tirodhan/modules/planning/routing/preparation.py`
- `src/tirodhan/modules/planning/routing/scheduling.py`
- `src/tirodhan/modules/planning/routing/validation.py`
- `src/tirodhan/modules/routing_operations/admission.py`
- `src/tirodhan/modules/routing_operations/admission_failures.py`
- `src/tirodhan/modules/routing_operations/admission_jobs.py`
- `src/tirodhan/modules/routing_operations/admission_leases.py`
- `src/tirodhan/modules/routing_operations/admission_models.py`
- `src/tirodhan/modules/routing_operations/admission_native.py`
- `src/tirodhan/modules/routing_operations/admission_queries.py`
- `src/tirodhan/modules/routing_operations/admission_roads.py`
- `src/tirodhan/modules/routing_operations/admission_state.py`
- `src/tirodhan/modules/routing_operations/admission_store.py`
- `src/tirodhan/modules/routing_operations/economics.py`
- `src/tirodhan/modules/routing_operations/matrix_cache.py`
- `src/tirodhan/modules/routing_operations/orchestration_models.py`
- `src/tirodhan/modules/routing_operations/regional_projection.py`
- `src/tirodhan/modules/routing_operations/rolling.py`
- `src/tirodhan/modules/routing_operations/snapshot.py`
- `src/tirodhan/workers/routing_admission.py`
- `src/tirodhan/workers/routing_regional.py`

### Tests and benchmark harness

- `tests/integration/test_admission_reliability.py`
- `tests/integration/test_admission_scalability.py`
- `tests/integration/test_commercial_admission_benchmark.py`
- `tests/integration/test_operational_orchestration.py`
- `tests/routing/test_admission_certificates.py`
- `tests/routing/test_admission_native.py`
- `tests/routing/test_admission_roads.py`
- `tests/routing/test_admission_scheduling.py`

## Review and release boundary

All approved A–I workstreams are implemented; the implementation does not defer native
model construction, validation, road acquisition, database publication or cold recovery
because another stage improved. The measured performance is for a declared synthetic
future-capacity workload, not an arbitrary city-wide production workload. Existing
source closure reconstruction, global structure/coverage and current membership hashing
still have regional linear cost. Complete-route native repair remains constrained
routing search and may exhaust its budget without a witness. Continuous input churn can
invalidate cursors repeatedly. A cold legacy population without a complete reusable
witness may remain UNKNOWN. These constraints are explicit rather than disguised as
capacity rejection or dropped accepted work.

Implementation details relative to the approved design are the separate SQL source-head
fence (region configuration is immutable), the 48-hour read superset projected into the
original routing horizon, degree-priority incident batching, and a bounded standard
OR-Tools constructor portfolio after a verified search failure. They preserve the
approved model and require no additional infrastructure. Composite provenance explicitly
identifies insertion/native contributions instead of claiming a fictitious engine run.
The approved unlimited commercial receiving-throughput assumption is applied only to
capacity certification, not to custody or operational handover authorization.

This is a local committed implementation for independent correctness/performance review.
It does not authorize production operation. The independently verified OSRM 6 negative-
distance defect remains a provider release blocker; no clamping, substituted distances
or paid service was introduced. Production release also requires environment-specific
capacity/arrival calibration, private graph/profile operations, invocation/cadence and
existing activation/recovery readiness controls. No new infrastructure decision is made
here. No PR, merge, deployment or production flag change is part of this delivery.
