"""Supported provisioning routes must be dominated by admission and preserve teardown."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tirodhan.deployment.deployment_admission import contract_digest
from tirodhan.deployment.lifecycle_checks import Checks
from tirodhan.deployment.prerequisites import CHECK_IDS, assert_manifest_complete
from tirodhan.deployment.release_setup import validate_policy_keys


def test_all_terraform_configuration_formats_are_bound_to_release_source(tmp_path: Path) -> None:
    directory = tmp_path / "infra/terraform/nonprod"
    directory.mkdir(parents=True)
    (directory / "main.tf").write_text("terraform {}")
    baseline = contract_digest(tmp_path)
    variables = directory / "nonprod.auto.tfvars.json"
    variables.write_text('{"suffix":"reviewed-separately"}')
    assert contract_digest(tmp_path) == baseline
    override = directory / "local_override.tf.json"
    override.write_text('{"provider":{"azurerm":{"subscription_id":"other"}}}')
    changed = contract_digest(tmp_path)
    assert changed != baseline
    override.write_text('{"provider":{"azurerm":{"subscription_id":"another"}}}')
    assert contract_digest(tmp_path) != changed


def test_all_mandatory_capabilities_have_real_registered_evaluators() -> None:
    assert_manifest_complete()
    assert all(callable(getattr(Checks, "check_" + key, None)) for key in CHECK_IDS)
    policy = json.loads(Path("deploy/nonprod-release-policy.json").read_text())
    assert policy["release_enabled"] is False
    # Public-key enrollment is allowed while the independent release hold remains active.
    validate_policy_keys(policy)


def test_workflow_mappings_cannot_silently_overwrite_gates() -> None:
    class UniqueKeysLoader(yaml.SafeLoader):
        pass

    def mapping(loader, node, deep=False):
        pairs = loader.construct_pairs(node, deep=deep)
        keys = [key for key, _ in pairs]
        assert len(keys) == len(set(keys)), keys
        return dict(pairs)

    UniqueKeysLoader.add_constructor("tag:yaml.org,2002:map", mapping)
    for path in Path(".github").rglob("*.yml"):
        yaml.load(path.read_text(), Loader=UniqueKeysLoader)


@pytest.mark.parametrize(
    "operation",
    [
        "foundation-plan",
        "foundation-apply",
        "migration-apply",
        "runtime-apply",
        "initialize-state-plan",
        "initialize-state-apply",
        "prepare-compensation",
        "compensate-foundation",
    ],
)
def test_all_lifecycle_routes_held_without_credentials(operation: str, tmp_path: Path) -> None:
    env = {
        **os.environ,
        "PYTHONPATH": str(Path("src").resolve()),
        "RELEASE_AUTHORIZATION_VALID": "true",
        "PREREQUISITES_SATISFIED": "true",
    }
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tirodhan.deployment.foundation_lifecycle",
            operation,
            "--plan",
            str(tmp_path / "absent.tfplan"),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode != 0
    assert "LIFECYCLE_BLOCKED" in result.stderr
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "operation", ["recover-vault", "enroll-recovery", "plan-transfer", "transfer-vault"]
)
def test_privileged_routes_held_without_credentials(operation: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "tirodhan.deployment.foundation_reconcile", operation],
        env={**os.environ, "PYTHONPATH": str(Path("src").resolve())},
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode != 0
    assert "BLOCKED_RECONCILIATION" in result.stderr


def test_every_azure_workflow_job_depends_on_offline_admission_except_protected_destroy() -> None:
    for path in Path(".github/workflows").glob("*.yml"):
        value = yaml.safe_load(path.read_text())
        jobs = value.get("jobs", {})

        def guarded(name: str, graph: dict = jobs) -> bool:
            needs = graph[name].get("needs", [])
            if isinstance(needs, str):
                needs = [needs]
            return "release-admission" in needs or any(guarded(n, graph) for n in needs)

        for name, job in jobs.items():
            cloud = any(
                "azure/login" in s.get("uses", "") or "nonprod-setup" in s.get("uses", "")
                for s in job.get("steps", [])
            )
            if cloud and path.name != "nonprod-destroy.yml":
                assert guarded(name), (path, name)


def test_architecture_and_teardown_invariants_remain_explicit() -> None:
    text = Path(".github/workflows/nonprod-destroy.yml").read_text()
    assert "--require-empty" in text and "destroy_aca_environment" in text
    assert "deployment_admission" not in text
    assert 'actual_state_id="$(az storage account show' in text
    assert 'az group show \\\n            --name "$STATE_RESOURCE_GROUP"' not in text
    assert "check_nonprod_bootstrap_rbac verify" in text
    assert "terraform state rm" not in text
    terraform = Path("infra/terraform/nonprod/ownership_transfer.tf").read_text()
    assert "destroy = false" in terraform
    vault = Path("infra/terraform/nonprod/key_vault.tf").read_text()
    assert 'data "azurerm_key_vault" "runtime"' in vault
    providers = Path("infra/terraform/nonprod/providers.tf").read_text()
    assert 'resource_provider_registrations = "none"' in providers
