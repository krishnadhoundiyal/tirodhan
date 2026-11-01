"""The hold stops supported provisioning entry points before credentials or effects."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


@pytest.mark.parametrize(
    "module",
    [
        "deployment_admission",
        "release",
        "bootstrap_database",
        "seed_secrets",
        "seed_ghcr_pull_pat",
        "rotate_log_sas",
    ],
)
def test_cli_denies_without_inputs_or_azure_authentication(module: str) -> None:
    env = {key: value for key, value in os.environ.items() if not key.startswith("AZURE_")}
    env["PYTHONPATH"] = str(Path("src").resolve())
    # A forged Boolean is not authorization.
    env.update(RELEASE_AUTHORIZATION_VALID="true", PREREQUISITES_SATISFIED="true")
    result = subprocess.run(
        [sys.executable, "-m", f"tirodhan.deployment.{module}"],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode != 0
    assert "DEPLOYMENT_HOLD" in result.stderr


@pytest.mark.parametrize(
    "name", ["nonprod-deploy", "nonprod-foundation", "rotate-log-sas", "seed-ghcr-pull-pat"]
)
def test_workflow_admission_precedes_every_credentialed_job(name: str) -> None:
    workflow = yaml.safe_load(Path(f".github/workflows/{name}.yml").read_text())
    jobs = workflow["jobs"]
    admission = jobs["release-admission"]
    assert admission["permissions"] == {"contents": "read"}
    assert "environment" not in admission
    assert all("azure/login" not in str(step) for step in admission["steps"])
    assert any("deployment_admission.py" in step.get("run", "") for step in admission["steps"])

    def depends_on_admission(job: str) -> bool:
        needs = jobs[job].get("needs", [])
        if isinstance(needs, str):
            needs = [needs]
        return "release-admission" in needs or any(depends_on_admission(n) for n in needs)

    for job in jobs:
        if job != "release-admission":
            assert depends_on_admission(job), job


def test_teardown_remains_independently_available() -> None:
    text = Path(".github/workflows/nonprod-destroy.yml").read_text()
    assert "deployment_admission" not in text
    assert "Complete and verify approved ACA environment deletion" in text
    assert "--require-empty" in text
