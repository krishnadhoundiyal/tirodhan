"""Offline operator authorizer for the existing NONPROD admission contract.

This module neither changes GitHub settings nor authenticates to Azure. Evidence is
collected separately on a trusted operator host, then reviewed before offline signing.
"""

from __future__ import annotations

import argparse
import copy
import getpass
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from tirodhan.deployment.database_evidence import verification_key_is_independent
from tirodhan.deployment.deployment_admission import (
    AUDIENCES,
    ROOT,
    AdmissionDenied,
    contract_digest,
    digest,
    maintenance_binding_keys,
    maintenance_operation_id,
    maintenance_target,
    operation_class,
    require_operation_policy,
    validate_authorization,
)
from tirodhan.deployment.lifecycle_evidence import BackendState, plan_prior_state, state_resources
from tirodhan.deployment.release_custody import (
    generate_pair,
    key_id,
    load_private,
    operator_only,
    private_directory,
    private_read,
    private_write,
    public_pem,
)

REPOSITORY = "krishnadhoundiyal/tirodhan"
DESTINATIONS = {
    "release": ("NONPROD_RELEASE_AUTHORIZATION", "repository", "deployment", "nonprod-plan"),
    "apply": ("NONPROD_APPLY_AUTHORIZATION", "nonprod", "deployment", "nonprod"),
    "database": (
        "NONPROD_DATABASE_VERIFY_AUTHORIZATION",
        "nonprod-database",
        "database_verification",
        "nonprod-database",
    ),
    "enrollment": ("NONPROD_RELEASE_AUTHORIZATION", "operator-local", "operator", "operator-local"),
    "recovery": ("NONPROD_RELEASE_AUTHORIZATION", "repository", "recovery", "nonprod-recovery"),
}
PLAN_OPERATIONS = ["terraform-setup", "foundation-plan", "migration-plan", "runtime-plan"]
CONSUMER_OPERATIONS = [
    "migration-plan",
    "migration-apply",
    "migration-start",
    "runtime-plan",
    "runtime-apply",
]


def expected_bindings(kind: str, apply_operation: str | None = None) -> dict[str, Any]:
    if kind == "enrollment":
        return {
            "enroll-recovery": {
                "local_execution_authorized": True,
                "recovery_enrollment_authorized": True,
            }
        }
    if kind == "recovery":
        return {
            "recover-vault": {
                "execution_environment": "nonprod-recovery",
                "recovery_authorized": True,
            }
        }
    if kind == "release":
        return {
            operation: {"execution_environment": "nonprod-plan"} for operation in PLAN_OPERATIONS
        }
    if kind == "database":
        return {
            "database-verify": {
                "execution_environment": "nonprod-database",
                "database_verification_authorized": True,
                "consumer_operations": CONSUMER_OPERATIONS,
            }
        }
    if kind != "apply" or apply_operation not in {
        "foundation-apply",
        "migration-apply",
        "runtime-apply",
    }:
        raise AdmissionDenied("ISSUANCE_OPERATION_NOT_SUPPORTED")
    result: dict[str, Any] = {
        "terraform-setup": {"execution_environment": "nonprod"},
        apply_operation: {"execution_environment": "nonprod"},
    }
    if apply_operation == "migration-apply":
        result["migration-start"] = {"execution_environment": "nonprod"}
    return result


def validate_policy_keys(policy: dict[str, Any]) -> None:
    for value in policy.get("authorization_keys", {}).values():
        if not isinstance(serialization.load_pem_public_key(value.encode()), Ed25519PublicKey):
            raise AdmissionDenied("RELEASE_KEY_MUST_BE_ED25519")
    for name, row in policy.get("database_evidence_keys", {}).items():
        UUID(row["principal_id"])
        if row.get("identity_kind") not in {"database", "database_verification"}:
            raise AdmissionDenied("DATABASE_KEY_IDENTITY_INVALID")
        if not verification_key_is_independent(policy, name):
            raise AdmissionDenied("DATABASE_KEY_MUST_BE_INDEPENDENT")


