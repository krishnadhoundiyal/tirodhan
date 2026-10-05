from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = ROOT / "deploy/bootstrap/bootstrap.sh"


def test_bootstrap_uses_supported_storage_cli_and_preserves_security_controls() -> None:
    script = BOOTSTRAP.read_text(encoding="utf-8")

    assert "--default-to-oauth-authentication" not in script
    assert "--allow-shared-key-access false" in script
    assert "--allow-blob-public-access false" in script
    assert "--https-only true" in script
    assert "--min-tls-version TLS1_2" in script

    assert "az rest" in script
    assert "defaultToOAuthAuthentication" in script
    assert "api-version=2025-06-01" in script


def test_bootstrap_uses_immutable_github_oidc_subjects() -> None:
    script = BOOTSTRAP.read_text(encoding="utf-8")

    assert 'github_owner_id="46423210"' in script
    assert 'github_repository_id="1385964140"' in script
    expected_repo_subject = (
        'github_subject_repo="krishnadhoundiyal@${github_owner_id}/'
        'tirodhan@${github_repository_id}"'
    )
    assert expected_repo_subject in script
    assert 'subject="repo:$github_subject_repo:environment:$environment"' in script
    assert 'subject "repo:$GITHUB_REPOSITORY:environment:$environment"' not in script
    assert "federated-credential delete" in script
    assert "federated-credential create" in script


def test_bootstrap_allows_published_service_bus_receiver_role() -> None:
    script = BOOTSTRAP.read_text(encoding="utf-8")

    assert "4f6d3b9b-027b-4f4c-9142-0e5a2a2247e0" in script
    assert "4f6db5ce-55e8-4d65-b4d2-4d2aade53608" not in script
    assert "az role assignment update --role-assignment" in script
    assert "az role assignment list --all --assignee-object-id" not in script
    assert "az role assignment list --assignee-object-id" in script
