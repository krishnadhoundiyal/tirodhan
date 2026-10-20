# Project Context

Current commercial policy (19 October 2026): projection-backed slot allowances and atomic holds
replace the customer per-booking routing feasibility prerequisite. The old customer preparation
API/worker are retired. No trained model or new infrastructure is introduced; operational route
validation, payment-first rules and custody guarantees remain. This supersedes earlier statements
requiring exact commercial certificates for every new booking. See
[implemented boundary](PROJECTED_SLOT_ALLOWANCES.md). Production activation remains disabled.

Regional capacity transfer now uses one versioned owner and refuses outstanding commitments,
cargo and live holds. Rider replacement is manager-approved; custody moves only through existing
evidenced transfers. Versioned advisory slot projections use database invalidation and finite
reconciliation, with exact commercial admission unchanged. Adaptive OSRM rectangles preserve strict
road provenance. Production remains disabled. See [implementation](REGIONAL_CAPACITY_AND_RECOVERY.md).

Commercial preparation now uses exact resumable pair insertion, sparse directed road acquisition,
independent shared route certificates, incremental source reads and a durable regional writer.
Migration 0030 removes commercial population ceilings, including 128 bookings; byte/time budgets
yield UNKNOWN and durable retry rather than rejecting capacity. This does not activate V2.
OSRM's verified upstream negative-distance defect remains a release gate. See
[implementation, validation and measured constraints](COMMERCIAL_ADMISSION_SCALABILITY.md).

Current engineering increment 8 adds finite regional owners, explicit pre-shift rider
confirmation, local per-profile OSRM, parallel native candidate workers, hybrid CP-SAT
selection and composite transactional activation with per-booking outcomes. Production
remains disabled. See [implementation and measured limits](V2_ROUTING_INCREMENT_8.md).

The approved 12 October 2026 V2 direction extends the V1 operating model below with
cross-cell/compatible-slot routing, continuous collection vehicles and authoritative
dispatch during committed operating shifts; flexible riders use voluntary acceptance.
OSRM + VROOM + OR-Tools is the approved technology combination. Future bookings require
no current rider, and advance provisional planning must preserve cancellation/financial
rights. Increments 1–5 implement the independent contract/gate, pinned road/solver
adapters, operational inputs, vehicle-bound authority/execution and opt-in continuous
committed-fleet orchestration. Production activation remains disabled and V1 execution
remains active until approved cutover. See [current boundary](V2_ROUTING_INCREMENT_5.md)
and the V2 amendments to ADR-004/006/015. Routing technology and V2 dispatch authority are
no longer open decisions; operational calibration and configuration remain open.

## Purpose

Tirodhan is a Delhi-first platform for arranging respectful household pickup of broken, damaged, or no-longer-required religious idols/sacred material and transporting them to designated government or government-authorized receiving locations.

The platform facilitates collection and handover. It does not itself restore, recycle, immerse, or process the material.

## Operating model

The initial model is intentionally flexible and cost-conscious:

- customers may maintain multiple saved addresses;
- households request pickup for a selected/confirmed serviceable address;
- pickups are booked into 30-minute time slots;
- a household location is mapped to a geographic cell;
- requests for an upcoming slot are frozen/planned by cell shortly before the slot;
- geographically compatible requests may be compacted into a shared collection group;
- if compaction cannot produce a shared group, a request remains serviceable as a singleton;
- a rider services an assigned collection group or outstanding subset after reassignment;
- riders may come from a managed fleet or from independent rickshaw pullers;
- operational exceptions are escalated to a human manager;
- successfully collected material is taken to a registered government/authorized receiving point;
- in-app pickup and handover evidence is captured;
- request completion is based on valid handover at the registered receiving point.

## Actors

### Customer

- authenticates by mobile OTP;
- may keep multiple saved addresses;
- confirms/selects a service address;
- selects a pickup slot;
- pays;
- may cancel while the request is still cancellable;
- receives collection confirmation and status;
- may use the human support channel for active-pickup exceptions.

### Rider

- is onboarded/approved by the platform;
- controls availability intent: `AVAILABLE` or `OFFLINE`;
- receives/accepts work only when operationally eligible and idle;
- services assigned collection work;
- records pickup execution;
- captures pickup and handover evidence;
- may escalate reachability/operational problems.

