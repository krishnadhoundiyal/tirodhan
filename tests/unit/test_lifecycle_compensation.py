"""Compensation must actually verify deletion, never relabel an exception as success."""

from __future__ import annotations

import copy
import subprocess
from pathlib import Path
from typing import Any

import pytest
from test_nonprod_lifecycle_contract import (
    SCOPE,
    VAULT,
    MemoryObservations,
    auth,
    state,
    write_plan,
)

from tirodhan.deployment import foundation_lifecycle as lifecycle
from tirodhan.deployment.deployment_admission import AdmissionDenied
from tirodhan.deployment.lifecycle_checks import Checks
from tirodhan.deployment.prerequisites import Controller

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
        automatic_owned_initial_apply_compensation=True,
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
