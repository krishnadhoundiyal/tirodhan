# ADR-013: CI/CD Contract and Container Registry

**Status:** Partially accepted; provider intentionally open

## CI/CD provider

Approved candidates are Azure DevOps Pipelines and GitHub Actions. The provider is not yet frozen.

## Pipeline contract

Application changes flow through lint/static checks, unit tests, relevant security checks, container build, image publication, nonprod deployment, smoke/integration verification, explicit production approval, and production deployment.

Infrastructure changes flow through Terraform formatting/validation, Terraform plan, production review/approval, and Terraform apply.

Use workload federation / OIDC where supported rather than long-lived Azure credentials.

## Container registry

GitHub Container Registry (GHCR) is the default MVP registry. Azure Container Registry Basic is a fallback only if GHCR causes a concrete Azure-specific authentication, reliability, operational, or deployment-integration problem.

## Consequences

- CI/CD workflow semantics remain portable between the two candidate providers;
- registry integration must not leak into domain/application logic;
- switching from GHCR to ACR is an infrastructure/configuration change, not an application redesign.
