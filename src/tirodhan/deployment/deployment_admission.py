"""Offline, signed release admission. No Azure authentication occurs in this module."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import jwt

ROOT = Path(__file__).resolve().parents[3]
CONTRACT_VERSION = "nonprod-lifecycle-v1"
MAINTENANCE_CLASSES = {"enroll-recovery": "ENROLLMENT", "recover-vault": "RECOVERY"}
AUDIENCES = {
    "DEPLOYMENT": "tirodhan-nonprod-provisioning",
    "ENROLLMENT": "tirodhan-nonprod-enrollment",
    "RECOVERY": "tirodhan-nonprod-recovery",
}


class AdmissionDenied(RuntimeError):
    """A sanitized, fail-closed admission failure."""


def operation_class(operation: str) -> str:
    return MAINTENANCE_CLASSES.get(operation, "DEPLOYMENT")


def require_operation_policy(policy: dict[str, Any], operation: str) -> str:
    """Only the two explicitly approved purposes are independent of the deployment hold."""
    purpose = operation_class(operation)
    if (
        policy.get("schema_version") != 1
        or policy.get("contract_version") != CONTRACT_VERSION
        or not policy.get("authorization_keys")
        or (purpose == "DEPLOYMENT" and policy.get("release_enabled") is not True)
    ):
        raise AdmissionDenied("DEPLOYMENT_HOLD: operation is not authorized")
    return purpose


def maintenance_target(config: dict[str, Any]) -> dict[str, str]:
    tf = config["terraform"]
    return {
        key: tf[key]
        for key in ("tenant_id", "subscription_id", "resource_group_name", "location", "suffix")
    } | {
        "vault_id": f"/subscriptions/{tf['subscription_id']}/resourceGroups/"
        f"{tf['resource_group_name']}/providers/Microsoft.KeyVault/vaults/td-np-{tf['suffix']}-kv"
    }


def maintenance_operation_id(
    operation: str, principal: str, session: str, binding: dict[str, Any]
) -> str:
    # A token renewal/runner attempt is not another logical operation.
    return digest(
        {
            "class": operation_class(operation),
            "operation": operation,
            "principal": principal,
            "session": session,
            "binding": {k: v for k, v in binding.items() if k != "operation_id"},
        }
    )


def maintenance_binding_keys(operation: str) -> set[str]:
    common = {"target", "recovery_client_id", "recovery_principal_id", "operation_created_at"}
    if operation == "enroll-recovery":
        return common | {
            "local_execution_authorized",
            "recovery_enrollment_authorized",
            "recovery_app_object_id",
        }
    if operation == "recover-vault":
        return common | {"execution_environment", "recovery_authorized", "deleted_vault"}
    raise AdmissionDenied("UNKNOWN_MAINTENANCE_OPERATION")


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def contract_digest(root: Path = ROOT) -> str:
    """Bind authorization to executable infrastructure/admission definitions."""
    paths = sorted(
        {
            *root.glob("src/**/*.py"),
            *root.glob("alembic/**/*.py"),
            *root.glob("deploy/bootstrap/*"),
            *root.glob("deploy/nonprod-*.json"),
            *root.glob(".github/workflows/*.yml"),
            *root.glob(".github/actions/nonprod-*/*.yml"),
            *root.glob("infra/terraform/nonprod/*.tf"),
            *root.glob("infra/terraform/nonprod/*.tf.json"),
            *root.glob("infra/terraform/nonprod/*.hcl"),
        }
    )
    return digest(
        {
            p.relative_to(root).as_posix(): hashlib.sha256(
                p.read_bytes().replace(b"\r\n", b"\n")
            ).hexdigest()
            for p in paths
            if p.is_file()
            and not p.name.endswith(".tfvars.json")
            and p.suffix in {".py", ".json", ".yml", ".tf", ".hcl", ".sh"}
        }
    )


def configuration() -> dict[str, Any]:
    try:
        values = json.loads(os.environ["NONPROD_TFVARS_JSON"])
        if not isinstance(values, dict):
            raise ValueError
        expected = {
            "subscription_id": os.environ["ARM_SUBSCRIPTION_ID"],
            "tenant_id": os.environ["ARM_TENANT_ID"],
            "resource_group_name": os.environ["NONPROD_RESOURCE_GROUP"],
        }
        if any(str(values.get(k, "")).casefold() != v.casefold() for k, v in expected.items()):
            raise ValueError
        return {
            "terraform": values,
            "state_account": os.environ["STATE_ACCOUNT_NAME"],
            "state_resource_group": os.environ["STATE_RESOURCE_GROUP"],
            "deployment_client_id": os.environ["ARM_CLIENT_ID"],
            "recovery_client_id": os.environ.get("AZURE_NONPROD_RECOVERY_CLIENT_ID") or None,
            "recovery_principal_id": os.environ.get("AZURE_NONPROD_RECOVERY_PRINCIPAL_ID") or None,
            "database_verification_client_id": os.environ.get(
                "AZURE_NONPROD_DATABASE_VERIFY_CLIENT_ID"
            )
            or None,
            "database_verification_principal_id": os.environ.get(
                "AZURE_NONPROD_DATABASE_VERIFY_PRINCIPAL_ID"
            )
            or None,
        }
    except (ValueError, KeyError, TypeError) as exc:
        raise AdmissionDenied("CONFIGURATION_UNAVAILABLE_OR_INCONSISTENT") from exc


@dataclass(frozen=True)
class Authorization:
    operation: str
    principal_id: str
    identity_kind: str
    configuration_hash: str
    contract_hash: str
    commit: str
    run_id: str
    claims: dict[str, Any]


def validate_authorization(
    token: str,
    *,
    operation: str,
    policy: dict[str, Any],
    config_hash: str,
    code_hash: str,
    commit: str,
    run_id: str,
) -> Authorization:
    purpose = require_operation_policy(policy, operation)
    try:
        header = jwt.get_unverified_header(token)
        key = policy["authorization_keys"][header["kid"]]
        claims = jwt.decode(
            token,
            key,
            algorithms=["EdDSA"],
            audience=AUDIENCES[purpose],
            issuer="tirodhan-nonprod-release",
            options={"require": ["exp", "iat", "nbf", "sub", "jti", "authorization_class"]},
        )
        from tirodhan.deployment.prerequisites import IDENTITIES, REQUIRED

        if (
            not run_id
            or claims.get("repository") != "krishnadhoundiyal/tirodhan"
            or claims.get("commit") != commit
            or claims.get("run_id") != run_id
            or claims.get("contract_hash") != code_hash
            or claims.get("configuration_hash") != config_hash
            or not isinstance(claims.get("operations"), list)
            or operation not in claims["operations"]
            or any(op not in REQUIRED for op in claims["operations"])
            or len(set(claims["operations"])) != len(claims["operations"])
            or claims.get("identity_kind")
            not in {"operator", "deployment", "recovery", "database", "database_verification"}
            or not isinstance(claims.get("sub"), str)
            or not claims["sub"]
            or operation not in REQUIRED
            or claims["identity_kind"] != IDENTITIES.get(operation, "deployment")
            or not isinstance(claims.get("bindings"), dict)
            or not isinstance(claims["bindings"].get(operation), dict)
            or set(claims["bindings"]) != set(claims["operations"])
            or claims.get("validation", {}).get("ci_commit") != commit
            or claims.get("validation", {}).get("contract_hash") != code_hash
            or not claims.get("validation", {}).get("coderabbit_review")
            or claims.get("authorization_class") != purpose
            or any(operation_class(op) != purpose for op in claims["operations"])
        ):
            raise ValueError
        if purpose != "DEPLOYMENT":
            binding = claims["bindings"][operation]
            if (
                claims["operations"] != [operation]
                or set(claims["bindings"]) != {operation}
                or binding.get("operation_id")
                != maintenance_operation_id(operation, claims["sub"], run_id, binding)
                or not isinstance(binding.get("target"), dict)
                or set(binding) != maintenance_binding_keys(operation) | {"operation_id"}
                or type(binding.get("operation_created_at")) is not int
                or not 0 < binding["operation_created_at"] <= claims["iat"]
            ):
                raise ValueError
            UUID(claims["sub"])
            UUID(binding["recovery_principal_id"])
            UUID(binding["recovery_client_id"])
            if purpose == "RECOVERY":
                from tirodhan.deployment.recovery_target import timestamp, validate_deleted_context

                validate_deleted_context(binding["deleted_vault"], {"terraform": binding["target"]})
                if (
                    claims["exp"]
                    >= timestamp(binding["deleted_vault"]["scheduled_purge_date"]).timestamp()
                ):
                    raise ValueError
    except (jwt.PyJWTError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise AdmissionDenied("RELEASE_AUTHORIZATION_INVALID") from exc
    return Authorization(
        operation,
        claims["sub"],
        claims["identity_kind"],
        config_hash,
        code_hash,
        commit,
        run_id,
        claims,
    )


def validate_scheduled_context(authorization: Authorization) -> None:
    if os.environ.get("GITHUB_EVENT_NAME") != "schedule":
        return
    binding = authorization.claims.get("bindings", {}).get(authorization.operation, {})
    if (
        authorization.operation != "rotate-log-sas"
        or binding.get("scheduled_execution_authorized") is not True
        or not os.environ.get("TIRODHAN_SCHEDULE")
        or binding.get("schedule") != os.environ["TIRODHAN_SCHEDULE"]
    ):
        raise AdmissionDenied("SCHEDULED_OPERATION_NOT_AUTHORIZED")


def require_release_authorization(operation: str | None = None) -> Authorization:
    """Never accept caller-provided PASS flags or unsigned evidence."""
    try:
        policy = json.loads((ROOT / "deploy/nonprod-release-policy.json").read_text())
        # Check the hold before configuration, credentials, subprocesses or network access.
        requested = operation or os.environ.get("TIRODHAN_NONPROD_OPERATION", "")
        require_operation_policy(policy, requested)
        if os.environ.get("GITHUB_REF", "refs/heads/main") != "refs/heads/main":
            raise AdmissionDenied("UNTRUSTED_GIT_REF")
        from tirodhan.deployment.lifecycle_checks import Checks
        from tirodhan.deployment.prerequisites import CHECK_IDS, assert_manifest_complete

        assert_manifest_complete()
        if any(not callable(getattr(Checks, "check_" + key, None)) for key in CHECK_IDS):
            raise AdmissionDenied("MANDATORY_VALIDATOR_UNIMPLEMENTED")
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        authorization = validate_authorization(
            os.environ.get("TIRODHAN_NONPROD_RELEASE_AUTHORIZATION", ""),
            operation=requested,
            policy=policy,
            config_hash=digest(configuration()),
            code_hash=contract_digest(),
            commit=commit,
            run_id=os.environ.get("TIRODHAN_OPERATOR_RUN_ID")
            or os.environ.get("GITHUB_RUN_ID", ""),
        )
        validate_scheduled_context(authorization)
        return authorization
    except AdmissionDenied:
        raise
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        raise AdmissionDenied("DEPLOYMENT_ADMISSION_UNAVAILABLE") from exc


def main() -> None:
    try:
        authorization = require_release_authorization()
        purpose = operation_class(authorization.operation)
        print(
            "RELEASE_AUTHORIZATION_VALID"
            if purpose == "DEPLOYMENT"
            else f"{purpose}_AUTHORIZATION_VALID"
        )
    except AdmissionDenied as exc:
        raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    main()
