"""Complete application enrollment does not require re-seeding migration credentials."""

import json
import sys
from unittest.mock import Mock

import pytest
from lifecycle_fakes import MemoryObservations, auth

from tirodhan.deployment import deployment_admission, lifecycle_checks, prerequisites, seed_secrets
from tirodhan.deployment.deployment_admission import AdmissionDenied
from tirodhan.deployment.lifecycle_checks import Checks
from tirodhan.deployment.release import MIGRATION_SECRET_NAMES

RUNTIME_NAMES = {
    "twofactor-api-key",
    "google-maps-api-key",
    "razorpay-key-secret",
    "razorpay-webhook-secret",
    "auth-jwt-private-key-pem",
    "auth-jwt-public-key-pem",
    "phone-encryption-keys",
    "phone-lookup-hmac-key",
    "address-encryption-keys",
    "customer-cursor-signing-key",
    "customer-data-encryption-keys",
    "customer-data-hmac-key",
}


def test_complete_batch_matches_actual_runtime_admission_requirements():
    observations = MemoryObservations()
    checks = Checks(auth("runtime-plan"), observations.config, {}, observations)
    assert checks.required_secret_names() - MIGRATION_SECRET_NAMES == RUNTIME_NAMES
    assert seed_secrets.approved_secret_batch(RUNTIME_NAMES)
    assert MIGRATION_SECRET_NAMES == {"ghcr-pull-pat", "fluent-bit-sas"}
    assert RUNTIME_NAMES.isdisjoint(MIGRATION_SECRET_NAMES)


@pytest.mark.parametrize("missing", sorted(RUNTIME_NAMES))
def test_every_runtime_credential_remains_mandatory(missing):
    assert not seed_secrets.approved_secret_batch(RUNTIME_NAMES - {missing})


@pytest.mark.parametrize(
    "extra", ["arbitrary-secret", "kaleyra-api-key", "fcm-credentials-json", "ghcr-pull-pat"]
)
def test_new_batch_does_not_allow_arbitrary_additions(extra):
    assert not seed_secrets.approved_secret_batch(RUNTIME_NAMES | {extra})


def test_incomplete_batch_has_no_admission_or_vault_effect(tmp_path, monkeypatch):
    path = tmp_path / "incomplete.json"
    path.write_text(
        json.dumps({name: "test-only" for name in RUNTIME_NAMES - {"twofactor-api-key"}})
    )
    admission, write = Mock(), Mock()
    monkeypatch.setattr(prerequisites, "require_provisioning", admission)
    monkeypatch.setattr(seed_secrets, "vault_put", write)
    monkeypatch.setattr(sys, "argv", ["seed", str(path)])
    with pytest.raises(SystemExit, match="complete approved"):
        seed_secrets.main()
    admission.assert_not_called()
    write.assert_not_called()


def test_complete_runtime_batch_still_requires_authority_before_any_cloud_access(
    tmp_path, monkeypatch
):
    (tmp_path / "deploy").mkdir()
    (tmp_path / "deploy/nonprod-release-policy.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "contract_version": "nonprod-lifecycle-v1",
                "release_enabled": False,
                "authorization_keys": {"test-only": "unused"},
            }
        )
    )
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps({name: "test-only" for name in RUNTIME_NAMES}))
    monkeypatch.setattr(deployment_admission, "ROOT", tmp_path)
    monkeypatch.setenv("RELEASE_AUTHORIZATION_VALID", "true")
    monkeypatch.setenv("PREREQUISITES_SATISFIED", "true")
    observations, write = Mock(), Mock()
    monkeypatch.setattr(lifecycle_checks, "Observations", observations)
    monkeypatch.setattr(seed_secrets, "vault_put", write)
    monkeypatch.setattr(sys, "argv", ["seed", str(path)])
    with pytest.raises(AdmissionDenied, match="DEPLOYMENT_HOLD"):
        seed_secrets.main()
    observations.assert_not_called()
    write.assert_not_called()
