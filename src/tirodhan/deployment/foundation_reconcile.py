"""PR #84 recovery and snapshot-backed ownership transfer, with separate admission."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from typing import Any

from tirodhan.deployment.deployment_admission import (
    AdmissionDenied,
    configuration,
    digest,
    require_release_authorization,
)
from tirodhan.deployment.lifecycle_checks import Checks
from tirodhan.deployment.lifecycle_evidence import BackendState, Observations, state_resources
from tirodhan.deployment.prerequisites import Controller, Grant, emit_grant


def admitted_with_grant(
    operation: str, **parameters: Any
) -> tuple[Checks, Observations, Grant]:
    """Retain the verified admission grant for checks at the mutation boundary."""
    auth = require_release_authorization(operation)
    from tirodhan.deployment.database_verification_request import refresh_database_evidence

    refresh_database_evidence(auth)
    io = Observations(configuration())
    checks = Checks(auth, io.config, parameters, io)
    grant = Controller(auth, checks).evaluate(operation)
    emit_grant(grant)
    return checks, io, grant


def admitted(operation: str, **parameters: Any) -> tuple[Checks, Observations]:
    """Preserve the existing two-value contract for non-apply lifecycle operations."""
    checks, io, _ = admitted_with_grant(operation, **parameters)
    return checks, io


def recover_vault() -> None:
    checks, io = admitted("recover-vault")
    if checks.check_vault():
        print("PERSISTENT_VAULT_READY: verified active vault; no mutation")
        return
    io.runner.run(
        "az",
        "keyvault",
        "recover",
        "--subscription",
        io.tf["subscription_id"],
        "--name",
        io.vault_id.rsplit("/", 1)[-1],
        "--resource-group",
        io.tf["resource_group_name"],
        "--location",
        io.tf["location"],
        "--output",
        "none",
    )
    # No optimistic success. Recovery postconditions are new actual observations.
    if not checks.check_vault():
        raise AdmissionDenied("RECOVERY_POSTCONDITIONS_NOT_SATISFIED")
    print("PERSISTENT_VAULT_RECOVERED: reevaluate deployment prerequisites under D")


def blob_args(io: Observations) -> tuple[str, ...]:
    return (
        "--account-name",
        io.config["state_account"],
        "--container-name",
        "tfstate",
        "--name",
        "tirodhan/nonprod.tfstate",
        "--auth-mode",
        "login",
    )


def snapshot_state(io: Observations, expected_fingerprint: str) -> dict[str, Any]:
    before = io.backend()
    if before.fingerprint != expected_fingerprint or before.etag is None:
        raise AdmissionDenied("STATE_CHANGED_BEFORE_BACKUP")
    snapshot = io.runner.json(
        "az",
        "storage",
        "blob",
        "snapshot",
        *blob_args(io),
        "--if-match",
        before.etag,
        "--output",
        "json",
    )
    snapshot_id = snapshot.get("snapshot")
    if not isinstance(snapshot_id, str) or not snapshot_id:
        raise AdmissionDenied("RECOVERABLE_BACKUP_MISSING")
    return {
        "snapshot": snapshot_id,
        "state_fingerprint": before.fingerprint,
        "original_etag": before.etag,
    }


def read_backup(io: Observations, backup: dict[str, Any]) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="tirodhan-verified-backup-") as temporary:
        path = Path(temporary) / "backup.json"
        io.runner.run(
            "az",
            "storage",
            "blob",
            "download",
            *blob_args(io),
            "--snapshot",
            backup["snapshot"],
            "--file",
            str(path),
            "--no-progress",
            "--output",
            "none",
        )
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict):
            raise AdmissionDenied("RECOVERABLE_BACKUP_INVALID")
        return value


def plan_transfer(plan: Path, backup_path: Path) -> None:
    checks, io = admitted("state-backup")
    state = checks.backend()
    io.runner.run(
        "terraform",
        "plan",
        "-input=false",
        "-refresh=false",
        "-target=azurerm_key_vault.runtime",
        f"-out={plan}",
        timeout=1800,
    )
    if io.backend().fingerprint != state.fingerprint:
        raise AdmissionDenied("STATE_CHANGED_DURING_TRANSFER_PLANNING")
    backup = snapshot_state(io, state.fingerprint)
    recovered = read_backup(io, backup)
    if digest(recovered) != digest(state.document):
        raise AdmissionDenied("BACKUP_CONTENT_MISMATCH")
    backup_path.write_text(json.dumps(backup), encoding="utf-8")
    print("OWNERSHIP_TRANSFER_PLANNED: verified snapshot; explicit transfer authorization required")


def transfer_vault(plan: Path, backup_path: Path) -> None:
    auth = require_release_authorization("transfer-vault")
    io = Observations(configuration())
    backup = json.loads(backup_path.read_text())
    recovered = read_backup(io, backup)
    checks = Checks(
        auth, io.config, {"plan": plan, "backup": backup, "backup_state": recovered}, io
    )
    before = checks.backend()
    if "azurerm_key_vault.runtime" not in before.resources:
        original = BackendState(
            "INITIALIZED", recovered, backup.get("original_etag"), state_resources(recovered)
        )
        expected = {k: v for k, v in original.resources.items() if k != "azurerm_key_vault.runtime"}
        if (
            original.fingerprint != checks.binding().get("state_fingerprint")
            or before.resources != expected
            or (before.document or {}).get("lineage") != recovered.get("lineage")
            or (before.document or {}).get("serial", -1) <= recovered.get("serial", -1)
            or not all(
                (
                    checks.check_target(),
                    checks.check_identity(),
                    checks.check_execution_authority(),
                    checks.check_permissions(),
                    checks.check_vault(),
                )
            )
        ):
            raise AdmissionDenied("OWNERSHIP_TRANSFER_REPLAY_UNVERIFIED")
        print("LEGACY_VAULT_OWNERSHIP_ALREADY_TRANSFERRED: verified no-op")
        return
    emit_grant(Controller(auth, checks).evaluate("transfer-vault"))
    # Terraform's saved-plan lineage/serial checks and native backend lease arbitrate writers.
    # No state rm, raw editing, force-unlock or lock=false escape hatch exists.
    io.runner.run("terraform", "apply", "-input=false", str(plan), timeout=1800)
    after = io.backend()
    expected = {k: v for k, v in before.resources.items() if k != "azurerm_key_vault.runtime"}
    if after.resources != expected or not checks.check_vault():
        raise AdmissionDenied("OWNERSHIP_TRANSFER_POSTCONDITION_FAILED")
    print("LEGACY_VAULT_OWNERSHIP_TRANSFERRED: backup retained; reevaluate prerequisites")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "operation", choices=["recover-vault", "enroll-recovery", "plan-transfer", "transfer-vault"]
    )
    parser.add_argument("--plan", type=Path, default=Path("vault-transfer.tfplan"))
    parser.add_argument("--backup", type=Path, default=Path("vault-transfer-backup.json"))
    args = parser.parse_args()
    try:
        if args.operation == "enroll-recovery":
            from tirodhan.deployment.recovery_enrollment import enroll

            enroll()
        elif args.operation == "recover-vault":
            recover_vault()
        elif args.operation == "plan-transfer":
            plan_transfer(args.plan, args.backup)
        else:
            transfer_vault(args.plan, args.backup)
    except (AdmissionDenied, OSError, ValueError, KeyError):
        raise SystemExit(
            "BLOCKED_RECONCILIATION: no unverified lifecycle operation is allowed"
        ) from None


if __name__ == "__main__":
    main()