The platform separately tracks whether an available rider is currently `IDLE`, `RESERVED`, or `BUSY`.

### Manager / Operations

- handles automatic-assignment escalation;
- manually assigns/reassigns riders when necessary;
- handles house/location/rider exceptions;
- may cancel/refund when fulfilment cannot proceed;
- records the operational resolution in the system.

## MVP principles

- founder-funded and highly cost-sensitive;
- target end-to-end delivery over broad feature depth;
- architecture should remain production-sensible;
- avoid infrastructure that does not solve a demonstrated need;
- retain human-in-the-loop fallbacks for operational exceptions;
- preserve logical service boundaries even where MVP economics favour fewer deployables;
- prefer established application/domain patterns rather than bespoke schema where the workflow is conventional;
- idempotency is required for every retryable/replayable/concurrent mutation.

Phase 1V selects Razorpay (`RAZORPAY`) for production payments and normal idempotent refunds.
Orders use stable per-attempt receipts with lookup/recovery. Authenticated payment.captured
webhooks, not client return, authorize financial success. Authorized Refund intents execute
asynchronously through Service Bus; cancellation/late-success refund policy remains open.

## Explicitly open

The following are not yet fixed:

- production compaction-distance and maximum group-request values;
- final route optimization algorithm, if any;
- detailed fleet rider-selection algorithm;
- item category taxonomy and pricing extensions beyond the approved flat pickup fee;
- detailed "material differs from booking" workflow;
- final offline-evidence validation policy;
- frontend/mobile technology;
- exact production payment gateway;
- final CI/CD provider selection between Azure DevOps Pipelines and GitHub Actions.

## V2 engineering implementation status

Increment 4 now implements transactional committed-route activation, revision fencing,
vehicle-bound pickup and whole-lot discharge/recovery using the merged engines/gate and
operating inputs. Default dispatch activation remains false. Admitted V2 work cannot
re-enter legacy grouping/dispatch; rolling single-owner admission, flexible supplementary
acceptance, delivery and global V1 retirement remain the next integration. No production
hosting/cost commitment is made. See [current implementation](V2_ROUTING_INCREMENT_4.md).
The earlier increment descriptions below are historical implementation boundaries.

Increment 1's contract/gate is merged. Increment 2 implements finite OSRM road matrix
acquisition and both approved pinned native engines behind that unchanged gate, with
isolated real-engine tests. No authoritative V2 assignment or production routing runtime
is enabled. The approved architecture is OSRM + VROOM + OR-Tools with committed operating
capacity first and flexible voluntary supplementary capacity; detailed economic objectives,
physical inputs/profile calibration and production graph hosting remain decisions for
subsequent increments. See [increment 2](V2_ROUTING_INCREMENT_2.md), ADR-004 and ADR-006.

## Increment 5 committed fleet implementation

The opt-in finite fleet worker now jointly evaluates idle, BUSY and loaded committed
resources across cells/slots, compares both pinned engines on a measured objective,
rebases on current positions and activates through existing transactional authority.
Production fleet/dispatch flags remain false. Increment 5 does not provision a schedule
or road-service hosting. See [implementation, validation and gaps](V2_ROUTING_INCREMENT_5.md).

## Current V2 boundary — increment 7

Regional capacity, receipt/acceptance, reattempt consent, explicit custody recovery, breaks/
shift authorization, destination fallback, ETA and finite preflight/reconciliation are behind
disabled production flags. No new cloud resources or fixed recurring service are provisioned.
See [implementation and release prerequisites](V2_ROUTING_INCREMENT_7.md).

## Manager business configuration increment

Manager-configured versioned flat INR pickup pricing replaces the unconfigured production pricing port. Entity-specific immutable histories and manager read/compare APIs cover editable masters; existing duty and operational facts remain authoritative. Replacement publishes bounded validated options, supports manager incidents and explicit candidate selection, and completes only after actual authoritative route activation. See [implementation and authority contracts](MANAGER_BUSINESS_CONFIGURATION.md). Production activation flags remain unchanged.
