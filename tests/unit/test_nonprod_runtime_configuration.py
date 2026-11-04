"""Existing workflow inputs can configure checkout without admitting secrets or release fields."""

import json
import os
from pathlib import Path

import pytest

from tirodhan.deployment import inputs

ROOT = Path(__file__).resolve().parents[2]

INVENTORY_RUNTIME = {
    "TIRODHAN_FINANCIAL_RECONCILIATION_BATCH_SIZE": "10",
    "TIRODHAN_FINANCIAL_RECONCILIATION_INTERVAL_SECONDS": "60",
    "TIRODHAN_FINANCIAL_RECONCILIATION_MAX_BACKOFF_SECONDS": "300",
    "TIRODHAN_FINANCIAL_RECONCILIATION_LEASE_SECONDS": "30",
    "TIRODHAN_FINANCIAL_UNRESOLVED_THRESHOLD_SECONDS": "3600",
    "TIRODHAN_COMMAND_IDEMPOTENCY_TTL_SECONDS": "300",
    "TIRODHAN_PLANNING_LEAD_TIME_MINUTES": "5",
    "TIRODHAN_FINANCIAL_INVENTORY_START_EPOCH": "1700000000",
    "TIRODHAN_FINANCIAL_INVENTORY_WINDOW_SECONDS": "3600",
    "TIRODHAN_FINANCIAL_INVENTORY_OVERLAP_SECONDS": "60",
    "TIRODHAN_FINANCIAL_INVENTORY_VISIBILITY_LAG_SECONDS": "30",
    "TIRODHAN_FINANCIAL_INVENTORY_PAGE_BUDGET": "5",
    "TIRODHAN_RAZORPAY_ACCOUNT_ID": "synthetic-account",
}