def policy_proposal(
    policy: dict[str, Any], enrollment: dict[str, Any], revoke: list[str]
) -> dict[str, Any]:
    """Public-only proposal. Existing entries cannot be silently replaced."""
    proposal = copy.deepcopy(policy)
    for group in ("authorization_keys", "database_evidence_keys"):
        for name, value in enrollment.get(group, {}).items():
            previous = proposal[group].get(name)
            if previous is not None and previous != value:
                old = previous if group == "authorization_keys" else previous["public_key"]
                new = value if group == "authorization_keys" else value["public_key"]
                try:
                    a = serialization.load_pem_public_key(old.encode())
                    b = serialization.load_pem_public_key(new.encode())
                    equivalent = (
                        isinstance(a, Ed25519PublicKey)
                        and isinstance(b, Ed25519PublicKey)
                        and (
                            a.public_bytes(
                                serialization.Encoding.Raw, serialization.PublicFormat.Raw
                            )
                            == b.public_bytes(
                                serialization.Encoding.Raw, serialization.PublicFormat.Raw
                            )
                        )
                    )
                except (ValueError, TypeError):
                    equivalent = False
                if group == "database_evidence_keys":
                    equivalent = equivalent and {
                        k: v for k, v in previous.items() if k != "public_key"
                    } == {k: v for k, v in value.items() if k != "public_key"}
                if not equivalent:
                    raise AdmissionDenied("KEY_ID_ALREADY_ENROLLED_WITH_DIFFERENT_VALUE")
            if previous is None:
                proposal[group][name] = value
        for name in revoke:
            proposal[group].pop(name, None)
    if any(
        name not in policy["authorization_keys"] and name not in policy["database_evidence_keys"]
        for name in revoke
    ):
        raise AdmissionDenied("REVOCATION_KEY_NOT_ENROLLED")
    proposal["release_enabled"] = False
    validate_policy_keys(proposal)
    return proposal


def validate_evidence(evidence: dict[str, Any], commit: str) -> dict[str, Any]:
    """Validate collected records, not caller supplied PASS/status assertions."""
    pull = evidence["pull"]
    source = pull["head"]["sha"]
    if (
        evidence["repository"] != REPOSITORY
        or evidence["target_commit"] != commit
        or pull["base"]["repo"]["full_name"] != REPOSITORY
        or pull["head"]["repo"]["full_name"] != REPOSITORY
        or pull["base"]["ref"] != "main"
        or pull.get("merged") is not True
        or pull.get("merge_commit_sha") != commit
        or evidence["target_tree"] != evidence["source_tree"]
    ):
        raise AdmissionDenied("REVIEWED_MAIN_PROVENANCE_INVALID")
    workflows = {".github/workflows/pr-checks.yml", ".github/workflows/nonprod-lifecycle-ci.yml"}
    runs = evidence["workflow_runs"]
    if len(runs) != len(workflows) or {r["path"] for r in runs} != workflows:
        raise AdmissionDenied("REQUIRED_CI_EVIDENCE_MISSING")
    for run in runs:
        if (
            run.get("head_sha") != source
            or run.get("status") != "completed"
            or run.get("conclusion") != "success"
            or run.get("event") != "pull_request"
            or run.get("repository", {}).get("full_name") != REPOSITORY
            or run.get("head_repository", {}).get("full_name") != REPOSITORY
            or not isinstance(run.get("id"), int)
        ):
            raise AdmissionDenied("CI_EVIDENCE_INVALID")
    review = evidence["review"]
    if (
        review.get("commit_id") != source
        or review.get("user", {}).get("login") != "coderabbitai[bot]"
        or review.get("user", {}).get("id") != 136622811
        or review.get("state") not in {"COMMENTED", "APPROVED"}
        or not review.get("submitted_at")
        or type(review.get("id")) is not int
        or evidence.get("unresolved_threads") != []
    ):
        raise AdmissionDenied("CODERABBIT_EVIDENCE_INVALID_OR_UNRESOLVED")
    return {
        "ci_commit": commit,
        "contract_hash": evidence["contract_hash"],
        "coderabbit_review": f"https://github.com/{REPOSITORY}/pull/{pull['number']}"
        f"#pullrequestreview-{review['id']}",
        "ci_run_ids": [r["id"] for r in runs],
        "reviewed_source_commit": source,
        "reviewed_tree": evidence["source_tree"],
        "evidence_sha256": digest(evidence),
    }


