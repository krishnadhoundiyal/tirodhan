"""Compensation must actually verify deletion, never relabel an exception as success."""

from __future__ import annotations

import copy
import subprocess
from pathlib import Path
from typing import Any

import pytest
from lifecycle_fakes import (
    SCOPE,
    VAULT,
    MemoryObservations,
    auth,
    state,
    write_plan,
)

from tirodhan.deployment import foundation_lifecycle as lifecycle
from tirodhan.deployment import prerequisites
from tirodhan.deployment.deployment_admission import AdmissionDenied
from tirodhan.deployment.lifecycle_checks import Checks
from tirodhan.deployment.prerequisites import Controller, Grant

ADDRESS = "azurerm_container_app_environment.nonprod"
ENVIRONMENT = SCOPE + "/providers/Microsoft.App/managedEnvironments/tirodhan-np-test01-aca"


@pytest.fixture(autouse=True)
def isolated_authorized_local_context(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("GITHUB_ACTIONS", "KEY_VAULT_URL", "DEPLOYMENT_SCOPE"):
        monkeypatch.delenv(name, raising=False)


def case(tmp_path: Path) -> tuple[MemoryObservations, Any, dict[str, Any], Path]:
    io = MemoryObservations()
    io.current = state({ADDRESS: ENVIRONMENT}, serial=1)
    io.active = {VAULT, ENVIRONMENT}
    io.runner.plan = {
        "variables": copy.deepcopy(io.runner.plan["variables"]),
        "resource_changes": [
            {
                "address": ADDRESS,
                "mode": "managed",
                "type": "azurerm_container_app_environment",
                "change": {"actions": ["delete"], "before": {"id": ENVIRONMENT}},
            }
        ],
    }
    authorization = auth(
        "compensate-foundation",
        original_plan_sha256="a" * 64,
        clean_start_fingerprint=state().fingerprint,
        compensation_authorized=True,
        terminal_reconciliation_authorized=True,
    )
    baseline = {
        "classification": "CLEAN_INITIALIZED",
        "run_id": authorization.run_id,
        "configuration_hash": authorization.configuration_hash,
        "contract_hash": authorization.contract_hash,
        "state_fingerprint": state().fingerprint,
        "managed_ids": [],
        "disposable_ids": [],
        "original_plan_sha256": "a" * 64,
        "created_addresses": [ADDRESS],
    }
    plan = tmp_path / "compensation.tfplan"
    write_plan(plan, io.current)
    import hashlib

    authorization.claims["bindings"]["compensate-foundation"].update(
        plan_sha256=hashlib.sha256(plan.read_bytes()).hexdigest(),
        state_fingerprint=io.current.fingerprint,
    )
    return io, authorization, baseline, plan


@pytest.mark.parametrize(
    "invalid", ["partial", "foreign", "later-owned", "advanced-stage", "wrong-initial-plan"]
)
def test_ineligible_baselines_never_authorize_cleanup(tmp_path: Path, invalid: str) -> None:
    io, authorization, baseline, plan = case(tmp_path)
    if invalid == "partial":
        baseline["classification"] = "PARTIAL_OWNED"
    elif invalid == "foreign":
        io.active.add(ENVIRONMENT + "-foreign")
    elif invalid == "later-owned":
        io.current.resources["azurerm_container_app.later"] = (
            SCOPE + "/providers/Microsoft.App/containerApps/later"
        )
    elif invalid == "advanced-stage":
        io.current.document["outputs"]["deployment_stage"] = {"value": 1}
    else:
        baseline["original_plan_sha256"] = "b" * 64
    with pytest.raises(AdmissionDenied):
        Controller(
            authorization,
            Checks(
                authorization,
                io.config,
                {
                    "plan": plan,
                    "baseline": baseline,
                },
                io,
            ),
        ).evaluate("compensate-foundation")
    assert not any(command[:2] == ("terraform", "apply") for command in io.runner.commands)


@pytest.mark.parametrize(
    "outcome",
    [
        "success",
        "unverified-aca",
        "unexpected-error",
        "polling-error",
        "nonempty-terminal",
        "missing-vault",
        "lost-rbac",
    ],
)
def test_actual_compensation_postconditions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    outcome: str,
) -> None:
    io, authorization, baseline, plan = case(tmp_path)
    monkeypatch.setattr(lifecycle, "require_release_authorization", lambda _: authorization)
    monkeypatch.setattr(lifecycle, "configuration", lambda: io.config)
    monkeypatch.setattr(lifecycle, "Observations", lambda _: io)
    monkeypatch.setattr(lifecycle, "read_baseline", lambda *_: baseline)
    effects = []
    original_plan = copy.deepcopy(io.runner.plan)

    def native(*args: str, **_: Any) -> str:
        effects.append(args)
        if args[:2] == ("terraform", "plan"):
            io.runner.plan = (
                original_plan if outcome == "nonempty-terminal" else {"resource_changes": []}
            )
        elif args[:2] == ("terraform", "apply"):
            io.current = state(serial=2)
        else:
            raise AssertionError(args)
        return ""

    io.runner.run = native

    def child(args: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        effects.append(tuple(args))
        if args[0] == "terraform":
            if outcome == "unexpected-error":
                return subprocess.CompletedProcess(args, 1, "", "Error: denied")
            if outcome in {"polling-error", "nonempty-terminal"}:
                message = (
                    "Error: deleting Managed Environment Managed Environment Name: "
                    '"tirodhan-np-test01-aca" polling support for the Content-Type "" '
                    "was not implemented"
                )
                return subprocess.CompletedProcess(args, 1, "", message)
            io.current = state(serial=2)
            return subprocess.CompletedProcess(args, 0, "", "")
        if outcome == "unverified-aca":
            return subprocess.CompletedProcess(
                args, 1, "", "ENVIRONMENT_DELETION_DEADLINE_EXCEEDED"
            )
        io.active = {VAULT}
        if outcome == "missing-vault":
            io.vault_present = False
        if outcome == "lost-rbac":
            original_json = io.runner.json
            io.runner.json = lambda *a: (
                [] if a[:4] == ("az", "role", "assignment", "list") else original_json(*a)
            )
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(lifecycle.subprocess, "run", child)
    if outcome in {"success", "polling-error"}:
        lifecycle.compensate(plan)
        assert "CLEAN_COMPENSATION_VERIFIED" in capsys.readouterr().out
        assert not io.current.resources and io.active == {VAULT}
    else:
        with pytest.raises(AdmissionDenied):
            lifecycle.compensate(plan)
        assert "CLEAN_COMPENSATION_VERIFIED" not in capsys.readouterr().out
    if outcome == "unexpected-error":
        assert len(effects) == 1  # No recovery after an unrecognized error.
    if outcome == "nonempty-terminal":
        assert not any(
            "compensation-convergence.tfplan" in " ".join(e) and e[:2] == ("terraform", "apply")
            for e in effects
        )


@pytest.mark.parametrize(
    "classification,serialized,operations",
    [
        ("CLEAN_INITIALIZED", True, ["prepare-compensation", "compensate-foundation"]),
        ("CLEAN_INITIALIZED", False, ["prepare-compensation", "compensate-foundation"]),
        ("PARTIAL_OWNED", True, ["prepare-compensation", "compensate-foundation"]),
        ("CLEAN_INITIALIZED", True, ["prepare-compensation"]),
        ("CLEAN_INITIALIZED", True, ["compensate-foundation"]),
    ],
)
def test_apply_routes_cleanup_only_from_serialized_eligible_first_attempt(
    monkeypatch, tmp_path, classification, serialized, operations
) -> None:
    from unittest.mock import Mock

    io = MemoryObservations()
    authorization = auth("foundation-apply")
    authorization.claims["operations"] = operations
    checks = Checks(authorization, io.config, {}, io)
    monkeypatch.setattr(
        lifecycle,
        "admitted_with_grant",
        lambda *a, **k: (checks, io, Grant(authorization, "foundation-apply", None, (), {})),
    )
    monkeypatch.setattr(lifecycle, "record_baseline", lambda *a: {"classification": classification})
    monkeypatch.setattr(lifecycle, "automatic_compensation_context", lambda: serialized)
    monkeypatch.setattr(prerequisites, "require_release_authorization", lambda _: authorization)
    io.runner.run = Mock(side_effect=AdmissionDenied("APPLY_FAILED"))
    prepare, compensate = Mock(), Mock()
    monkeypatch.setattr(lifecycle, "prepare_compensation", prepare)
    monkeypatch.setattr(lifecycle, "compensate", compensate)
    with pytest.raises(AdmissionDenied):
        lifecycle.apply("foundation-apply", tmp_path / "plan")
    eligible = classification == "CLEAN_INITIALIZED" and serialized and len(operations) == 2
    assert prepare.called is eligible and compensate.called is eligible
    assert io.runner.run.call_count == 1


@pytest.mark.parametrize("operation", ["migration-apply", "runtime-apply"])
@pytest.mark.parametrize("evidence_current", [True, False])
def test_apply_revalidates_database_evidence_immediately_before_terraform(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operation: str,
    evidence_current: bool,
) -> None:
    from unittest.mock import Mock

    io = MemoryObservations()
    authorization = auth(operation)
    checks = Checks(authorization, io.config, {}, io)
    grant = Grant(authorization, operation, io.current.fingerprint, ("database",), {})
    monkeypatch.setattr(lifecycle, "admitted_with_grant", lambda *a, **k: (checks, io, grant))
    monkeypatch.setattr(prerequisites, "require_release_authorization", lambda _: authorization)
    monkeypatch.setattr(prerequisites, "configuration", lambda: io.config)
    monkeypatch.setattr(Checks, "check_database", lambda _: evidence_current)
    monkeypatch.setattr(checks, "check_migration_job", lambda: True)
    native_apply = Mock()
    io.runner.run = native_apply

    plan = tmp_path / "approved.tfplan"
    if evidence_current:
        lifecycle.apply(operation, plan)
        native_apply.assert_called_once_with(
            "terraform", "apply", "-input=false", str(plan), timeout=1800
        )
    else:
        with pytest.raises(AdmissionDenied, match="DATABASE_EVIDENCE_NO_LONGER_CURRENT"):
            lifecycle.apply(operation, plan)
        native_apply.assert_not_called()


@pytest.mark.parametrize("operation", ["foundation-apply", "migration-apply", "runtime-apply"])
@pytest.mark.parametrize("authorization_status", ["expired", "replaced", "current"])
def test_apply_refuses_expired_or_replaced_release_before_native_effect(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operation: str,
    authorization_status: str,
) -> None:
    from unittest.mock import Mock

    io = MemoryObservations()
    authorization = auth(operation)
    authorization.claims["operations"] = [
        operation,
        "prepare-compensation",
        "compensate-foundation",
    ]
    checks = Checks(authorization, io.config, {}, io)
    grant = Grant(authorization, operation, io.current.fingerprint, (), {})
    monkeypatch.setattr(lifecycle, "admitted_with_grant", lambda *a, **k: (checks, io, grant))
    monkeypatch.setattr(lifecycle, "record_baseline", lambda *_: {"classification": "CLEAN_INITIALIZED"})
    monkeypatch.setattr(lifecycle, "automatic_compensation_context", lambda: True)
    monkeypatch.setattr(checks, "check_foundation", lambda: True)
    monkeypatch.setattr(checks, "check_migration_job", lambda: True)
    monkeypatch.setattr(Checks, "check_database", lambda _: True)
    prepare, compensate, native = Mock(), Mock(), Mock()
    monkeypatch.setattr(lifecycle, "prepare_compensation", prepare)
    monkeypatch.setattr(lifecycle, "compensate", compensate)
    io.runner.run = native

    def authorized(_: str):
        if authorization_status == "expired":
            raise AdmissionDenied("RELEASE_AUTHORIZATION_INVALID")
        if authorization_status == "replaced":
            return auth(operation, replacement_claim=True)
        return authorization

    monkeypatch.setattr(prerequisites, "require_release_authorization", authorized)
    if authorization_status == "current":
        lifecycle.apply(operation, tmp_path / "reviewed.tfplan")
        native.assert_called_once()
    else:
        with pytest.raises(AdmissionDenied, match="RELEASE_AUTHORIZATION"):
            lifecycle.apply(operation, tmp_path / "reviewed.tfplan")
        native.assert_not_called()
    prepare.assert_not_called()
    compensate.assert_not_called()


def test_crash_replay_does_not_overwrite_existing_baseline_or_repeat_apply(
    monkeypatch, tmp_path
) -> None:
    from unittest.mock import Mock

    io = MemoryObservations()
    authorization = auth("foundation-apply", state_fingerprint=io.current.fingerprint)
    checks = Checks(authorization, io.config, {}, io)
    monkeypatch.setattr(
        lifecycle,
        "admitted_with_grant",
        lambda *a, **k: (checks, io, Grant(authorization, "foundation-apply", None, (), {})),
    )
    plan = tmp_path / "plan"
    plan.write_bytes(b"approved-plan")
    calls = []

    def existing(*args, **kwargs):
        calls.append(args)
        raise AdmissionDenied("BlobAlreadyExists")

    io.runner.run = existing
    compensate = Mock()
    monkeypatch.setattr(lifecycle, "compensate", compensate)
    with pytest.raises(AdmissionDenied, match="BlobAlreadyExists"):
        lifecycle.apply("foundation-apply", plan)
    assert len(calls) == 1 and calls[0][:4] == ("az", "storage", "blob", "upload")
    assert "--if-none-match" in calls[0]
    compensate.assert_not_called()


@pytest.mark.parametrize(
    "workflow,allowed",
    [
        ("nonprod-foundation.yml", True),
        ("nonprod-reconcile.yml", False),
        ("local", False),
    ],
)
def test_automatic_cleanup_requires_the_serialized_foundation_workflow(
    monkeypatch, tmp_path, workflow, allowed
) -> None:
    io, authorization, baseline, plan = case(tmp_path)
    authorization.claims["bindings"]["compensate-foundation"][
        "automatic_owned_initial_apply_compensation"
    ] = True
    monkeypatch.setenv("GITHUB_ACTIONS", "true" if workflow != "local" else "false")
    monkeypatch.setenv(
        "GITHUB_WORKFLOW_REF",
        f"krishnadhoundiyal/tirodhan/.github/workflows/{workflow}@refs/heads/main",
    )
    checks = Checks(authorization, io.config, {"baseline": baseline, "plan": plan}, io)
    assert checks.check_clean_baseline() is allowed
