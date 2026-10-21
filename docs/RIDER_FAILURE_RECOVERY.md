# Consolidated rider failure and operational recovery

Implementation on `codex/manager-business-configuration`, based on
`56f71ab637b2ca629ec6aa1da88def23c6a2adb0`. This extends the reviewed manager
configuration increment. Production dispatch/orchestration flags remain disabled.

## Implementation map

| Boundary | Principal files |
|---|---|
| Concrete schema and origins | `migrations/versions/0035_rider_failure_recovery.py`, `failure_models.py`, `dispatch_models.py`, `capacity_models.py`, migration metadata registration |
| Manager commands and polling | `failure_commands.py`, `api/routes/routing_recovery.py`, gated registration in `main.py` |
| Local recommendations | `recovery_planning.py`, `replacement_options.py`, `replacements.py`, existing candidate/evaluation relations |
| Authoritative activation | `failure_execution.py`, shared `authority.commit_routes`, existing route execution/recovery/custody commands |
| Durable execution and reconciliation | `failure_runtime.py`, `workers/routing_recovery.py`, existing planning worker/scheduler/regional maintenance and outbox publisher |
| Rider delivery | `route_notifications.py`, existing rider notification worker and push port; per-device revision progress |
| Compatibility and history | execution origin reader, business-admin lineage, retention inventory, legacy assignment eligibility guard |
| Regression evidence | `test_rider_failure_recovery.py`, `test_rider_failure_native.py`, `test_recovery_worker.py`; historical migration seed and worker envelope fixture updates |

Paths in the routing rows are relative to `src/tirodhan/modules/routing_operations/` unless
otherwise qualified. ADR-002/004/006/014 and the relevant domain/schema/security/idempotency
documents now describe the implemented boundary. No dependency declaration, Terraform resource
or production feature default changes.

## Authority and policy

Manager approval authorizes an exact recovery allocation; it does not itself assign,
collect, transfer, discharge or complete a booking. A manager-coordinated rendezvous
is the approved initial physical-transfer model. Directions are coordinated by the
manager; the backend exposes the participants and existing transfer intent. No synthetic
household pickup, automated HANDOFF route task or additional custody system is introduced.
The existing two-party receipts / approved donor-witness evidence and CompleteTransfer
remain mandatory. The original custodian remains responsible until physical transfer
commits. Shift expiry cannot erase that responsibility or create available capacity.

Both the regional planner and recovery call `authority.commit_routes` for assignment,
route revision, task, delivery and replacement-completion persistence. The regional origin
retains its run/engine/composite/source/fleet-cycle/lease checks. Recovery verifies the
persisted manager-selected assignment, regional ownership version and claimed execution
lease against current authoritative state. It never fabricates a shadow run or fleet cycle.
RouteActivation has exactly one concrete origin: the existing run with engine identity,
or an immutable RecoveryWitness with no invented engine identity. Execution reads immutable
policy through either origin. History readers expose the recovery witness; retention
inventory protects active-route diagnostics for both origins.

Before-pickup ownership release occurs with successor activation, not manager selection.
Explicit failed-service resolution may release an uncollected household through existing
RouteRecovery mechanics; the manager must affirm no collection occurred. Successful pickup
facts are never reversed. Completed lots survive failure of later recovery assignments.

## Incident and allocation relations (migration 0035)

- RiderFailureIncident is one open incident per failed rider, with manager, reason, region,
  creation time and optimistic version.
- FailureRoute snapshots every affected nonclosed route and its then-current revision.
- Existing RiderReplacement is the per-booking obligation ledger, with an additional concrete
  failure incident FK. Previously recorded request actor/reason/time and legacy incident
  references are retained when adopting an unapproved request.
- RecoveryAssignment records an exact approved candidate and receiving shift, progress,
  retry deadline, token/expiry and actual activation FK.
- RecoveryAssignmentItem preserves allocation membership; its partial unique index prevents
  simultaneous active allocations for one obligation. Released membership remains history.
