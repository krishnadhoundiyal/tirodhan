from __future__ import annotations

import httpx
import pytest

from tirodhan.deployment import nonprod_images

REPOSITORY = "krishnadhoundiyal/tirodhan-nonprod"
COMMIT = "a" * 40


def test_ref_pins_both_nonprod_packages_and_reviewed_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable, package in nonprod_images.PACKAGE_REPOSITORIES.items():
        monkeypatch.setenv(variable, f"ghcr.io/{package}:{COMMIT}")
    assert len(nonprod_images.image_refs(COMMIT)) == 2
    assert not nonprod_images.image_refs("b" * 40)
    monkeypatch.setenv("APPLICATION_IMAGE", f"ghcr.io/krishnadhoundiyal/tirodhan:{COMMIT}")
    assert not nonprod_images.image_refs(COMMIT)


@pytest.mark.parametrize(
    "tag_status, denied", [(200, False), (401, True), (403, True), (404, False)]
)
def test_anonymous_registry_token_needs_actual_tag_denial(tag_status: int, denied: bool) -> None:
    def request_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "ghcr.io"
        if request.url.path == "/token":
            assert "authorization" not in request.headers
            assert request.url.params["scope"] == f"repository:{REPOSITORY}:pull"
            return httpx.Response(200, json={"token": "anonymous"})
        assert request.url.path == f"/v2/{REPOSITORY}/tags/list"
        assert request.headers["authorization"] == "Bearer anonymous"
        return httpx.Response(tag_status, json={"errors": [{"code": "DENIED"}]})

    with httpx.Client(transport=httpx.MockTransport(request_handler)) as client:
        assert nonprod_images.anonymous_access_denied(client, REPOSITORY) is denied


@pytest.mark.parametrize(
    "status, body, denied",
    [
        (403, {"errors": [{"code": "DENIED"}]}, True),
        (403, {"message": "proxy forbidden"}, False),
        (429, {"errors": [{"code": "DENIED"}]}, False),
        (200, {}, False),
        (302, {}, False),
    ],
)
def test_requires_explicit_registry_denial(
    status: int, body: dict[str, object], denied: bool
) -> None:
    def request_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/token"
        return httpx.Response(status, json=body)

    with httpx.Client(transport=httpx.MockTransport(request_handler)) as client:
        assert nonprod_images.anonymous_access_denied(client, REPOSITORY) is denied
