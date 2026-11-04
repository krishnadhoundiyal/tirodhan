"""Existing workflow inputs can configure checkout without admitting secrets or release fields."""

import json
import os
from pathlib import Path

import pytest

from tirodhan.deployment import inputs

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def input_directory(tmp_path, monkeypatch):
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