- RecoveryWitness is immutable contract/policy/source evidence, not mutable route ownership.
- RoutePushDelivery retains per-registration revision-notification progress.

One incident can have multiple receiving riders and independent activation outcomes. The
manager approves a list of explicit candidate/subset mappings. Missing obligations remain
visible and unallocated; they are not interpreted as successfully recovered. Whole-household
cargo lots remain indivisible. Initial implementation permits one pending recovery assignment
per receiving shift. Further work can be consolidated on that rider after its preceding
assignment activates and the enlarged incumbent is re-evaluated. This deliberately avoids
combining independently feasible marginal proposals without a joint proof. It does not limit
the number of riders used for one incident or require incident-wide atomic activation.

A database trigger protects approved assignment terms. Witnesses and incident route snapshots
are immutable. Adopted new history makes downgrade refuse before data removal. Existing
financial schema, initiation-term triggers, accepted bookings and custody history are preserved.

## Recommendation and feasibility

The approved baseline is fixed-incumbent local route repair with precedence-respecting paired
pickup/discharge insertion. Each candidate includes all its existing work and relevant rescued
obligations. Existing stop order and protected prefix are retained by the insertion path.
Each insertion is independently feasibility-gated; bounded exhaustion is UNKNOWN, never a proof
that no replacement exists. Native VROOM and OR-Tools remain bounded fallback alternatives.
Local repair has the explicit `LOCAL_REPAIR` selection identity; native identities remain real.

The regional source read is retained for authoritative operational projection. Local matrix and
solver scope contain the receiving vehicle, its complete commitments and rescued work, relevant
receiving points and windows. Other current discharge visits at those facilities, including
other regions, remain external occupancy constraints. Scope/byte/time limits precede matrix
acquisition. No clamping, fabricated road values or paid matrix provider is introduced.

For collected material, recommendation accounts for hypothetical acquisition at the donor's
fresh observed position using existing handling assumptions. That witness cannot activate.
The donor position comes from the canonical vehicle-position ledger even after its shift expires;
it is rendezvous information and never makes the failed donor eligible commercial capacity.
Recovery continuation is rebuilt from actual custody only after evidenced transfer. A source
fingerprint plus current regional membership version fences stale local work. A consistent
MVCC read brackets acquisition; SERIALIZABLE commit re-reads the dependencies under authority
locks. Road graph/profile provenance and independent facility validation are retained.
The live clock is refreshed after Payment/resource/facility/allocation locks: time spent
waiting for authority cannot preserve an expired shift or stale vehicle-position observation.
The end-of-transaction lease fence also includes elapsed persistence time.

Insertion enumerates O(m^2) pickup/discharge position pairs for a route of m incumbent stops;
naive forward validation contributes another route-length factor. Technical candidate and wall
budgets bound execution. It is a feasible repair heuristic, not a globally optimal allocation
solver. Native fallback remains subject to existing node/search budgets. Full-city source-read
cost, graph memory/startup and sustained incident-throughput calibration are not established
by the small native acceptance fixture.

## API and manager lifecycle

All routes are registered only behind existing disabled-by-default orchestration gates.

- GET `/v2/manager/riders/{id}/recovery-state`: current route IDs/versions and open incidents.
- POST `/v2/manager/rider-failures`: typed FAIL_RIDER, APPROVE_RECOVERY, RETRY_RECOVERY or
  RESOLVE_FAILED_PICKUP commands. Returns HTTP 202 and the stable result resource ID.
- GET `/v2/manager/rider-failures/{id}`: incident, paginated obligations and recovery legs.
- Existing GET `/v2/manager/replacements/{id}` and evaluation history: candidate provenance,
  freshness, selected target, transfer state and authoritative activation distinction.
- Existing manager route/history views and rider dispatch-sync: actual current revisions.

FAIL_RIDER verifies expected complete route versions, freezes resources, preserves completed
pickups/custody and creates an obligation for every affected household. Profile suspension is
still separate. Legacy booking-based manager failure commands expand to the whole route;
new incidents cannot bypass allocation approval through the legacy decision command.
Legacy assignment/availability entry points also reject an open failure or blocked operating
vehicle, including after the donor's empty route closes. Device registration, evidence and
custody reporting remain available.

