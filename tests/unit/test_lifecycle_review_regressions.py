"""Review regressions at actual observation, workflow and side-effect boundaries."""

import io
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from lifecycle_fakes import VAULT, MemoryObservations, auth

from tirodhan.deployment import destroy_preservation
from tirodhan.deployment.deployment_admission import AdmissionDenied, validate_scheduled_context
from tirodhan.deployment.lifecycle_evidence import ObservationError, Runner


@pytest.mark.parametrize(
    "stderr,code",
    [
        ("ERROR: (ResourceNotFound) hidden-body", "ResourceNotFound"),
        ('ERROR: {"error":{"code":"ResourceNotFound","message":"hidden"}}', "ResourceNotFound"),
        ("ERROR: hidden-body\nErrorCode:BlobNotFound", "BlobNotFound"),
        ("ERROR: (AuthorizationFailed) hidden-body", "AuthorizationFailed"),
        ("ERROR: token=must-not-be-printed", "COMMAND_FAILED"),
    ],
)
def test_real_runner_recognizes_only_structured_failure_codes(monkeypatch, stderr, code) -> None:
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 1, "", stderr)
    )
    with pytest.raises(ObservationError, match="^" + code + "$"):
        Runner().run("az", "rest")


def test_runner_resolves_launcher_without_changing_arguments_or_timeout(
    tmp_path, monkeypatch
) -> None:
    from tirodhan.deployment import lifecycle_evidence

    installed = tmp_path / "installed CLI with spaces"
    (installed / "wbin").mkdir(parents=True)
    interpreter = installed / "python.exe"
    interpreter.touch()
    executable = installed / "wbin" / "az.cmd"
    monkeypatch.setattr(lifecycle_evidence.shutil, "which", lambda name: str(executable))
    monkeypatch.setattr(lifecycle_evidence, "os", SimpleNamespace(name="nt"))
    observed = []

    def run(args, **kwargs):
        observed.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, '{"id":"verified"}', "")

    monkeypatch.setattr(subprocess, "run", run)
    assert Runner().json("az", "ad", "sp", "show", "--id", "exact-id") == {"id": "verified"}
    assert observed[0][0] == (
        str(interpreter),
        "-IBm",
        "azure.cli",
        "ad",
        "sp",
        "show",
        "--id",
        "exact-id",
    )
    assert observed[0][1]["timeout"] == 180
    assert not observed[0][1].get("shell", False)


@pytest.mark.parametrize("launcher", ["unknown.cmd", "az.bat", "wbin/az.cmd"])
def test_unverified_batch_installation_never_runs_a_shell(tmp_path, monkeypatch, launcher) -> None:
    from tirodhan.deployment import lifecycle_evidence

    monkeypatch.setattr(lifecycle_evidence.shutil, "which", lambda name: str(tmp_path / launcher))
    monkeypatch.setattr(lifecycle_evidence, "os", SimpleNamespace(name="nt"))

    def forbidden(*args, **kwargs):
        pytest.fail("Unverified batch launcher executed")

    monkeypatch.setattr(subprocess, "run", forbidden)
    with pytest.raises(ObservationError, match="^UNSUPPORTED_BATCH_LAUNCHER$"):
        Runner().run("az", "version")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows MSI Azure CLI argument preservation")
def test_native_windows_azure_cli_preserves_json_and_shell_metacharacters() -> None:
    launcher = shutil.which("az")
    if not launcher or not (Path(launcher).parent.parent / "python.exe").is_file():
        pytest.skip("Official Azure CLI MSI installation is not available")
    expected = [
        '{"scope":"exact resource", "value":"read only"}',
        "https://example.invalid/read?a=1&b=2",
        "%USERNAME%",
        "literal&argument",
    ]
    query = "`" + json.dumps(expected) + "`"
    assert Runner().json("az", "version", "--query", query, "-o", "json") == expected


@pytest.mark.parametrize(
    "initial,expectation,final,passes",
    [
        (True, "true", True, True),
        (True, "true", False, False),
        (False, "false", False, True),
        (False, "", False, False),
    ],
)
def test_empty_final_inventory_honors_observed_retention_expectation(
    monkeypatch, capsys, initial, expectation, final, passes
) -> None:
    observations = MemoryObservations()
    observations.vault_present = initial
    observations.active = {VAULT} if initial else set()
    assert destroy_preservation.retention_required(observations) is initial
    observations.vault_present = final
    observations.active = {VAULT} if final else set()
    monkeypatch.setattr(destroy_preservation, "configuration", lambda: observations.config)
    monkeypatch.setattr(destroy_preservation, "Observations", lambda _: observations)
    monkeypatch.setattr(destroy_preservation.sys, "stdin", io.StringIO(""))
    monkeypatch.setattr(destroy_preservation.sys, "argv", ["preservation", "--verify-expectation"])
    monkeypatch.setenv("REQUIRE_PERSISTENT_VAULT", expectation)
    destroy_preservation.main()
    output = capsys.readouterr().out.strip()
    assert (output == "") is passes


@pytest.mark.parametrize(
    "approved,schedule,operation",
    [
        (True, "23 4 */3 * *", "rotate-log-sas"),
        (False, "23 4 */3 * *", "rotate-log-sas"),
        (True, "different", "rotate-log-sas"),
        (True, "23 4 */3 * *", "foundation-apply"),
    ],
)
def test_schedule_requires_exact_explicit_rotator_authority(
    monkeypatch, approved, schedule, operation
) -> None:
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    monkeypatch.setenv("TIRODHAN_SCHEDULE", "23 4 */3 * *")
    authorization = auth(operation, scheduled_execution_authorized=approved, schedule=schedule)
    if approved and schedule == "23 4 */3 * *" and operation == "rotate-log-sas":
        validate_scheduled_context(authorization)
    else:
        with pytest.raises(AdmissionDenied, match="SCHEDULED_OPERATION_NOT_AUTHORIZED"):
            validate_scheduled_context(authorization)


def test_every_offline_admission_uses_pinned_python_before_install() -> None:
    for path in Path(".github/workflows").glob("*.yml"):
        job = yaml.safe_load(path.read_text()).get("jobs", {}).get("release-admission")
        if job:
            steps = job["steps"]
            install = next(i for i, s in enumerate(steps) if "pip install" in s.get("run", ""))
            assert any(
                s.get("uses") == "actions/setup-python@v5" and s["with"]["python-version"] == "3.12"
                for s in steps[:install]
            )