def state_binding(request: dict[str, Any], plan: Path) -> dict[str, Any]:
    snapshot = request["state"]
    state = BackendState(
        snapshot["kind"],
        snapshot["document"],
        snapshot["etag"],
        state_resources(snapshot["document"]),
    )
    if state.kind != "INITIALIZED" or not isinstance(state.etag, str) or not state.etag:
        raise AdmissionDenied("AUTHORITATIVE_INITIALIZED_STATE_REQUIRED")
    prior = plan_prior_state(plan)
    if any(prior.get(k) != snapshot["document"].get(k) for k in ("lineage", "serial")):
        raise AdmissionDenied("PLAN_STATE_IDENTITY_CHANGED")
    plan_hash = hashlib.sha256(plan.read_bytes()).hexdigest()
    review = request["plan_review"]
    if review != {
        "plan_sha256": plan_hash,
        "state_fingerprint": state.fingerprint,
        "lineage": snapshot["document"]["lineage"],
        "serial": snapshot["document"]["serial"],
        "etag": state.etag,
        "reviewed_by_operator": True,
    }:
        raise AdmissionDenied("PLAN_REVIEW_OR_STATE_CHANGED")
    return {
        "plan_sha256": plan_hash,
        "state_fingerprint": state.fingerprint,
        "state_lineage": prior["lineage"],
        "state_serial": prior["serial"],
        "state_etag": state.etag,
    }


