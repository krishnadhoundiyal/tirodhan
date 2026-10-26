"""BDEP-01 deployment wiring invariants; no Azure or external providers."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import hcl2
import pytest

from tirodhan.deployment import seed_secrets

ROOT = Path(__file__).resolve().parents[2]
STACK = ROOT / "infra/terraform/nonprod"


def load(name: str) -> dict:
    with (STACK / name).open(encoding="utf-8") as source:
        return hcl2.load(source)


def test_opt_in_jobs_default_absent_and_stay_stage_four() -> None:
    variables = load("variables.tf")["variable"]
    for name in ("customer_notifications_schedule", "routing_availability_schedule"):
        settings = next(item[name] for item in variables if name in item)
        assert settings["default"] is None
    locals_text = (STACK / "locals.tf").read_text()
    for name in ("customer_notifications", "routing_availability"):
        assert f"var.{name}_schedule == null ? {{}}" in locals_text
        assert f'{name} = "{name}"' in locals_text
        assert f'{name} = var.{name}_schedule' in locals_text
    jobs = load("jobs.tf")["resource"]
    scheduled = next(
        item["azurerm_container_app_job"]["scheduled"]
        for item in jobs
        if "azurerm_container_app_job" in item
        and "scheduled" in item["azurerm_container_app_job"]
    )
    assert scheduled["for_each"] == "${var.deployment_stage >= 4 ? local.jobs : {}}"
    assert scheduled["schedule_trigger_config"][0]["cron_expression"] == "${local.job_cron[each.key]}"
    assert len(scheduled["template"][0]["container"]) == 2


def test_keyvault_refs_and_feature_settings_are_allowlisted() -> None:
    variables = load("variables.tf")["variable"]
    names = next(item["runtime_secret_names"]["default"] for item in variables if "runtime_secret_names" in item)
    refs = {
        "TIRODHAN_CUSTOMER_CURSOR_SIGNING_KEY": "customer-cursor-signing-key",
        "TIRODHAN_CUSTOMER_DATA_ENCRYPTION_KEYS": "customer-data-encryption-keys",
        "TIRODHAN_CUSTOMER_DATA_HMAC_KEY": "customer-data-hmac-key",
        "TIRODHAN_CUSTOMER_VAPID_PRIVATE_KEY_PEM": "customer-vapid-private-key-pem",
    }
    assert all(names[key] == value for key, value in refs.items())
    assert set(refs.values()) == seed_secrets.CUSTOMER_SECRET_NAMES
    available = (ROOT / "deploy/runtime-env.names").read_text().splitlines()
    assert "TIRODHAN_CUSTOMER_DATA_ACTIVE_KEY_ID" in available
    assert "TIRODHAN_CUSTOMER_FCM_ENABLED" in available
    assert "TIRODHAN_CUSTOMER_WEB_PUSH_ENABLED" in available
    assert "TIRODHAN_CUSTOMER_CURSOR_SIGNING_KEY" not in available


def test_approved_credential_batches_are_complete() -> None:
    legacy = seed_secrets.LEGACY_SECRET_NAMES
    customer = seed_secrets.CUSTOMER_SECRET_NAMES
    assert legacy.isdisjoint(customer)
    assert seed_secrets.SECRET_NAMES == legacy | customer
    for batch in (legacy, seed_secrets.CUSTOMER_DATA_SECRET_NAMES, seed_secrets.CUSTOMER_WEB_PUSH_SECRET_NAMES, customer, legacy | customer):
        assert seed_secrets.approved_secret_batch(set(batch))
    assert not seed_secrets.approved_secret_batch({"customer-data-hmac-key"})
    assert not seed_secrets.approved_secret_batch({"arbitrary-secret"})


@pytest.mark.parametrize("kind", ["legacy", "customer", "vapid"])
def test_secret_seeding_does_not_echo_values(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    groups = {
        "legacy": seed_secrets.LEGACY_SECRET_NAMES,
        "customer": seed_secrets.CUSTOMER_DATA_SECRET_NAMES,
        "vapid": seed_secrets.CUSTOMER_WEB_PUSH_SECRET_NAMES,
    }
    credentials = {key: "test-value-not-a-secret" for key in groups[kind]}
    path = tmp_path / "secrets.json"
    path.write_text(json.dumps(credentials), encoding="utf-8")
    sent: list[str] = []
    monkeypatch.setattr(seed_secrets, "vault_put", lambda url, name, value: sent.append(name))
    monkeypatch.setenv("KEY_VAULT_URL", "https://test.vault.azure.net/")
    monkeypatch.setattr(sys, "argv", ["seed", str(path)])
    seed_secrets.main()
    assert set(sent) == set(credentials)
    assert "test-value-not-a-secret" not in capsys.readouterr().out
