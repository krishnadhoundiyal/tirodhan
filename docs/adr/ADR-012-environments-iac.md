# ADR-012: Environment Topology and Infrastructure as Code

**Status:** Accepted for MVP

## Decision

Permanent environments are local, Azure `nonprod`, and Azure `prod`. Do not maintain permanent Azure `dev`, `qa`, and `staging` environments by default. Production-like staging is ephemeral and created only when required, then destroyed. Production data must not be copied directly to non-production environments.

Terraform is the selected Infrastructure-as-Code tool. `nonprod` and `prod` use separate state; the same reusable Terraform modules/patterns support ephemeral staging; modules remain intentionally lean.

## Rationale

PostgreSQL and Service Bus create fixed cost floors. Multiplying those across idle permanent environments is not justified for a founder-funded MVP. Terraform provides repeatable Azure provisioning and a consistent IaC operating model if AWS/GCP variants are later evaluated.

## Consequences

- environment-specific configuration and secrets remain separated;
- infrastructure recovery/recreation depends on committed Terraform;
- test data in nonprod must be synthetic/generated/anonymized as appropriate.
