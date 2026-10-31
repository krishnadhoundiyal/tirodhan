# NONPROD ACA deletion recovery

This closes the incomplete-deletion path in PR #94 after run
[41201744821](https://github.com/krishnadhoundiyal/tirodhan/actions/runs/41201744821/job/124539048864).
AzureRM 5.8.0 failed while polling managed-environment deletion with an empty response
Content-Type after approximately 17 minutes. That error alone proves neither successful
deletion nor continued existence. The provider pin remains unchanged; no unverified upgrade
is presented as a fix.

The recovery now actively drives the approved environment to absence through ARM, independently
of AzureRM's LRO poller. It preserves Terraform as the infrastructure/state owner, with this
limited deletion recovery authorized by the same protected saved destroy plan. It never creates
resources, deletes a resource group, purges a vault, grants roles, or runs `terraform state rm`.
No new paid service or recurring resource is introduced.

## Workflow and authorization

The existing `Destroy NONPROD` workflow remains manual, main-only, confirmation- and
environment-approval-gated, serialized through `nonprod-release`. A normal Terraform apply
still executes the approved saved destroy plan first. Unrecognized apply failures remain fatal.
Only the existing narrow, single-environment polling-error classification permits recovery
after a nonzero Terraform result.

The new `Complete and verify approved ACA environment deletion` step runs after both normal
Terraform success and the recognized polling failure. It feeds the **original saved plan** to
`python -m tirodhan.deployment.destroy_aca_environment`. The helper revalidates all managed
actions with the existing destroy-plan guard, then extracts exactly one approved managed
environment DELETE ID. The subscription, RG, namespace, type and single-segment name are
checked before credentials or HTTP calls. The target is never guessed from an RG/name string.
Multiple environments are refused; an empty/no-environment plan grants no recovery target.
Final Terraform/RG/bootstrap checks still apply to terminal empty plans.

The helper uses the existing Azure CLI OIDC login via `AzureCliCredential`, explicitly selecting
the approved subscription and configured tenant. It needs only existing deployment permissions;
no PostgreSQL administrator, runtime-identity change, new secret or broader role is added.
The endpoint is fixed to public Azure ARM, API version `2025-07-01`, verified TLS, no proxies
or redirects. Poll URLs are restricted to HTTPS ARM, the same subscription, and Microsoft.App
operation paths in the approved RG or the provider's subscription-level operation scope.
Credentials never travel in command arguments; HTTP bodies/token errors are not logged.

## Completion protocol

1. Direct GET of the exact environment ID establishes presence. A successful response must
   carry that same ID and valid properties. Two consecutive HTTP 404 responses with the ARM
   `ResourceNotFound` code, separated by the poll interval, are required for success.
   Authentication failure, unknown 404 codes, malformed bodies and RG inventory results do
   not establish absence.
2. A present resource authorizes an idempotent DELETE of **that exact approved ID only**.
   Restarting after a crash can reattach to a pre-existing deletion through the same DELETE;
   it cannot create a second resource. Once an existing deletion is observed, it is monitored.
3. HTTP 202 follows `Azure-AsyncOperation`, or `Location` if the former is absent.
   `Retry-After` integer/date values are honored. JSON polling is independent of Content-Type.
   Empty 200/204 responses and an operation's `Succeeded` status are only completion candidates:
   the environment itself must still become confirmed absent.
4. Transient reads/polls (429, 500, 502, 503, 504 and HTTP transport errors) wait and retry within
   the deadline. An ambiguous DELETE always re-reads resource state before any resubmission.
   Known in-progress conflicts are monitored. Explicit retryable operation failures
   (`InternalServerError`, `ServiceUnavailable`, `OperationTimedOut`) can issue another DELETE,
   including if resource properties still show the old scheduled-deletion state.
5. Authorization/lock/in-use errors and non-retryable Failed/Canceled operations fail with
   controlled HTTP/provider codes. The helper does not remove locks or delete unapproved apps
   to clear dependencies. A stuck deletion fails with the last safe observed state.
6. Only after direct resource absence is proven may the existing broader RG-empty check and
   fresh `--require-empty` destroy plan reconcile Terraform's remote state under its normal
   lock. Empty state, empty application RG and preserved bootstrap resources remain final gates.

There are at most three DELETE submissions per helper invocation, with at least 30 seconds
between submissions; observation defaults to ten seconds and the recovery budget to 1,800
seconds. These are explicit reviewed workflow arguments, not business admission policies.
HTTP operations have at most a 15-second timeout per I/O phase, credential commands ten seconds,
and bodies at most 128 KiB. The monotonic recovery deadline is checked between I/O operations;
it is not a claim of preempting stalled libc DNS or an OS call. The destroy-apply job has a
90-minute outer runner timeout covering initial Terraform work, recovery and final checks.
Exhaustion remains a failed job with recoverable Terraform state and actionable diagnostics.

## Idempotency and crash/retry

| Boundary | Protection and replay |
|---|---|
| Authorization | Original guarded destroy plan; exact resource ID and scoped existing RBAC |
| Concurrency | Existing workflow serialization; normal Terraform backend lock for state operations; concurrent DELETEs address the same idempotent ARM resource |
| Crash before DELETE | New approved run reads current state before sending |
| Lost response after Azure accepted DELETE | Read-first observation/reattachment of the same ID; no new identity or resource |
| Already absent resource | Two direct confirmed reads; zero new DELETEs |
| Crash before Terraform reconciliation | Retained remote state; next normal refresh observes absence and guarded terminal apply records it |
| External provider | ARM resource DELETE is idempotent; HTTP acceptance/LRO status never substitutes for resource absence |

There is no application DB mutation, inbox/outbox, financial or fulfilment lifecycle change.
The helper is a finite operator/CI action, not an API, scheduler, background service or
independently deployed component. A later protected rerun is still required after a runner
failure; no workflow is dispatched automatically by this change.

## Verification and operational acceptance

Tests exercise real HTTP request shapes with `httpx.MockTransport`, stateful failure/retry
histories, exact replay, concurrent requests, lost provider responses, wrong scope, malicious
poll URLs, empty Content-Type/body completion, throttling, malformed/error responses and
bounded stuck resources. Shell tests execute the workflow gates using fake Terraform/Azure
commands and verify that a failed direct-resource check prevents terminal state reconciliation
after both initial Terraform success and the recognized polling error.

Verification completed:

- Final related deployment/bootstrap/runtime/workflow/destroy regressions: **174 passed**,
  network-isolated Linux container, 8.22 seconds. This includes **100 focused** deletion
  controller, plan guard and actual workflow-shell gate cases, including compressed responses.
- Ruff lint and formatting pass for **581 Python files**; strict mypy passes for **270 source
  modules**. `git diff --check` passes. No Terraform HCL/provider/resource definition changed.

These tests do not prove a live Azure deletion. No Azure authentication, deletion, deployment,
workflow dispatch, state mutation or provider upgrade was performed during implementation.
After review/merge, an authorized protected destroy run must prove the actual environment's
direct ResourceNotFound observations, Terraform state convergence and preserved bootstrap
resources before the live issue is considered operationally closed.

Protocol references:
[managed environment DELETE](https://learn.microsoft.com/en-us/rest/api/resource-manager/containerapps/managed-environments/delete?view=rest-resource-manager-containerapps-2025-07-01),
[asynchronous ARM operations](https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/async-operations).
