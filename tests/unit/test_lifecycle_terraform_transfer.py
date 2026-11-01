"""Verify the real pinned Terraform forget-plan protocol without any apply or Azure access."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


def test_targeted_ownership_plan_forgets_only_exact_binding_without_state_mutation(
    tmp_path: Path,
) -> None:
    binary = os.environ.get("TIRODHAN_TEST_TERRAFORM_BINARY")
    if not binary:
        pytest.skip("Pinned Terraform supplied by lifecycle CI or explicit local test invocation")
    (tmp_path / "main.tf").write_text("""
terraform { required_version = "= 1.13.5" }
removed {
  from = terraform_data.old
  lifecycle { destroy = false }
}
resource "terraform_data" "unrelated" { input = "must-not-be-created" }
""")
    document = {
        "version": 4,
        "terraform_version": "1.13.5",
        "serial": 1,
        "lineage": "00000000-0000-0000-0000-000000000001",
        "outputs": {},
        "resources": [
            {
                "mode": "managed",
                "type": "terraform_data",
                "name": "old",
                "provider": 'provider["terraform.io/builtin/terraform"]',
                "instances": [
                    {
                        "schema_version": 0,
                        "attributes": {
                            "id": "old-id",
                            "input": None,
                            "output": None,
                            "triggers_replace": None,
                        },
                        "sensitive_attributes": [],
                    }
                ],
            }
        ],
    }
    state = tmp_path / "terraform.tfstate"
    state.write_text(json.dumps(document))
    original = state.read_bytes()

    def run(*args: str) -> str:
        result = subprocess.run(
            [binary, *args], cwd=tmp_path, capture_output=True, text=True, timeout=60
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout

    run("init", "-backend=false", "-input=false")
    run(
        "plan",
        "-refresh=false",
        "-input=false",
        "-target=terraform_data.old",
        "-out=transfer.tfplan",
    )
    plan = json.loads(run("show", "-json", "transfer.tfplan"))
    changes = [r for r in plan.get("resource_changes", []) if r.get("mode") == "managed"]
    assert [(r["address"], r["change"]["actions"]) for r in changes] == [
        ("terraform_data.old", ["forget"])
    ]
    from tirodhan.deployment.lifecycle_evidence import plan_prior_state

    prior = plan_prior_state(tmp_path / "transfer.tfplan")
    assert prior["lineage"] == document["lineage"] and prior["serial"] == document["serial"]
    assert state.read_bytes() == original


def test_clean_destroy_does_not_require_missing_bootstrap_data(tmp_path: Path) -> None:
    """Run #8's empty-backend case must not require provisioning a vault to tear down."""
    binary = os.environ.get("TIRODHAN_TEST_TERRAFORM_BINARY")
    if not binary:
        pytest.skip("Pinned Terraform supplied by lifecycle CI or explicit local test invocation")
    (tmp_path / "main.tf").write_text("""
terraform { required_version = "= 1.13.5" }
data "terraform_remote_state" "bootstrap" {
  backend = "local"
  config = { path = "genuinely-absent-bootstrap.tfstate" }
}
output "bootstrap" { value = data.terraform_remote_state.bootstrap.outputs }
""")
    for args in [
        ["init", "-backend=false", "-input=false"],
        ["plan", "-destroy", "-input=false", "-out=empty-destroy.tfplan"],
    ]:
        result = subprocess.run(
            [binary, *args], cwd=tmp_path, capture_output=True, text=True, timeout=60
        )
        assert result.returncode == 0, result.stdout + result.stderr
    assert not (tmp_path / "terraform.tfstate").exists()


def test_initialized_empty_destroy_never_reads_absent_azure_vault(tmp_path: Path) -> None:
    """Use the real pinned AzureRM provider with all authentication methods disabled.

    Reading either data source would fail provider authentication before any ARM call.
    The destroy graph must prune them, independent of provider-specific 404 handling.
    """
    binary = os.environ.get("TIRODHAN_TEST_TERRAFORM_BINARY")
    if not binary:
        pytest.skip("Pinned Terraform and installed AzureRM supplied by lifecycle CI")
    root = Path("infra/terraform/nonprod").resolve()
    shutil.copyfile(root / ".terraform.lock.hcl", tmp_path / ".terraform.lock.hcl")
    (tmp_path / "main.tf").write_text("""
terraform {
  required_version = "= 1.13.5"
  required_providers {
    azurerm = { source = "hashicorp/azurerm", version = "= 5.8.0" }
  }
}
provider "azurerm" {
  features {}
  resource_provider_registrations = "none"
  use_cli = false
  use_oidc = false
  use_msi = false
}
variable "suffix" { default = "test01" }
data "azurerm_resource_group" "nonprod" { name = "genuinely-absent-nonprod" }
output "vault" { value = data.azurerm_key_vault.runtime.id }
""")
    (tmp_path / "key_vault.tf").write_bytes((root / "key_vault.tf").read_bytes())
    state = tmp_path / "terraform.tfstate"
    state.write_text(
        json.dumps(
            {
                "version": 4,
                "terraform_version": "1.13.5",
                "serial": 0,
                "lineage": "00000000-0000-0000-0000-000000000001",
                "outputs": {},
                "resources": [],
            }
        )
    )
    original = state.read_bytes()
    environment = {
        k: v for k, v in os.environ.items() if not k.startswith(("ARM_", "AZURE_", "TF_CLI_ARGS"))
    }
    environment["ARM_SUBSCRIPTION_ID"] = "00000000-0000-0000-0000-000000000001"
    for args in [
        [
            "init",
            "-backend=false",
            "-input=false",
            "-lockfile=readonly",
            f"-plugin-dir={root / '.terraform/providers'}",
        ],
        ["plan", "-destroy", "-input=false", "-out=empty.tfplan"],
    ]:
        result = subprocess.run(
            [binary, *args],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            timeout=90,
        )
        assert result.returncode == 0, result.stdout + result.stderr
    assert state.read_bytes() == original
