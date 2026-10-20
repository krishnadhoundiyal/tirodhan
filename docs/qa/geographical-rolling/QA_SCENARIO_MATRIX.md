# Geographical operational acceptance matrix

Base `eefabf5c2174533653f71bdf7e961a039c46b261`; native OSRM 6.0.0/car,
VROOM 1.15.0, OR-Tools routing and CP-SAT 9.14.6206. PostgreSQL 17/PostGIS 3.5.
No production enablement or paid service. Source coordinates are public OSM, never customer data.

| Suite | Geography | New paid arrivals per cycle | Routing horizon | Declared fleet | Rolling cycles |
|---|---|---|---|---|---|
| Primary | West/North/South/East concurrently | 20 each | 7200 s | 10/15/20/25 respectively | 4 per region |
| Extended | Every region × threshold × horizon | 20/40/60 | 7200/10800/14400 s | 10/20/25 by threshold | 4 per combination |
| Adjacent ownership | Separate adjacent logical Delhi regions | Small real booking book | 7200 s | Single-owner resources | Concurrent transfer / planning |
| Hypothesis histories | Generated West/North/South/East public road subsets | Generated small arrivals/geography/cancellation/timing | Generated 7200/10800/14400 s | 2; confirmation and mass capacity varied | Three sampled continuous histories |

There are 36 extended combinations (144 regional cycles), additional to the 16-cycle principal
scenario. The fleet interpretation uses all four requested sizes across scenarios. The extended
test is opt-in separately because it executes real native routing and commercial booking history.

Each new batch purchases real projected holds first, then creates bookings and processes
authenticated **test** captures, including exact capture replay. No accepted booking, operational
assignment or financial success is inserted directly. Synthetic identity, serviceability and
authorized receiving-point inputs are declared fixtures, not claims of live serviceability.

Appointments are distributed over twelve distinct half-hour slots starting 09:30 next local day.
An eight-hour confirmed shift and nine-hour receiving window are explicit synthetic input facts.
This is a six-hour appointment book and a **rolling** 2/3/4-hour route horizon. Therefore the
accumulation threshold counts paid arrivals, including future bookings outside the current routing
horizon. It is not a claim that all 60 new bookings belong in one matrix or one pickup slot.
Out-of-horizon bookings must remain accepted and later eligible. The original attempt placing all
West arrivals in one slot correctly encountered FULL at 17; no constraint was relaxed to bypass it.

Routing uses 180-second pickup/60-second discharge service, declared 100 kg/100 litre vehicle
capacity, and existing conservative forecast assumptions (120/60-second local/cluster travel,
1500-second facility travel). These are engineering inputs, not calibrated Delhi fleet parameters.
All exact operational road/shift/capacity/window/custody checks remain enabled. No additional
reserve percentage is introduced. Candidate budgets remain distinct from commercial capacity.

Primary clocks advance by at least a minute per arrival cycle. Explicit North execution follows
the recorded schedule: protected departure, collection, later cargo-bearing replan, then evidenced
discharge. Those physical steps can advance the operational clock further; it must never go back.
Real wall-clock solver duration is measured independently from this controlled domain clock.

The count threshold is a labelled **QA driver**, not a native production count trigger. Every
invocation records its region scope, new/previous bookings, generation/status and actual solver
calls. A successful activation with no new assignment does not establish successful incorporation.

Required invariants: cumulative booking accounting; no duplicate incompatible ownership; unique
active rider/vehicle authority; independently valid proposed windows/loads/precedence/facilities;
immutable completed history; protected-prefix preservation; unchanged cargo attribution unless an
authorized physical transfer occurs; atomic single-region resource ownership; safe provider/solver
failure without false successful assignment. Failures remain acceptance failures.

## Reproduction

`./tests/routing/run-geographical.ps1 -SkipBuild` runs primary real acceptance. Add `-Extended`
for all 36 additional combinations. Remove `-SkipBuild` to rebuild the existing pinned native image
with the repository's Dockerfiles. `-Selection tests/integration/test_geographical_stateful.py`
runs the generated real-service histories. The wrapper owns and removes its disposable DB/container;
it does not touch existing databases. Cached road preparation is under ignored `.tools`.

The versioned coordinate manifest pins the full BBBike NewDelhi OSM source hash and timestamp.
If that rolling download no longer matches, the wrapper refuses to relabel it: the exact historical
PBF is required. Raw MLD graph files are local derived artifacts, not another deployed service.
Allow at least 2 GB for graph preparation, 1 GB for PostgreSQL and 5 GB for native acceptance;
CPU bounds are 2/2/4 respectively. Actual measured durations and incomplete requirements belong in
QA_FINAL_REPORT.md; this scenario specification is not execution evidence.