Approval performs bounded live authorization, version and elapsed-time proof checks. It invokes
no road provider, solver or regional planner. Existing customer promises are not changed.
The selected rider is never silently substituted. Changed current routes are repaired only
for that selected resource. Failure persists retry/attention state. Manager-requested new options
withdraw the old unactivated allocation with immutable decision history; an unresolved physical
transfer must be resolved first. Started household work requires explicit manager resolution.
An evidenced transfer abort retains the original custodian and moves recovery to manager
attention. Explicit manager retry can authorize a fresh transfer to the selected rider, or
withdraw for new options. Each renewed physical attempt has a distinct idempotent command;
the aborted intent and evidence remain intact. An unresolved or completed transfer cannot
be discarded by requesting new options.

Decision RECORDED, activation PENDING/RUNNING/ACTIVATED/ATTENTION, actual transfer state and rider
receipt remain distinct. An incident resolves after all obligations have authoritative recovery
(or an existing valid cancellation/completion), custody prerequisites are satisfied and current nonclosed
routes are acknowledged. Booking completion remains governed by actual receiving-point handover.
Polling and incident reconciliation bind activation routes to the approved receiving shift;
other routes in a composite regional activation do not supply that allocation's receipt state.
Polling exposes both `activation_revision_id` for history and `revision_id` for the receiver's
current route revision, with acknowledgment/deadline/escalation belonging to that current revision.

## Concurrency and idempotency

| Operation | Business key and DB protection | Replay / external effect |
|---|---|---|
| Manager command | actor + client UUID + typed fingerprint; SERIALIZABLE; expected versions; one open incident/rider | same resource after live authorization; changed replay conflicts; no external I/O |
| Recommendation | replacement ID/version + claim token/expiry | one current compute owner; stale owners cannot publish; provider/solver work outside locks |
| Allocation | exact incident/candidate/obligation mapping; one active allocation/obligation and one pending receiving shift | approval/decisions/items/outbox atomic; ownership remains unchanged |
| Activation | assignment ID + token/expiry; immutable witness and unique origin; Payment-first source locks | assignment/revision/tasks/completion/delivery/outbox atomic; replay returns activation |
| Worker message | transport inbox plus work ID/phase and domain claims | duplicate messages do not duplicate business effects; crash after commit replays committed result |
| Physical transfer | existing transfer intent/sequence, participants and evidence | existing immutable per-lot custody semantics; no dependency on media upload |
| Revision notification | revision/device progress and transport inbox | send outside DB transactions; ambiguous push can duplicate; a fresh receipt-timeout wave may notify again |
| Periodic reconciliation | bounded due IDs, row claims and unique outbox keys | reconstructs missed/expired execution; advances no business completion from a wake alone |

Resource locking retains profile -> availability -> identity/role -> vehicle ordering after
Payment/work-unit/request ancestors. Emergency recovery may include an inactive donor, while
live authorization remains mandatory for the receiving rider. Competing regional/recovery
commits use the same writer and resource locks. Disjoint compute is concurrent; shared route or
facility mutations serialize briefly. Old regional fingerprints cannot overwrite a recovery
revision. Actual approved targets remain constraints for subsequent planning. Either origin's
successful activation updates the associated recovery assignment in the same transaction.

## Existing runtime wiring and recovery

Identifier-only RecoveryWorkRequested outbox events are explicitly routed to the existing
session-enabled planning queue. Session IDs are per recovery work identity, not an entire region.
The existing planning worker dispatches these typed messages through a durable inbox. The
recovery handler uses the shared finite private-road supervisor and the pinned native adapters.
Recommendation/activation use separate atomic token claims; an advisory publication fence alone
is not treated as a compute claim. Bounded activation draining precedes advisory work; it does
not promise broker priority or preemption of an already-running native call.

