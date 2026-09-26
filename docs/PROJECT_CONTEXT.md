# Project Context

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

## Explicitly open

The following are not yet fixed:

- geographic cell sizing/resolution;
- final compaction/clustering algorithm;
- final route optimization algorithm, if any;
- detailed fleet rider-selection algorithm;
- item category taxonomy and final pricing formula;
- detailed "material differs from booking" workflow;
- final offline-evidence validation policy;
- frontend/mobile technology;
- exact production payment gateway;
- final CI/CD provider selection between Azure DevOps Pipelines and GitHub Actions.
