from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tirodhan.deployment import seed_gateway_secret as seed
from tirodhan.deployment.deployment_admission import AdmissionDenied


@pytest.fixture
def runner(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("TIRODHAN_NONPROD_SEED_TARGET", "gateway-secret")
    monkeypatch.setenv("KEY_VAULT_URL", "https://example.vault.azure.net")
    metadata_reader = Mock()
    io = SimpleNamespace(vault_id="/vaults/example", runner=metadata_reader)
    monkeypatch.setattr(seed, "require_release_authorization", Mock())
    monkeypatch.setattr(seed, "admitted_with_grant", Mock(return_value=(None, io, None)))
    monkeypatch.setattr(seed, "token_urlsafe", Mock(return_value="sanitized-test-placeholder"))
    monkeypatch.setattr(seed, "vault_put", Mock())
    return metadata_reader


def test_protected_first_seed_then_replay_creates_one_version(runner, capsys):
    active = [{"name": "gateway-secret", "attributes": {"enabled": True}}]
    runner.json.side_effect = [[], active, active]
    seed.main()
    seed.main()
    seed.vault_put.assert_called_once_with(
        "https://example.vault.azure.net", "gateway-secret", "sanitized-test-placeholder"
    )
    assert "sanitized-test-placeholder" not in capsys.readouterr().out


def test_disabled_existing_version_requires_review_without_write(runner):
    runner.json.return_value = [{"name": "gateway-secret", "attributes": {"enabled": False}}]
    with pytest.raises(AdmissionDenied, match="EXISTING_GATEWAY_SECRET_REQUIRES_REVIEW"):
        seed.main()
    seed.vault_put.assert_not_called()


def test_uncertain_write_is_observed_before_retry(runner):
    active = [{"name": "gateway-secret", "attributes": {"enabled": True}}]
    runner.json.side_effect = [[], active]
    seed.vault_put.side_effect = TimeoutError
    with pytest.raises(TimeoutError):
        seed.main()
    seed.main()
    seed.vault_put.assert_called_once()


def test_unverified_metadata_blocks_write(runner):
    runner.json.return_value = {"error": "unavailable"}
    with pytest.raises(AdmissionDenied, match="GATEWAY_SECRET_METADATA_UNAVAILABLE"):
        seed.main()
    seed.vault_put.assert_not_called()