The existing finite planning scheduler reconciles due recovery work even when V1 planning
configuration is absent. It recreates outbox debt, expires claims, recognizes authoritative
cancellation/completion, runs due receipt deadlines and resolves eligible incidents. Existing
cancellation reconciliation detaches canceled outstanding route tasks without a regional solve.
An activation rechecks obligation coverage after preflight and under commit authority; a
cancellation cannot manufacture an empty successor activation or restore a canceled booking.
Contradictory canceled-but-collected facts retain custody and require attention. Regional maintenance
also processes bounded recovery before ordinary optimization. It no longer waits for a regional
solve before computing the first recommendation. The conditional utc_now import is corrected.

VehicleRouteRevised / VehicleRouteDeliveryRequested now have explicit publisher and rider
consumer routes. Delivery is best effort; receipt is revision-bound and authoritative. Successful
assignment/custody never rolls back due to push failure. Manager polling remains the manager
result channel; no manager push/UI implementation is claimed.

`TIRODHAN_ROUTING_RECOVERY_PROFILES_PATH` identifies a validated list of the existing
LocalRoadProfile contract. Existing orchestration/fleet/dispatch flags must all be enabled.
The runtime additionally needs pinned osrm-routed/VROOM/OR-Tools, actual graph files and their
profile/provenance configuration. The local native test image supplies these dependencies;
the default production image is not silently converted into a graph-hosting runtime. Missing
configuration keeps work durable/retryable. Production enablement requires reviewed packaging,
CPU/memory, graph storage/startup, broker lock-renewal and cadence measurements. No Terraform
resource, queue, paid service, warm road server or always-on deployment is added by this change.

Receipt timing uses existing FleetPolicy delivery_ack_seconds and dispatch_readiness_seconds.
Recommendation has a 60-second compute claim and a 30-second search envelope; activation has a
120-second claim with finite acquisition/native budgets. Expiry fences publication and commit;
periodic reconciliation restores abandoned work. These technical bounds are not new customer
SLAs. Deployment resource/cost calibration remains an explicit release prerequisite.

## Compatibility and validation

Old route activations retain run/engine provenance. Existing legacy replacement history remains
readable; new incident allocations use the common writer. Upgrade API and execution workers before
adopting recovery-origin revisions: older workers require run provenance and cannot execute them.
An adopted-history downgrade requires reviewed export/drain; feature disablement is not rollback.
No checkout feasibility check, reserve percentage, payment/refund policy or financial trigger is
introduced or changed.

## Reproducing validation

Use disposable PostgreSQL/PostGIS 17-3.5 only. The default repository suite uses the same
Python 3.12 and development extra as `.github/workflows/pr-checks.yml`. A second run with
the optional routing extra exercises regional tests that otherwise skip at module import.
The native tests use the existing pinned routing image and Delhi fixture; they validate
actual directed road matrices and both native solvers rather than mock native proposals.

```powershell
# Point this only at a disposable PostgreSQL/PostGIS database.
$env:TIRODHAN_TEST_DATABASE_URL = '<disposable PostgreSQL asyncpg URL>'
python -m pip install -e '.[dev]'
pytest -q -p no:cacheprovider --tb=short
ruff check .
ruff format --check .
mypy --no-incremental src

# In a separate clean Python 3.12 environment for optional regional coverage:
python -m pip install -e '.[dev,routing]'
pytest -q -p no:cacheprovider --tb=short

# PowerShell 7; each invocation creates/removes its own disposable database.
./tests/routing/run-geographical.ps1 -Selection tests/integration/test_rider_failure_native.py
./tests/routing/run-geographical.ps1 -SkipBuild -Selection tests/integration/test_manager_replacement_native.py
```

The focused database/worker/schema selection is
`tests/integration/test_rider_failure_recovery.py tests/unit/test_recovery_worker.py
tests/integration/test_manager_migration.py`. Install the routing extra for this selection:
the new database tests import the regional fixture, whose module explicitly skips without it.
It covers complete incident discovery, replay, contending managers, expiry-fenced claims,
provider failure/recovery, receiver shift expiry/holds, cancellation races at three boundaries,
transport replay, per-device partial notification retry, lost wake recovery and refused lossy
rollback. Existing financial-history/schema validation remains strict; extension-owned PostGIS
objects retain the existing ownership filter, and all initiation-term triggers remain checked.