def validated_request(
    request: dict[str, Any],
    policy: dict[str, Any],
    config: dict[str, Any],
    *,
    code_hash: str,
    commit: str,
    plan: Path | None = None,
    fresh_capture: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    kind = request["kind"]
    profiles = expected_bindings(kind, request.get("apply_operation"))
    for operation in profiles:
        require_operation_policy(policy, operation)
    secret, destination, identity, environment = DESTINATIONS[kind]
    now = int(time.time())
    age = policy["maximum_evidence_age_seconds"]
    if (
        request["schema_version"] != 1
        or request["repository"] != REPOSITORY
        or request["environment"] != environment
        or request["destination"] != destination
        or request["secret_name"] != secret
        or request["identity_kind"] != identity
        or request["commit"] != commit
        or request["configuration_hash"] != digest(config)
        or request["contract_hash"] != code_hash
        or request["evidence"]["contract_hash"] != code_hash
        or type(request["captured_at"]) is not int
        or request["captured_at"] > now
        or (fresh_capture and now - request["captured_at"] > age)
        or type(request["expires_at"]) is not int
        or request["expires_at"] <= now
        or not request["release_session"]
    ):
        raise AdmissionDenied("ISSUANCE_CONTEXT_CHANGED_OR_EXPIRED")
    UUID(request["release_session"])
    UUID(request["principal_id"])
    identity_evidence = request["principal"]
    client = config.get(
        "database_verification_client_id"
        if kind == "database"
        else "recovery_client_id"
        if kind == "recovery"
        else "deployment_client_id"
    )
    if (
        identity_evidence["id"] != request["principal_id"]
        or (kind != "enrollment" and (not client or identity_evidence["appId"] != client))
        or identity_evidence["tenant_id"] != config["terraform"]["tenant_id"]
        or (
            kind == "database"
            and request["principal_id"] != config["database_verification_principal_id"]
        )
    ):
        raise AdmissionDenied("ISSUANCE_PRINCIPAL_NOT_AUTHORIZED")
    validate_policy_keys(policy)
    if kind == "database":
        row = policy["database_evidence_keys"].get(request["database_key_id"], {})
        if (
            row.get("principal_id") != request["principal_id"]
            or row.get("identity_kind") != identity
        ):
            raise AdmissionDenied("SQL_VERIFIER_KEY_NOT_ENROLLED")
    bindings = expected_bindings(kind, request.get("apply_operation"))
    if request.get("operations") != sorted(bindings):
        raise AdmissionDenied("ISSUANCE_OPERATIONS_INCONSISTENT")
    if kind in {"enrollment", "recovery"}:
        operation = next(iter(bindings))
        binding = copy.deepcopy(request["maintenance_binding"])
        if (
            binding.get("target") != maintenance_target(config)
            or set(binding) != maintenance_binding_keys(operation)
            or any(binding.get(k) != v for k, v in bindings[operation].items())
            or binding.get("recovery_client_id") != config.get("recovery_client_id")
            or not config.get("recovery_client_id")
            or type(binding.get("operation_created_at")) is not int
            or not 0 < binding["operation_created_at"] <= request["captured_at"]
            or config["recovery_client_id"]
            in {config["deployment_client_id"], config.get("database_verification_client_id")}
        ):
            raise AdmissionDenied("MAINTENANCE_TARGET_OR_PROFILE_INVALID")
        UUID(binding["recovery_principal_id"])
        UUID(binding["recovery_client_id"])
        if kind == "enrollment":
            UUID(binding["recovery_app_object_id"])
            if binding["recovery_principal_id"] == request["principal_id"]:
                raise AdmissionDenied("ENROLLMENT_REQUIRES_SEPARATE_OPERATOR")
        elif binding["recovery_principal_id"] != request["principal_id"]:
            raise AdmissionDenied("RECOVERY_PRINCIPAL_MISMATCH")
        if kind == "recovery":
            from tirodhan.deployment.recovery_target import timestamp, validate_deleted_context

            validate_deleted_context(binding["deleted_vault"], config)
            if (
                request["expires_at"]
                >= timestamp(binding["deleted_vault"]["scheduled_purge_date"]).timestamp()
            ):
                raise AdmissionDenied("RECOVERY_AUTHORIZATION_OUTLIVES_DELETED_TARGET")
        binding["operation_id"] = maintenance_operation_id(
            operation, request["principal_id"], request["release_session"], binding
        )
        bindings[operation] = binding
    if kind == "apply":
        if plan is None:
            raise AdmissionDenied("REVIEWED_SAVED_PLAN_REQUIRED")
        bindings[request["apply_operation"]].update(state_binding(request, plan))
    validation = validate_evidence(request["evidence"], commit)
    validation["request_sha256"] = digest(request)
    return bindings, validation


def issue(
    request: dict[str, Any],
    *,
    expected_request_hash: str,
    policy: dict[str, Any],
    config: dict[str, Any],
    code_hash: str,
    commit: str,
    private_key: Path,
    password: bytes,
    output: Path,
    plan: Path | None = None,
) -> dict[str, Any]:
    operator_only()
    if digest(request) != expected_request_hash:
        raise AdmissionDenied("OPERATOR_REVIEW_DIGEST_MISMATCH")
    bindings, validation = validated_request(
        request, policy, config, code_hash=code_hash, commit=commit, plan=plan
    )
    private_directory(output.parent)
    if os.path.lexists(output):
        raise AdmissionDenied("ISSUANCE_OUTPUT_ALREADY_EXISTS")
    key = load_private(private_key, password)
    kid = key_id(key)
    if policy["authorization_keys"].get(kid) != public_pem(key):
        raise AdmissionDenied("RELEASE_SIGNER_NOT_ENROLLED")
    now = int(time.time())
    claims = {
        "iss": "tirodhan-nonprod-release",
        "aud": AUDIENCES[operation_class(next(iter(bindings)))],
        "authorization_class": operation_class(next(iter(bindings))),
        "iat": now,
        "nbf": now,
        "exp": request["expires_at"],
        "jti": str(uuid4()),
        "sub": request["principal_id"],
        "repository": REPOSITORY,
        "commit": commit,
        "run_id": request["release_session"],
        "configuration_hash": digest(config),
        "contract_hash": code_hash,
        "operations": sorted(bindings),
        "identity_kind": request["identity_kind"],
        "bindings": bindings,
        "validation": validation,
    }
    token = jwt.encode(claims, key, algorithm="EdDSA", headers={"kid": kid})
    for operation in bindings:
        validate_authorization(
            token,
            operation=operation,
            policy=policy,
            config_hash=digest(config),
            code_hash=code_hash,
            commit=commit,
            run_id=request["release_session"],
        )
    journal = private_key.parent / "issuance-journal"
    if not journal.exists():
        try:
            private_directory(journal, create=True)
        except FileExistsError:
            pass
    # Claim before writing a credential. A crash leaves an audit claim and blocks reissue.
    audit = {
        "request_sha256": expected_request_hash,
        "authorization_id": claims["jti"],
        "key_id": kid,
        "secret_name": request["secret_name"],
        "destination": request["destination"],
        "commit": commit,
        "configuration_hash": digest(config),
        "contract_hash": code_hash,
        "operations": sorted(bindings),
        "expires_at": request["expires_at"],
        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
    }
    try:
        private_write(journal / f"{expected_request_hash}.json", json.dumps(audit).encode())
    except FileExistsError:
        raise AdmissionDenied("ISSUANCE_REQUEST_ALREADY_CLAIMED") from None
    private_write(output, token.encode())
    return audit


def readiness(policy: dict[str, Any], config: dict[str, Any] | None) -> dict[str, Any]:
    issues = []
    if policy.get("release_enabled") is not True:
        issues.append("DEPLOYMENT_HOLD")
    if not policy.get("authorization_keys"):
        issues.append("RELEASE_PUBLIC_KEY_NOT_ENROLLED")
    if config is None:
        issues.append("AUTHORITATIVE_CONFIGURATION_UNAVAILABLE")
    pinned = config is not None and any(
        row.get("identity_kind") == "database_verification"
        and row.get("principal_id") == config.get("database_verification_principal_id")
        for row in policy.get("database_evidence_keys", {}).values()
    )
    if not pinned:
        issues.append("SQL_VERIFIER_PUBLIC_KEY_NOT_ENROLLED")
    validate_policy_keys(policy)
    return {
        "status": "BLOCKED" if issues else "SOURCE_KEYS_ENROLLED_ONLY",
        "missing": issues,
        "unverified": [
            "GITHUB_SECRET_VALUES",
            "AZURE_PERMISSIONS",
            "FRESH_SQL_EVIDENCE",
            "GOOGLE_NO_COST_ELIGIBILITY_AND_QUOTAS",
            "OPERATION_PREREQUISITES",
        ],
        "configuration_hash": digest(config) if config is not None else None,
        "contract_hash": contract_digest(),
        "provisioning_authorized": False,
    }


def _password(*, confirm: bool = False) -> bytes:
    if not sys.stdin.isatty():
        raise AdmissionDenied("INTERACTIVE_OPERATOR_TERMINAL_REQUIRED")
    value = getpass.getpass("Custody passphrase (never stored): ")
    if not value or (confirm and value != getpass.getpass("Confirm custody passphrase: ")):
        raise AdmissionDenied("CUSTODY_PASSPHRASE_NOT_CONFIRMED")
    return value.encode()


def verify_token(
    token: str,
    request: dict[str, Any],
    policy: dict[str, Any],
    config: dict[str, Any],
    *,
    code_hash: str,
    commit: str,
    plan: Path | None = None,
) -> None:
    bindings, validation = validated_request(
        request, policy, config, code_hash=code_hash, commit=commit, plan=plan, fresh_capture=False
    )
    for operation in bindings:
        authorization = validate_authorization(
            token,
            operation=operation,
            policy=policy,
            config_hash=digest(config),
            code_hash=code_hash,
            commit=commit,
            run_id=request["release_session"],
        )
        if (
            authorization.principal_id != request["principal_id"]
            or authorization.claims["validation"] != validation
            or authorization.claims["exp"] != request["expires_at"]
            or authorization.claims["bindings"] != bindings
            or sorted(authorization.claims["operations"]) != sorted(bindings)
        ):
            raise AdmissionDenied("TOKEN_DOES_NOT_MATCH_REVIEWED_REQUEST")


def main() -> None:
    from tirodhan.deployment.deployment_admission import configuration
    from tirodhan.deployment.lifecycle_evidence import Runner

    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    keys = commands.add_parser("keygen")
    keys.add_argument("--directory", type=Path, required=True)
    keys.add_argument("--verifier-principal", required=True)
    proposal = commands.add_parser("policy-proposal")
    proposal.add_argument("--enrollment", type=Path)
    proposal.add_argument("--revoke", action="append", default=[])
    proposal.add_argument("--output", type=Path, required=True)
    commands.add_parser("readiness")
    for name in ("issue", "verify"):
        signing = commands.add_parser(name)
        signing.add_argument("--request", type=Path, required=True)
        signing.add_argument("--plan", type=Path)
        if name == "issue":
            signing.add_argument("--reviewed-request-sha256", required=True)
            signing.add_argument("--private-key", type=Path, required=True)
            signing.add_argument("--output", type=Path, required=True)
        else:
            signing.add_argument("--token-file", type=Path, required=True)
    args = parser.parse_args()
    try:
        operator_only()
        if args.command == "keygen":
            generated = generate_pair(
                args.directory, _password(confirm=True), args.verifier_principal
            )
            print(
                json.dumps(
                    {
                        "status": "ENCRYPTED_KEYS_VALIDATED",
                        "key_ids": {group: list(entries) for group, entries in generated.items()},
                    }
                )
            )
            return
        policy = json.loads((ROOT / "deploy/nonprod-release-policy.json").read_text())
        if args.command == "policy-proposal":
            public = json.loads(private_read(args.enrollment)) if args.enrollment else {}
            value = policy_proposal(policy, public, args.revoke)
            private_write(args.output, json.dumps(value, indent=2).encode())
            print("PUBLIC_POLICY_PROPOSAL_WRITTEN: hold retained; no enrollment performed")
        elif args.command == "readiness":
            try:
                config = configuration()
            except AdmissionDenied:
                config = None
            print(json.dumps(readiness(policy, config), indent=2))
        else:
            request = json.loads(private_read(args.request))
            config = configuration()
            commit = Runner().run("git", "-C", str(ROOT), "rev-parse", "HEAD")
            # Check everything, including the hold, before prompting to unlock the key.
            validated_request(
                request,
                policy,
                config,
                code_hash=contract_digest(),
                commit=commit,
                plan=args.plan,
                fresh_capture=args.command != "verify",
            )
            if args.command == "verify":
                verify_token(
                    private_read(args.token_file).decode(),
                    request,
                    policy,
                    config,
                    code_hash=contract_digest(),
                    commit=commit,
                    plan=args.plan,
                )
                print("AUTHORIZATION_VERIFIED: exact reviewed operation context")
                return
            if digest(request) != args.reviewed_request_sha256:
                raise AdmissionDenied("OPERATOR_REVIEW_DIGEST_MISMATCH")
            print(
                json.dumps(
                    {
                        "request_sha256": digest(request),
                        "secret_name": request["secret_name"],
                        "destination": request["destination"],
                        "principal_id": request["principal_id"],
                        "commit": commit,
                        "configuration_hash": digest(config),
                        "contract_hash": contract_digest(),
                        "release_session": request["release_session"],
                        "operations": request["operations"],
                        "expires_at": request["expires_at"],
                    },
                    indent=2,
                )
            )
            if not sys.stdin.isatty() or input("Type ISSUE followed by the request SHA-256: ") != (
                "ISSUE " + args.reviewed_request_sha256
            ):
                raise AdmissionDenied("EXACT_ISSUANCE_REVIEW_NOT_CONFIRMED")
            audit = issue(
                request,
                expected_request_hash=args.reviewed_request_sha256,
                policy=policy,
                config=config,
                code_hash=contract_digest(),
                commit=commit,
                private_key=args.private_key,
                password=_password(),
                output=args.output,
                plan=args.plan,
            )
            print(json.dumps({"status": "AUTHORIZATION_WRITTEN_AND_VERIFIED", **audit}, indent=2))
    except AdmissionDenied as exc:
        raise SystemExit(str(exc)) from None
    except Exception:
        # Never print exception messages/arguments: parsers may contain raw credential material.
        raise SystemExit(
            "RELEASE_SETUP_BLOCKED: no successful enrollment or issuance reported"
        ) from None


if __name__ == "__main__":
    main()
