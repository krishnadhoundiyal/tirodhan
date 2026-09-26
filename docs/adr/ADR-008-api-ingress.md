# ADR-008: API Ingress

**Status:** Accepted for MVP, explicitly replaceable

## Decision

Start with Azure API Management Consumption as the public API edge.

APIM may perform generic edge concerns such as:

- routing;
- token/JWT validation;
- throttling;
- correlation/policy;
- authenticated invocation of the backend.

Business authorization remains in FastAPI.

The transactional backend is a single modular FastAPI deployment rather than separately deployed customer/rider/manager microservices.

## Cost rule

APIM is not an architectural dependency. If it materially increases cost, it may be removed and clients may call the protected Container App ingress directly without rewriting business logic.

## Networking

Because Consumption-tier APIM does not provide the same private-VNet backend topology as higher tiers, the backend may remain publicly addressable but must be strongly authenticated/authorized.

Workers/jobs have no public ingress.
