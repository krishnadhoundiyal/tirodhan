# ADR-001: Compute Platform

**Status:** Accepted for MVP

## Decision

Use Azure Container Apps as the common container compute platform.

- Transactional FastAPI API: Azure Container App, Consumption model, scale-to-zero/minimum replicas 0 where operationally acceptable.
- Asynchronous workers: separate Azure Container Apps with `min replicas = 0`.
- Finite scheduled work: Azure Container Apps Jobs where appropriate.
- Workloads are packaged as OCI/Docker containers.

Azure Functions, App Service, and AKS are not the selected MVP compute platform.

## Rationale

The project is highly cost-sensitive and initially low/variable traffic. Container Apps provides a common container model and can avoid paying for continuously idle compute.

## Consequences

- workers remain separate where scaling/failure characteristics differ;
- application code must not assume Container Apps-specific business semantics;
- platform choice may be revisited if scale/cost changes.
