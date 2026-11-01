"""No native backend command may implicitly initialize state before admission."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from lifecycle_fakes import CONFIG, RESOURCE, MemoryObservations, auth, state

from tirodhan.deployment import backend_setup as backend
from tirodhan.deployment import foundation_reconcile as reconcile
from tirodhan.deployment.deployment_admission import ROOT, AdmissionDenied
from tirodhan.deployment.lifecycle_checks import Checks
from tirodhan.deployment.lifecycle_evidence import BackendState
from tirodhan.deployment.prerequisites import Controller


@pytest.fixture
def observed(monkeypatch: pytest.MonkeyPatch) -> MemoryObservations:
    monkeypatch.chdir(ROOT / "infra/terraform/nonprod")
    for key in tuple(os.environ):
        if key.startswith("TF_") or key in {"GITHUB_ACTIONS", "DEPLOYMENT_SCOPE", "KEY_VAULT_URL"}:
            monkeypatch.delenv(key, raising=False)
    io = MemoryObservations()
    io.current = BackendState("STATE_ABSENT", None, None, {})
    monkeypatch.setattr(reconcile, "configuration", lambda: io.config)
    monkeypatch.setattr(reconcile, "Observations", lambda _: io)
    return io


def authorize(monkeypatch: pytest.MonkeyPatch, operation: str, **binding: Any) -> Any:
    authorization = auth(operation, initialization_authorized=True, **binding)
    monkeypatch.setattr(reconcile, "require_release_authorization", lambda _: authorization)
    monkeypatch.setattr(backend, "require_release_authorization", lambda _: authorization)
    return authorization


def test_intent_preparation_never_opens_native_backend(
    observed: MemoryObservations, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    authorize(monkeypatch, "initialize-state-plan")
    path = tmp_path / "intent.json"
    backend.initialize("initialize-state-plan", path)
    assert json.loads(path.read_text())["before_state_fingerprint"] == observed.current.fingerprint
    assert not any(
        c[:2] in {("terraform", "plan"), ("terraform", "init"), ("terraform", "state")}
        for c in observed.runner.commands
    )


@pytest.mark.parametrize(
    "outcome", ["success", "failed", "unknown", "missing-vault", "rbac", "foreign"]
)
def test_native_initialization_requires_intent_and_verifies_real_postconditions(
    observed: MemoryObservations, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, outcome: str
) -> None:
    authorize(monkeypatch, "initialize-state-plan")
    path = tmp_path / "intent.json"
    backend.initialize("initialize-state-plan", path)
    authorize(
        monkeypatch,
        "initialize-state-apply",
        intent_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        state_fingerprint=observed.current.fingerprint,
    )
    commands: list[tuple[str, ...]] = []

    def native(io: Any, *args: str, data_dir: str | None = None) -> None:
        assert data_dir is not None  # Fresh isolated native metadata, never local-state migration.
        commands.append(args)
        if outcome == "failed":
            raise AdmissionDenied("BACKEND_SETUP_OUTCOME_UNVERIFIED")
        if args[0] == "init" and outcome != "unknown":
            io.current = state()
            if outcome == "missing-vault":
                io.vault_present = False
            if outcome == "foreign":
                io.active.add(RESOURCE)
            if outcome == "rbac":
                monkeypatch.setattr(Checks, "check_permissions", lambda _: False)

    monkeypatch.setattr(backend, "native", native)
    if outcome == "success":
        backend.initialize("initialize-state-apply", path)
        assert [c[:2] for c in commands] == [("init", "-reconfigure"), ("state", "pull")]
        # Lost result, replay or a concurrent initializer never overwrites existing state.
        with pytest.raises(AdmissionDenied, match="PREREQUISITES_BLOCKED:initialization"):
            backend.initialize("initialize-state-apply", path)
        assert len(commands) == 2
    else:
        with pytest.raises(AdmissionDenied):
            backend.initialize("initialize-state-apply", path)


@pytest.mark.parametrize(
    "invalid", ["hash", "target", "fingerprint", "foreign", "initialized", "not-authorized"]
)
def test_invalid_initialization_has_no_native_side_effect(
    observed: MemoryObservations, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, invalid: str
) -> None:
    authorize(monkeypatch, "initialize-state-plan")
    path = tmp_path / "intent.json"
    backend.initialize("initialize-state-plan", path)
    if invalid == "target":
        value = json.loads(path.read_text())
        value["state_account_id"] += "-other"
        path.write_text(json.dumps(value))
    authorization = authorize(
        monkeypatch,
        "initialize-state-apply",
        intent_sha256="bad" if invalid == "hash" else hashlib.sha256(path.read_bytes()).hexdigest(),
        state_fingerprint="bad" if invalid == "fingerprint" else observed.current.fingerprint,
    )
    if invalid == "not-authorized":
        authorization.claims["bindings"]["initialize-state-apply"]["initialization_authorized"] = (
            False
        )
    if invalid == "foreign":
        observed.active.add(RESOURCE)
    if invalid == "initialized":
        observed.current = state()
    monkeypatch.setattr(
        backend, "native", lambda *a, **k: pytest.fail("unauthorized native access")
    )
    with pytest.raises(AdmissionDenied):
        backend.initialize("initialize-state-apply", path)


@pytest.mark.parametrize("kind", ["STATE_ABSENT", "BACKEND_ABSENT", "INITIALIZED"])
def test_regular_setup_demands_actual_initialized_backend(
    observed: MemoryObservations, kind: str
) -> None:
    observed.current = state() if kind == "INITIALIZED" else BackendState(kind, None, None, {})
    checks = Checks(auth("terraform-setup"), observed.config, {}, observed)
    if kind == "INITIALIZED":
        Controller(checks.auth, checks).evaluate("terraform-setup")
    else:
        with pytest.raises(AdmissionDenied):
            Controller(checks.auth, checks).evaluate("terraform-setup")
    assert not any(c[:2] == ("terraform", "init") for c in observed.runner.commands)


@pytest.mark.parametrize(
    "override", ["TF_CLI_ARGS_init", "TF_DATA_DIR", "TF_WORKSPACE", "ARM_ACCESS_KEY", "ARM_USE_MSI"]
)
def test_unsafe_backend_context_blocks_before_native_access(
    observed: MemoryObservations, monkeypatch: pytest.MonkeyPatch, override: str
) -> None:
    monkeypatch.setenv(override, "unsafe")
    assert not backend.initialization_context(observed)


@pytest.mark.parametrize(
    "key,value",
    [
        ("TF_CLI_ARGS_apply", "-lock=false"),
        ("TF_CLI_ARGS_plan", "-lock=false"),
        ("TF_WORKSPACE", "unapproved"),
        ("TF_DATA_DIR", "/unapproved"),
        ("TF_CLI_CONFIG_FILE", "/unapproved"),
    ],
)
def test_direct_terraform_routes_cannot_change_workspace_or_disable_locks(
    observed: MemoryObservations, monkeypatch: pytest.MonkeyPatch, key: str, value: str
) -> None:
    monkeypatch.setenv(key, value)
    checks = Checks(auth("foundation-plan"), observed.config, {}, observed)
    assert not checks.check_terraform_context()
    assert observed.runner.commands == []


@pytest.mark.parametrize("consumer", ["setup", "existing-backend"])
def test_unverified_inputs_block_every_context_consumer(observed, consumer):
    observed.inputs_verified = False
    checks = Checks(auth("foundation-plan"), observed.config, {}, observed)
    accepted = (
        backend.initialization_context(observed)
        if consumer == "setup"
        else checks.check_terraform_context()
    )
    assert accepted is False
    assert observed.runner.commands == []


@pytest.mark.parametrize("failure", ["exit", "timeout"])
def test_native_failure_is_sanitized_and_never_claimed_success(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    def run(*args: Any, **kwargs: Any) -> Any:
        if failure == "timeout":
            raise subprocess.TimeoutExpired("private-state", 300)
        return subprocess.CompletedProcess([], 1, b"private-state", b"private-credential")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(AdmissionDenied, match="^BACKEND_SETUP_OUTCOME_UNVERIFIED$"):
        backend.native(MemoryObservations(), "init")


@pytest.mark.parametrize("initialized", [True, False])
def test_teardown_setup_is_independent_of_hold_but_cannot_initialize_state(
    observed: MemoryObservations, monkeypatch: pytest.MonkeyPatch, initialized: bool
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv(
        "GITHUB_WORKFLOW_REF",
        "krishnadhoundiyal/tirodhan/.github/workflows/nonprod-destroy.yml@refs/heads/main",
    )
    monkeypatch.setenv("TIRODHAN_EXECUTION_ENVIRONMENT", "nonprod-plan")
    monkeypatch.setattr(backend, "configuration", lambda: observed.config)
    monkeypatch.setattr(backend, "Observations", lambda _: observed)
    monkeypatch.setattr(
        backend,
        "require_release_authorization",
        lambda _: pytest.fail("teardown must remain available"),
    )
    observed.vault_present = False  # Run #8's initialized-empty legacy baseline remains valid.
    if initialized:
        observed.current = state()
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(backend, "native", lambda io, *args, **kwargs: commands.append(args))
    if initialized:
        backend.setup("destroy")
        assert len(commands) == 1
    else:
        with pytest.raises(AdmissionDenied, match="DESTROY_BACKEND_NOT_INITIALIZED"):
            backend.setup("destroy")
        assert commands == []


def test_teardown_mode_cannot_bypass_provisioning_workflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv(
        "GITHUB_WORKFLOW_REF",
        "krishnadhoundiyal/tirodhan/.github/workflows/nonprod-deploy.yml@refs/heads/main",
    )
    with pytest.raises(AdmissionDenied, match="DESTROY_SETUP_CONTEXT_INVALID"):
        backend.setup("destroy")


def test_source_dominates_all_native_backend_setup_with_admission() -> None:
    import yaml

    action = yaml.safe_load((ROOT / ".github/actions/nonprod-setup/action.yml").read_text())
    script = action["runs"]["steps"][-1]["run"]
    assert "deployment.backend_setup" in script and "terraform init" not in script
    for path in (ROOT / ".github/workflows").glob("*.yml"):
        value = yaml.safe_load(path.read_text())
        for job in value.get("jobs", {}).values():
            for step in job.get("steps", []):
                script = step.get("run", "")
                if "terraform init" in script:
                    assert "-backend=false" in script, path
                if step.get("uses") == "./.github/actions/nonprod-setup":
                    assert (step.get("with", {}).get("mode") == "destroy") == (
                        path.name == "nonprod-destroy.yml"
                    )


@pytest.mark.parametrize("lineage,serial", [("", 0), (" ", 0), ("valid", -1), ("valid", True)])
def test_empty_resources_do_not_validate_missing_backend_identity(
    lineage: str, serial: int
) -> None:
    from tirodhan.deployment.lifecycle_evidence import state_resources

    with pytest.raises(AdmissionDenied, match="INVALID_STATE_IDENTITY"):
        state_resources({"version": 4, "resources": [], "lineage": lineage, "serial": serial})


@pytest.mark.parametrize("cached_workspace", [None, "default", "unapproved", ""])
def test_cached_workspace_cannot_override_the_bound_backend(
    monkeypatch, tmp_path, cached_workspace
):
    from tirodhan.deployment import lifecycle_evidence

    project = tmp_path / "infra/terraform/nonprod"
    data = project / ".terraform"
    data.mkdir(parents=True)
    (data / "terraform.tfstate").write_text(json.dumps({"backend": {"type": "azurerm"}}))
    if cached_workspace is not None:
        (data / "environment").write_text(cached_workspace)
    monkeypatch.setattr(lifecycle_evidence, "ROOT", tmp_path)
    monkeypatch.chdir(project)
    monkeypatch.delenv("TF_DATA_DIR", raising=False)
    io = lifecycle_evidence.Observations(CONFIG)
    if cached_workspace in (None, "default"):
        assert io.terraform_context() == {"type": "azurerm"}
    else:
        with pytest.raises(AdmissionDenied, match="TERRAFORM_WORKSPACE_CONTEXT_MISMATCH"):
            io.terraform_context()


@pytest.mark.parametrize(
    "change",
    [
        None,
        "stage",
        "missing",
        "changed",
        "terraform.tfvars",
        "terraform.tfvars.json",
        "extra.auto.tfvars",
        "extra.auto.tfvars.json",
        "ambient",
    ],
)
def test_real_input_reader_rejects_unsigned_parameter_overlays(monkeypatch, tmp_path, change):
    from tirodhan.deployment import lifecycle_evidence

    project = tmp_path / "infra/terraform/nonprod"
    project.mkdir(parents=True)
    approved = project / "nonprod.auto.tfvars.json"
    approved.write_text(json.dumps(CONFIG["terraform"]))
    monkeypatch.setattr(lifecycle_evidence, "ROOT", tmp_path)
    monkeypatch.chdir(project)
    for key in tuple(os.environ):
        if key.startswith("TF_VAR_"):
            monkeypatch.delenv(key)
    if change == "missing":
        approved.unlink()
    elif change == "changed":
        approved.write_text(
            json.dumps(CONFIG["terraform"] | {"runtime_env": {"unreviewed": "true"}})
        )
    elif change in {
        "terraform.tfvars",
        "terraform.tfvars.json",
        "extra.auto.tfvars",
        "extra.auto.tfvars.json",
    }:
        (project / change).write_text("{}")
    elif change == "ambient":
        monkeypatch.setenv("TF_VAR_runtime_env", '{"unreviewed":"true"}')
    elif change == "stage":
        monkeypatch.setenv("TF_VAR_deployment_stage", "0")
    io = lifecycle_evidence.Observations(CONFIG)
    if change in (None, "stage"):
        assert io.terraform_inputs()
    else:
        with pytest.raises(AdmissionDenied):
            io.terraform_inputs()
