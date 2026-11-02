# NONPROD authorization readiness

Prepared on 2 November 2026. This nonsecret inventory is not deployment authority.

| Requirement | Status |
|---|---|
| Release hold | Satisfied: committed policy remains disabled |
| Admission/prerequisite contract | Retained; issuance uses the existing validator |
| Independent encrypted key generation | Implemented; tested with synthetic keys only; no operational keys generated |
| Release public-key enrollment | Missing: committed authorization key registry is empty |
| V key/principal enrollment | Missing: committed database evidence key registry is empty |
| GitHub secret and variable values | Unverified; presence cannot prove unreadable values |
| Azure identities, permissions and SQL enrollment | Unverified; no Azure changes made |
| Final source/configuration and review evidence | Pending final PR #100 review/merge and approved actual inputs |
| Apply bindings | Derived from reviewed native artifact/exact state; no live plan requested |
| Fresh SQL evidence | Missing until separately authorized existing verifier execution |
| 2Factor | Wiring implemented; actual credential and provider acceptance unverified |
| Google no-cost eligibility | Owner supplied India/project number 742429799693; billing, usage and quotas unverified |
| Deferred migration safeguards | Issues #98/#99 remain open |
| Provisioning authority | Blocked; no operational signing/enrollment/dispatch/deployment performed |

Use `release_setup readiness` and read-only `release_capture inventory` under approved
operator access. Follow [the custody/issuance runbook](NONPROD_RELEASE_AUTHORIZATION.md).

Local validation: 572 lifecycle/authorization/security/concurrency/teardown tests passed;
four native Terraform fixtures require the pinned CI toolchain. Windows custody testing
passed 40 tests with disposable synthetic keys. Repository lint/format and type checking
of 289 source modules passed. Final-head GitHub CI and CodeRabbit review remain required.