The four native scenarios cover both existing compatibility scenarios plus a two-rider cargo
rescue through each actual native fallback. The donor shift has expired; original cargo survives.
An unresolved rendezvous cannot be discarded. Evidenced abort is followed by explicit same-rider
retry (VROOM) or renewed candidate selection (OR-Tools), a fresh transfer intent, two-party
physical receipts, atomic custody transfer, actual activation, receipt and facility discharge.
The original empty cart closes through existing recovery and remains unavailable for legacy
assignment while its failure hold remains. These are functional acceptance cases, not a
city-scale performance benchmark.

## Actual validation

The final source snapshot was checked bidirectionally against the workspace: all 451 Python
files under `src`, `tests` and `migrations` match after line-ending normalization, with no
missing or extra files in those roots. Tests use task-owned disposable PostgreSQL/PostGIS;
no tests use the existing application database. Interrupted development runs are excluded
from the final totals. The final full-suite runtime is Python 3.12.15; the disposable
database reports PostgreSQL 17.5 and PostGIS 3.5.2 (image `postgis/postgis:17-3.5`).

| Check | Result |
|---|---|
| Full repository suite, Python 3.12 with optional routing dependency | 1,535 passed, 65 skipped, 10 warnings in 1087.82s (18m 07s) |
| Focused incident/worker/migration suite, Python 3.12 with routing | 19 passed in 62.42s |
| Actual OSRM/VROOM/OR-Tools recovery and existing compatibility scenarios | 4 passed, 4 warnings in 76.30s |
| Ruff | Passed |
| Formatting | Passed; 526 files checked in the workspace |
| Strict mypy | Passed; 248 source files |

Native fixture checks enforce OSRM 6.0.0, VROOM 1.15.0 and OR-Tools 9.14.6206. The four
native warnings concern the existing capacity-reservation/booking Cartesian-product query;
that unrelated projection query is unchanged. The full run reports six instances of that
warning plus four existing Firebase SDK `MulticastMessage.tokens` deprecation warnings.
The 65 skips are explicit native/geographical/performance opt-ins, not missing database or
routing-library coverage. Four of those skipped native cases were separately executed above;
the other optional campaigns were not run for this increment.

The post-lock timing regression uses a deterministic 45-second simulated authority wait
with a normally configured 30-second position-freshness limit and a 120-second work lease.
Before the clock correction it incorrectly committed an activation; afterward it records
RESOURCE_AUTHORIZATION_STALE without a recovery witness or ownership release. Other
concurrency tests use real PostgreSQL sessions and synchronized lock contention.

The receipt regression executes two independently approved allocations to the same receiver,
activates a newer route revision and acknowledges only that current revision. Before the
read-model correction the earlier allocation incorrectly appeared unacknowledged; afterward
both allocations report the current receipt while retaining their separate activation revision
IDs. The same receiving-shift query excludes unrelated activation routes during reconciliation.

An initial Python 3.13 full run exposed the optional-cycle initialization regression in
the shared-writer extraction, a historical migration seed calling modern dispatch against
an older schema, and obsolete worker-envelope test doubles. These were corrected. An
unchanged OTP traceback assertion also fails on base commit `56f71ab` under Python 3.13:
expanded traceback source context includes the test's literal OTP. A standalone baseline
probe confirms that the API key, provider session and phone are absent from that traceback.
The same test passed in the Python 3.12 compatibility selection. Identity/provider logic
and that pre-existing test remain unchanged.

Local native acceptance does not validate deployed Azure Service Bus/FCM delivery, production
graph storage/startup or sustained incident throughput. Production enablement remains subject
to the packaging, operational acceptance and resource/cost review described above. This
increment is intended for independent code review; it does not authorize production cutover.
