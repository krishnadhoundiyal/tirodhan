# Project Context

## Purpose

Tirodhan is a Delhi-first platform for arranging respectful household pickup of broken, damaged, or no-longer-required religious idols/sacred material and transporting them to designated government or government-authorized receiving locations.

The platform facilitates collection and handover. It does not itself restore, recycle, immerse, or process the material.

## Operating model

The initial model is intentionally flexible and cost-conscious:

- households request pickup;
- pickups are booked into 30-minute time slots;
- a household address is mapped to a geographic cell;
- requests for an upcoming slot are planned by cell shortly before the slot;
- geographically compatible requests may be compacted into a shared collection group;
- a single rider services a collection group;
- if compaction cannot produce a shared group, a request remains serviceable as a singleton;
- riders may come from a managed fleet or from independent rickshaw pullers;
- operational exceptions are escalated to a human manager;
- successfully collected material is taken to a registered government/authorized kiosk/receiving point;
- in-app disposal/handover evidence is captured there;
- request completion is based on validated handover/disposal evidence.

## Actors

### Customer

- authenticates by mobile OTP;
- confirms service address;
- selects a pickup slot;
- pays;
- may cancel while the request is still cancellable;
- receives collection confirmation and status;
- may use the human support channel for active-pickup exceptions.

### Rider

- is onboarded/approved by the platform;
- marks themselves `AVAILABLE` or `OFFLINE`;
- may accept eligible offered work;
- services assigned collection groups;
- records pickup execution;
- captures collection and handover evidence;
- may escalate reachability/operational problems.

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
- preserve logical service boundaries even where MVP economics favour fewer deployables.

## Explicitly open

The following are not yet fixed:

- geographic cell sizing/resolution;
- final compaction/clustering algorithm;
- final route optimization algorithm, if any;
- detailed fleet rider-selection algorithm;
- detailed "material differs from booking" workflow;
- frontend/mobile technology;
- exact production payment gateway;
- final CI/CD provider selection between Azure DevOps Pipelines and GitHub Actions.
