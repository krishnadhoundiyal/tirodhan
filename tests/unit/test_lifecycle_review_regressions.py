"""Review regressions at actual observation, workflow and side-effect boundaries."""

import io
import subprocess
import sys
from pathlib import Path

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


def test_runner_resolves_launcher_without_changing_arguments_or_timeout(monkeypatch) -> None:
    from tirodhan.deployment import lifecycle_evidence

    executable = r"C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin\az.cmd"
    monkeypatch.setattr(lifecycle_evidence.shutil, "which", lambda name: executable)
    observed = []

    def run(args, **kwargs):
        observed.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, '{"id":"verified"}', "")

    monkeypatch.setattr(subprocess, "run", run)
    assert Runner().json("az", "ad", "sp", "show", "--id", "exact-id") == {"id": "verified"}
    assert observed[0][0] == (executable, "ad", "sp", "show", "--id", "exact-id")
    assert observed[0][1]["timeout"] == 180
    assert not observed[0][1].get("shell", False)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows batch-launcher resolution")
def test_native_windows_path_launcher_is_executable(tmp_path, monkeypatch) -> None:
    directory = tmp_path / "installed CLI with spaces"
    directory.mkdir()
    helper = directory / "observe.py"
    helper.write_text("import json, sys; print(json.dumps(sys.argv[1:]))\n")
    (directory / "tirodhan-observation.cmd").write_text(f'@"{sys.executable}" "{helper}" %*\n')
    monkeypatch.setenv("PATH", str(directory))
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    expected = ['{"scope":"exact resource", "value":"read only"}', "exact-id"]
    assert Runner().json("tirodhan-observation", *expected) == expected


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
