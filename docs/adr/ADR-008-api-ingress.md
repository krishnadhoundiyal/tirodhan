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

## NONPROD browser ingress

Cloudflare fronts the Customer and Manager hostnames. Its Worker sends page and
asset requests to separate read-only routes on the existing APIM Consumption
gateway. APIM routes each product to its Azure Container App. The Worker
authenticates to APIM, and APIM uses a separate Key Vault credential for the
web origins. The origin health endpoint remains available to ACA probes.

The browser session and business APIs use the separate API hostname through
Cloudflare and the same APIM gateway. APIM owns browser refresh cookies.
FastAPI remains responsible for session validation, role checks and business
authorization.
