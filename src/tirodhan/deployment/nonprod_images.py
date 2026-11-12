"""Verify the NONPROD GHCR package access boundary."""

from __future__ import annotations

import os
import re

import httpx

PACKAGE_REPOSITORIES = {
    "APPLICATION_IMAGE": "krishnadhoundiyal/tirodhan-nonprod",
    "FLUENT_BIT_IMAGE": "krishnadhoundiyal/tirodhan-nonprod-log-sidecar",
}
REGISTRY = "https://ghcr.io"


def image_refs(commit: str) -> tuple[str, ...]:
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        return ()
    refs = tuple(os.environ.get(variable, "") for variable in PACKAGE_REPOSITORIES)
    expected = tuple(f"ghcr.io/{repo}:{commit}" for repo in PACKAGE_REPOSITORIES.values())
    return refs if refs == expected else ()


def _registry_denied(response: httpx.Response) -> bool:
    if response.status_code not in {401, 403}:
        return False
    document = response.json()
    errors = document.get("errors") if isinstance(document, dict) else None
    return isinstance(errors, list) and any(
        isinstance(error, dict) and error.get("code") in {"DENIED", "UNAUTHORIZED"}
        for error in errors
    )


def anonymous_access_denied(client: httpx.Client, repository: str) -> bool:
    try:
        response = client.get(
            f"{REGISTRY}/token",
            params={"service": "ghcr.io", "scope": f"repository:{repository}:pull"},
        )
        if response.status_code != 200:
            return _registry_denied(response)
        document = response.json()
        token = document.get("token") if isinstance(document, dict) else None
        if not isinstance(token, str) or not token.strip():
            return False
        tags = client.get(
            f"{REGISTRY}/v2/{repository}/tags/list",
            params={"n": 1},
            headers={"Authorization": f"Bearer {token}"},
        )
        return _registry_denied(tags)
    except (httpx.HTTPError, ValueError):
        return False


def private_release_images(commit: str) -> tuple[str, ...]:
    refs = image_refs(commit)
    if not refs:
        return ()
    with httpx.Client(timeout=10, follow_redirects=False, trust_env=False) as client:
        if all(anonymous_access_denied(client, repo) for repo in PACKAGE_REPOSITORIES.values()):
            return refs
    return ()


if __name__ == "__main__":
    if not private_release_images(os.environ.get("GITHUB_SHA", "")):
        raise SystemExit("NONPROD_GHCR_PRIVACY_UNVERIFIED")
    print("Anonymous pull access denied for both approved NONPROD packages")
