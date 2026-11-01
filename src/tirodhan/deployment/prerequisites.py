"""One mandatory prerequisite contract, independent of the installed check registry."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tirodhan.deployment.deployment_admission import (
    ROOT,
    AdmissionDenied,
    Authorization,
    configuration,
    require_release_authorization,
)

COMMON = frozenset({"target", "identity", "execution_authority"})
DEPLOYMENT = COMMON | {"backend", "permissions", "vault", "ownership", "state"}
FOUNDATION = DEPLOYMENT | {"providers", "postgres_sku", "features"}
MIGRATION = DEPLOYMENT | {"foundation", "database", "secrets", "images", "features"}
REQUIRED: dict[str, frozenset[str]] = {
    "bootstrap": COMMON | {"bootstrap_authority"},
    "initialize-state-plan": COMMON | {"initialization", "permissions", "vault"},
    "initialize-state-apply": COMMON | {"initialization", "permissions", "vault", "saved_plan"},
    "recover-vault": COMMON | {"recovery_authority", "recovery_target"},
    "enroll-recovery": COMMON | {"recovery_enrollment_authority"},
    "state-backup": COMMON | {"backend", "permissions", "vault", "legacy_ownership"},
    "transfer-vault": COMMON
    | {"backend", "permissions", "vault", "legacy_ownership", "recoverable_backup", "saved_plan"},
    "foundation-plan": FOUNDATION,
    "foundation-apply": FOUNDATION | {"saved_plan"},
    "database-bootstrap": COMMON | {"database_authority", "foundation"},
    "database-verify": COMMON
    | {"database_verification_authority", "verification_request", "foundation"},
    "migration-plan": MIGRATION,
    "migration-apply": MIGRATION | {"saved_plan"},
    "migration-start": MIGRATION | {"migration_job"},
    "runtime-plan": MIGRATION | {"migration"},
    "runtime-apply": MIGRATION | {"migration", "saved_plan"},
    "seed-secrets": COMMON | {"permissions", "vault", "secret_batch"},
    "rotate-log-sas": COMMON | {"permissions", "vault", "log_storage"},
    "compensate-foundation": DEPLOYMENT | {"clean_baseline", "saved_plan"},
    "prepare-compensation": DEPLOYMENT | {"clean_baseline"},
}
TERRAFORM_OPERATIONS = frozenset(
    {
        "initialize-state-plan",
        "initialize-state-apply",
        "state-backup",
        "transfer-vault",
        "foundation-plan",
        "foundation-apply",
        "migration-plan",
        "migration-apply",
        "migration-start",
        "runtime-plan",
        "runtime-apply",
        "prepare-compensation",
        "compensate-foundation",
    }
)
for _operation in TERRAFORM_OPERATIONS:
    REQUIRED[_operation] = REQUIRED[_operation] | {"terraform_context"}
CHECK_IDS = frozenset().union(*REQUIRED.values())
IDENTITIES = {
    "bootstrap": "operator",
    "recover-vault": "recovery",
    "enroll-recovery": "operator",
    "database-bootstrap": "database",
    "database-verify": "database_verification",
}


def assert_manifest_complete(path: Path = ROOT / "deploy/nonprod-prerequisites.json") -> None:
    try:
        manifest = json.loads(path.read_text())
        checks = manifest["checks"]
        profiles = manifest["operations"]
        if (
            manifest["schema_version"] != 1
            or set(checks) != CHECK_IDS
            or set(profiles) != set(REQUIRED)
            or any(set(profiles[op]) != required for op, required in REQUIRED.items())
            or any(
                not all(
                    checks[key].get(field)
                    for field in ("authority", "identity", "scope", "allowed_operation", "failure")
                )
                for key in CHECK_IDS
            )
        ):
            raise ValueError
    except (ValueError, KeyError, TypeError, OSError) as exc:
        raise AdmissionDenied("MANDATORY_PREREQUISITE_MANIFEST_INCOMPLETE") from exc


@dataclass(frozen=True)
class Grant:
    authorization: Authorization
    operation: str
    state_fingerprint: str | None
    satisfied_checks: tuple[str, ...]
    not_applicable: dict[str, str]


class Controller:
    def __init__(self, authorization: Authorization, checks: Any) -> None:
        self.authorization = authorization
        self.checks = checks

    def evaluate(self, operation: str) -> Grant:
        assert_manifest_complete()
        if operation not in REQUIRED or self.authorization.operation != operation:
            raise AdmissionDenied("UNKNOWN_OR_UNAUTHORIZED_OPERATION")
        expected_identity = IDENTITIES.get(operation, "deployment")
        if self.authorization.identity_kind != expected_identity:
            raise AdmissionDenied("OPERATION_IDENTITY_BOUNDARY")
        try:
            registry: dict[str, Callable[[], bool]] = self.checks.registry()
        except (AttributeError, TypeError) as exc:
            raise AdmissionDenied("MANDATORY_VALIDATOR_UNIMPLEMENTED") from exc
        # Missing even one mandatory check is a failure, never vacuous all([]).
        if set(registry) != CHECK_IDS:
            raise AdmissionDenied("MANDATORY_VALIDATOR_UNIMPLEMENTED")
        for key in (
            "target",
            "identity",
            "execution_authority",
            *(["terraform_context"] if "terraform_context" in REQUIRED[operation] else []),
            *sorted(REQUIRED[operation] - COMMON - {"terraform_context", "database"}),
            *(["database"] if "database" in REQUIRED[operation] else []),
        ):
            try:
                if registry[key]() is not True:
                    raise AdmissionDenied("PREREQUISITE_NOT_SATISFIED")
            except Exception as exc:
                # Report only controlled identifiers, never exceptions containing secrets/state.
                raise AdmissionDenied(f"PREREQUISITES_BLOCKED:{key}") from exc
        state = getattr(self.checks, "observed_state", None)
        return Grant(
            self.authorization,
            operation,
            state.fingerprint if state else None,
            tuple(sorted(REQUIRED[operation])),
            {
                key: f"Outside explicit operation profile: {operation}"
                for key in sorted(CHECK_IDS - REQUIRED[operation])
            },
        )


def require_provisioning(operation: str, **parameters: Any) -> Grant:
    # Offline release authorization always precedes Azure credentials or observations.
    authorization = require_release_authorization(operation)
    from tirodhan.deployment.database_verification_request import refresh_database_evidence

    refresh_database_evidence(authorization)
    from tirodhan.deployment.lifecycle_checks import Checks

    checks = Checks(authorization, configuration(), parameters)
    result = Controller(authorization, checks).evaluate(operation)
    emit_grant(result)
    return result


def emit_grant(grant: Grant) -> None:
    print(
        json.dumps(
            {
                "status": "PREREQUISITES_SATISFIED",
                "operation": grant.operation,
                "contract_hash": grant.authorization.contract_hash,
                "configuration_hash": grant.authorization.configuration_hash,
                "satisfied_checks": grant.satisfied_checks,
                "not_applicable": grant.not_applicable,
                "state_fingerprint": grant.state_fingerprint,
            },
            sort_keys=True,
        )
    )


def revalidate_database(grant: Grant) -> None:
    """After read-only release preparation, recheck expiry at the provider-effect boundary."""
    if "database" not in REQUIRED[grant.operation]:
        return
    from tirodhan.deployment.lifecycle_checks import Checks

    current = require_release_authorization(grant.operation)
    if (
        current != grant.authorization
        or Checks(current, configuration(), {}).check_database() is not True
    ):
        raise AdmissionDenied("DATABASE_EVIDENCE_NO_LONGER_CURRENT")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=sorted(REQUIRED))
    parser.add_argument("--plan", type=Path)
    args = parser.parse_args()
    try:
        require_provisioning(args.operation, plan=args.plan)
    except AdmissionDenied as exc:
        raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    main()
