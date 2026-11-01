"""PR #84 foundation lifecycle, preserving current main's deletion recovery protocol."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from tirodhan.deployment.check_destroy_plan import validate_destroy_plan
from tirodhan.deployment.deployment_admission import (
    AdmissionDenied,
    configuration,
    require_release_authorization,
)
from tirodhan.deployment.foundation_reconcile import admitted, admitted_with_grant
from tirodhan.deployment.lifecycle_checks import Checks, automatic_compensation_context
from tirodhan.deployment.lifecycle_evidence import Observations, classify_state
from tirodhan.deployment.prerequisites import Controller, revalidate_database


def verify_clean(io: Observations) -> None:
    state = io.backend()
    if state.kind != "INITIALIZED" or state.resources:
        raise AdmissionDenied("MANAGED_STATE_NOT_EMPTY")
    remaining = {r.casefold() for r in io.inventory()} - {io.vault_id.casefold()}
    if remaining:
        raise AdmissionDenied("DISPOSABLE_AZURE_RESOURCES_REMAIN")
    vault = io.arm(io.vault_id, "2023-07-01")
    if vault is None:
        raise AdmissionDenied("PERSISTENT_VAULT_NOT_PRESERVED")
    from tirodhan.deployment.destroy_preservation import verify_retained_vault

    verify_retained_vault(io)


def baseline_name(run_id: str, plan_hash: str) -> str:
    import re

    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", run_id) or not re.fullmatch(
        r"[a-f0-9]{64}", plan_hash
    ):
        raise AdmissionDenied("INVALID_BASELINE_IDENTITY")
    return f"lifecycle/baselines/{run_id}/{plan_hash}.json"


def verify_preserved_permissions(checks: Checks) -> None:
    try:
        if checks.check_permissions() is not True:
            raise AdmissionDenied("BOOTSTRAP_RBAC_NOT_PRESERVED")
    except (ValueError, KeyError, TypeError, AdmissionDenied) as exc:
        raise AdmissionDenied("BOOTSTRAP_RBAC_NOT_PRESERVED") from exc


def baseline_blob_args(io: Observations, name: str) -> tuple[str, ...]:
    return (
        "--account-name",
        io.config["state_account"],
        "--container-name",
        "tfstate",
        "--name",
        name,
        "--auth-mode",
        "login",
    )


def record_baseline(checks: Checks, plan: Path) -> dict[str, Any]:
    state = checks.io.backend()
    inventory = checks.io.inventory()
    classification = classify_state(
        state, inventory, app_scope=checks.io.scope, retained={checks.io.vault_id}
    )
    approved_plan = checks.io.runner.json("terraform", "show", "-json", str(plan))
    value: dict[str, Any] = {
        "classification": classification,
        "run_id": checks.auth.run_id,
        "configuration_hash": checks.auth.configuration_hash,
        "contract_hash": checks.auth.contract_hash,
        "state_fingerprint": state.fingerprint,
        "managed_ids": sorted(state.resources.values()),
        "disposable_ids": sorted(
            r for r in inventory if r.casefold() != checks.io.vault_id.casefold()
        ),
        "original_plan_sha256": hashlib.sha256(plan.read_bytes()).hexdigest(),
        "created_addresses": sorted(
            row["address"]
            for row in approved_plan.get("resource_changes", [])
            if row.get("mode", "managed") == "managed"
            and row.get("change", {}).get("actions") == ["create"]
        ),
    }
    if state.fingerprint != checks.binding().get("state_fingerprint"):
        raise AdmissionDenied("BASELINE_CHANGED_AFTER_AUTHORIZATION")
    if classification != "CLEAN_INITIALIZED":
        return value  # Owned partial resumes are explicitly ineligible for compensation.
    name = baseline_name(checks.auth.run_id, str(value["original_plan_sha256"]))
    with tempfile.TemporaryDirectory(prefix="tirodhan-clean-baseline-") as temporary:
        path = Path(temporary) / "baseline.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        checks.io.runner.run(
            "az",
            "storage",
            "blob",
            "upload",
            *baseline_blob_args(checks.io, name),
            "--file",
            str(path),
            "--if-none-match",
            "*",
            "--overwrite",
            "false",
            "--output",
            "none",
        )
    return value


def read_baseline(io: Observations, run_id: str, plan_hash: str) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="tirodhan-baseline-read-") as temporary:
        path = Path(temporary) / "baseline.json"
        io.runner.run(
            "az",
            "storage",
            "blob",
            "download",
            *baseline_blob_args(io, baseline_name(run_id, plan_hash)),
            "--file",
            str(path),
            "--no-progress",
            "--output",
            "none",
        )
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise AdmissionDenied("BASELINE_UNAVAILABLE")
        return value


def apply(operation: str, plan: Path) -> None:
    if operation not in {"foundation-apply", "migration-apply", "runtime-apply"}:
        raise AdmissionDenied("UNKNOWN_DEPLOYMENT_APPLY_OPERATION")
    checks, io, grant = admitted_with_grant(operation, plan=plan)
    baseline: dict[str, Any] | None = None
    if operation == "foundation-apply":
        baseline = record_baseline(checks, plan)
    try:
        # The admitted token can expire while preparing a foundation baseline or
        # waiting for the provider call. Fail closed before the Terraform effect.
        revalidate_database(grant)
        io.runner.run("terraform", "apply", "-input=false", str(plan), timeout=1800)
        if operation == "foundation-apply" and not checks.check_foundation():
            raise AdmissionDenied("FOUNDATION_POSTCONDITIONS_FAILED")
        if operation == "migration-apply" and not checks.check_migration_job():
            raise AdmissionDenied("MIGRATION_JOB_POSTCONDITIONS_FAILED")
    except AdmissionDenied:
        allowed = checks.auth.claims.get("operations", [])
        if (
            baseline is not None
            and baseline["classification"] == "CLEAN_INITIALIZED"
            and automatic_compensation_context()
            and {"prepare-compensation", "compensate-foundation"}.issubset(set(allowed))
        ):
            rollback_plan = plan.with_name("foundation-compensation.tfplan")
            prepare_compensation(rollback_plan)
            compensate(rollback_plan)
            raise AdmissionDenied("FOUNDATION_FAILED_COMPENSATION_VERIFIED") from None
        raise  # Partial/reused baselines never enter automatic destruction.
    print(f"VERIFIED_APPLY:{operation}")


def complete_aca_deletion(io: Observations, plan: Path) -> None:
    import sys

    document = io.runner.json("terraform", "show", "-json", str(plan))
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tirodhan.deployment.destroy_aca_environment",
            "--expected-subscription-id",
            io.tf["subscription_id"],
            "--expected-resource-group",
            io.tf["resource_group_name"],
            "--protected-state-account",
            io.config["state_account"],
            "--protected-state-container",
            "tfstate",
        ],
        input=json.dumps(document),
        text=True,
        capture_output=True,
        timeout=1850,
    )
    if result.returncode:
        raise AdmissionDenied("ACA_ABSENCE_NOT_CONFIRMED")


def compensate(plan: Path) -> None:
    auth = require_release_authorization("compensate-foundation")
    io = Observations(configuration())
    binding = auth.claims.get("bindings", {}).get("compensate-foundation", {})
    baseline = read_baseline(io, auth.run_id, binding.get("original_plan_sha256", ""))
    checks = Checks(auth, io.config, {"plan": plan, "baseline": baseline}, io)
    Controller(auth, checks).evaluate("compensate-foundation")
    result = subprocess.run(
        ["terraform", "apply", "-input=false", str(plan)],
        capture_output=True,
        text=True,
        timeout=1800,
    )
    if result.returncode:
        message = result.stdout + result.stderr
        expected_name = io.tf["resource_group_name"] + "-aca"
        if not (
            message.count("Error:") == 1
            and 'polling support for the Content-Type "" was not implemented' in message
            and "deleting Managed Environment" in message
            and f'Managed Environment Name: "{expected_name}"' in message
        ):
            raise AdmissionDenied("UNEXPECTED_COMPENSATION_APPLY_FAILURE")
    complete_aca_deletion(io, plan)
    remaining = {r.casefold() for r in io.inventory()} - {io.vault_id.casefold()}
    if remaining:
        raise AdmissionDenied("COMPENSATION_AZURE_RESOURCES_REMAIN")
    if result.returncode:
        if binding.get("terminal_reconciliation_authorized") is not True:
            raise AdmissionDenied("TERMINAL_RECONCILIATION_NOT_AUTHORIZED")
        require_release_authorization("compensate-foundation")
        terminal = plan.with_name("compensation-convergence.tfplan")
        io.runner.run(
            "terraform", "plan", "-destroy", "-input=false", f"-out={terminal}", timeout=1800
        )
        document = io.runner.json("terraform", "show", "-json", str(terminal))
        try:
            validate_destroy_plan(
                document,
                expected_subscription_id=io.tf["subscription_id"],
                expected_resource_group=io.tf["resource_group_name"],
                protected_state_account=io.config["state_account"],
                protected_state_container="tfstate",
                require_empty=True,
            )
        except RuntimeError as exc:
            raise AdmissionDenied("TERMINAL_RECONCILIATION_NOT_EMPTY") from exc
        # Only a zero-managed-change plan may record the resource-specific absence proof.
        io.runner.run("terraform", "apply", "-input=false", str(terminal), timeout=1800)
    verify_clean(io)
    verify_preserved_permissions(Checks(auth, io.config, {}, io))
    print("CLEAN_COMPENSATION_VERIFIED: original deployment remains failed")


def prepare_compensation(plan: Path) -> None:
    auth = require_release_authorization("prepare-compensation")
    io = Observations(configuration())
    binding = auth.claims.get("bindings", {}).get("prepare-compensation", {})
    baseline = read_baseline(io, auth.run_id, binding.get("original_plan_sha256", ""))
    checks = Checks(auth, io.config, {"baseline": baseline}, io)
    Controller(auth, checks).evaluate("prepare-compensation")
    io.runner.run("terraform", "plan", "-destroy", "-input=false", f"-out={plan}", timeout=1800)
    print("COMPENSATION_PLAN_PREPARED: no delete executed; explicit apply authorization required")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "operation",
        choices=[
            "foundation-plan",
            "foundation-apply",
            "migration-apply",
            "runtime-apply",
            "initialize-state-plan",
            "initialize-state-apply",
            "compensate-foundation",
            "prepare-compensation",
        ],
    )
    parser.add_argument("--plan", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.operation.startswith("initialize-state-"):
            from tirodhan.deployment.backend_setup import initialize

            initialize(args.operation, args.plan)
        elif args.operation == "foundation-plan":
            _, io = admitted(args.operation)
            io.runner.run("terraform", "plan", "-input=false", f"-out={args.plan}", timeout=1800)
        elif args.operation == "prepare-compensation":
            prepare_compensation(args.plan)
        elif args.operation == "compensate-foundation":
            compensate(args.plan)
        else:
            apply(args.operation, args.plan)
    except (AdmissionDenied, ValueError, OSError, KeyError):
        raise SystemExit("LIFECYCLE_BLOCKED: deployment or cleanup is not verified") from None


if __name__ == "__main__":
    main()