@pytest.fixture
def input_directory(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_WORKFLOW_REF", raising=False)
    stack = tmp_path / "infra/terraform/nonprod"
    stack.mkdir(parents=True)
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    (deploy / "runtime-env.names").write_text((ROOT / "deploy/runtime-env.names").read_text())
    for name in list(os.environ):
        if name.startswith("TIRODHAN_"):
            monkeypatch.delenv(name)
    # Track the variables that inputs.main sets so the test does not leak them.
    monkeypatch.setenv("TIRODHAN_RAZORPAY_MERCHANT_DISPLAY_NAME", "Test merchant")
    monkeypatch.setenv("TIRODHAN_PAYMENT_CHECKOUT_READINESS_SECONDS", "30")
    monkeypatch.chdir(stack)
    return stack


def test_checkout_settings_and_disabled_inventory_schedule_use_existing_input_path(
    input_directory, monkeypatch
):
    proposed = {
        "runtime_env": {
            "TIRODHAN_RAZORPAY_MERCHANT_DISPLAY_NAME": "Test merchant",
            "TIRODHAN_PAYMENT_CHECKOUT_READINESS_SECONDS": "30",
        },
        "financial_inventory_schedule": None,
    }
    monkeypatch.setenv("NONPROD_TFVARS_JSON", json.dumps(proposed))
    inputs.main()
    assert json.loads((input_directory / "nonprod.auto.tfvars.json").read_text()) == proposed


def test_checkout_lifetime_still_obeys_the_application_bound(input_directory, monkeypatch):
    monkeypatch.setenv(
        "NONPROD_TFVARS_JSON",
        json.dumps({"runtime_env": {"TIRODHAN_PAYMENT_CHECKOUT_READINESS_SECONDS": "61"}}),
    )
    with pytest.raises(ValueError, match="1..60"):
        inputs.main()
    assert not (input_directory / "nonprod.auto.tfvars.json").exists()


@pytest.mark.parametrize(
    "name", ["deployment_stage", "application_image", "migration_operation_key"]
)
def test_release_owned_fields_still_cannot_be_supplied(input_directory, monkeypatch, name):
    monkeypatch.setenv("NONPROD_TFVARS_JSON", json.dumps({name: "untrusted"}))
    with pytest.raises(RuntimeError, match="Only approved nonsecret"):
        inputs.main()
    assert not (input_directory / "nonprod.auto.tfvars.json").exists()


def test_secret_cannot_enter_runtime_configuration(input_directory, monkeypatch):
    monkeypatch.setenv(
        "NONPROD_TFVARS_JSON",
        json.dumps({"runtime_env": {"TIRODHAN_RAZORPAY_KEY_SECRET": "test-only"}}),
    )
    with pytest.raises(RuntimeError, match="approved nonsecret setting"):
        inputs.main()
    assert not (input_directory / "nonprod.auto.tfvars.json").exists()


@pytest.mark.parametrize("missing", sorted(INVENTORY_RUNTIME))
def test_inventory_schedule_cannot_admit_an_incomplete_job(input_directory, monkeypatch, missing):
    runtime = {k: v for k, v in INVENTORY_RUNTIME.items() if k != missing}
    for name, value in runtime.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv(
        "NONPROD_TFVARS_JSON",
        json.dumps({"runtime_env": runtime, "financial_inventory_schedule": "0 * * * *"}),
    )
    with pytest.raises(ValueError):
        inputs.main()
    assert not (input_directory / "nonprod.auto.tfvars.json").exists()


def test_inventory_complete_policy_is_shared_with_worker_and_live_admission(
    input_directory, monkeypatch
):
    from lifecycle_fakes import MemoryObservations, auth

    from tirodhan.deployment.lifecycle_checks import Checks
    from tirodhan.deployment.runtime_prerequisites import proposed_settings
    from tirodhan.workers.financial_inventory import configured_policies

    config = {"runtime_env": INVENTORY_RUNTIME, "financial_inventory_schedule": "0 * * * *"}
    for name, value in INVENTORY_RUNTIME.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("NONPROD_TFVARS_JSON", json.dumps(config))
    inputs.main()
    assert (input_directory / "nonprod.auto.tfvars.json").exists()
    configured_policies(proposed_settings(INVENTORY_RUNTIME))
    observations = MemoryObservations()
    observations.config["terraform"].update(config)
    checks = Checks(auth("runtime-plan"), observations.config, {}, observations)
    assert checks.check_features()
    observations.config["terraform"]["runtime_env"] = {}
    with pytest.raises(ValueError):
        checks.check_features()


@pytest.mark.parametrize("workflow", ["nonprod-destroy.yml", "nonprod-reconcile.yml"])
def test_broken_optional_policy_cannot_strand_protected_maintenance(
    input_directory, monkeypatch, workflow
):
    from lifecycle_fakes import MemoryObservations, auth

    from tirodhan.deployment.lifecycle_checks import Checks

    proposed = {"runtime_env": {}, "financial_inventory_schedule": "0 * * * *"}
    monkeypatch.setenv(
        "GITHUB_WORKFLOW_REF",
        f"krishnadhoundiyal/tirodhan/.github/workflows/{workflow}@refs/heads/main",
    )
    monkeypatch.setenv("NONPROD_TFVARS_JSON", json.dumps(proposed))
    inputs.main()
    assert json.loads((input_directory / "nonprod.auto.tfvars.json").read_text()) == proposed
    observations = MemoryObservations()
    observations.config["terraform"].update(proposed)
    with pytest.raises(ValueError):
        Checks(auth("runtime-apply"), observations.config, {}, observations).check_features()


@pytest.mark.parametrize(
    "workflow",
    [
        "nonprod-foundation.yml",
        "nonprod-deploy.yml",
        "unknown.yml",
        "nonprod-destroy.yml@refs/heads/untrusted",
    ],
)
def test_other_contexts_never_skip_workload_policy(input_directory, monkeypatch, workflow):
    monkeypatch.setenv(
        "GITHUB_WORKFLOW_REF",
        f"krishnadhoundiyal/tirodhan/.github/workflows/{workflow}@refs/heads/main",
    )
    monkeypatch.setenv(
        "NONPROD_TFVARS_JSON",
        json.dumps({"runtime_env": {}, "financial_inventory_schedule": "0 * * * *"}),
    )
    with pytest.raises(ValueError):
        inputs.main()
    assert not (input_directory / "nonprod.auto.tfvars.json").exists()
