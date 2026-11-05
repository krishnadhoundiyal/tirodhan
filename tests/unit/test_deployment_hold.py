"""The hold stops supported provisioning entry points before credentials or effects."""

from __future__ import annotations

import json
import os
import shutil
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
@pytest.mark.parametrize("enabled,token", [(False, ""), (True, ""), (True, "not-a-jwt")])
def test_cli_denies_without_inputs_or_azure_authentication(
    module: str, enabled: bool, token: str, tmp_path: Path
) -> None:
    # Exercise the real entry points with both policy states without changing the checkout.
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    policy = json.loads(Path("deploy/nonprod-release-policy.json").read_text())
    policy["release_enabled"] = enabled
    (deploy / "nonprod-release-policy.json").write_text(json.dumps(policy))
    shutil.copyfile("deploy/nonprod-prerequisites.json", deploy / "nonprod-prerequisites.json")
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("AZURE_", "ARM_", "TIRODHAN_", "GITHUB_", "NONPROD_", "STATE_"))
    }
    env["PYTHONPATH"] = str(Path("src").resolve())
    # A forged Boolean is not authorization.
    env.update(RELEASE_AUTHORIZATION_VALID="true", PREREQUISITES_SATISFIED="true")
    sub = "00000000-0000-0000-0000-000000000001"
    tenant = "00000000-0000-0000-0000-000000000004"
    env.update(
        ARM_SUBSCRIPTION_ID=sub,
        ARM_TENANT_ID=tenant,
        ARM_CLIENT_ID="00000000-0000-0000-0000-000000000003",
        NONPROD_RESOURCE_GROUP="tirodhan-np-test01",
        STATE_ACCOUNT_NAME="syntheticstate",
        STATE_RESOURCE_GROUP="synthetic-state-rg",
        GITHUB_REF="refs/heads/main",
        NONPROD_TFVARS_JSON=json.dumps(
            {
                "subscription_id": sub,
                "tenant_id": tenant,
                "resource_group_name": "tirodhan-np-test01",
            }
        ),
        TIRODHAN_NONPROD_OPERATION="foundation-plan",
        TIRODHAN_NONPROD_RELEASE_AUTHORIZATION=token,
    )
    child = """
import pathlib, platform, runpy, subprocess, sys
from tirodhan.deployment import deployment_admission as admission
admission.ROOT = pathlib.Path(sys.argv[1])
target = sys.argv[2]
# Prime Python's benign OS probe before blocking all effect subprocesses.
platform.processor()
def guard(command, *args, **kwargs):
    if list(command) == ['git', 'rev-parse', 'HEAD']:
        return subprocess.CompletedProcess(command, 0, stdout='a' * 40 + '\\n', stderr='')
    raise AssertionError('External command reached before admission: ' + str(command[0]))
subprocess.run = guard
sys.argv = [target]
if target == 'deployment_admission':
    admission.main()
else:
    runpy.run_module('tirodhan.deployment.' + target, run_name='__main__')
"""
    result = subprocess.run(
        [sys.executable, "-c", child, str(tmp_path), module],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode != 0
    assert ("RELEASE_AUTHORIZATION_INVALID" if enabled else "DEPLOYMENT_HOLD") in result.stderr
    assert "Traceback" not in result.stderr


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
    assert any("deployment_admission" in step.get("run", "") for step in admission["steps"])

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
