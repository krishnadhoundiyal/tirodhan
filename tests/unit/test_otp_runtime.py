from __future__ import annotations

import base64
import json
import logging

import httpx
import pytest
from pydantic import SecretStr

from tirodhan.core.config import Settings
from tirodhan.main import create_app
from tirodhan.modules.identity.kaleyra import KaleyraVerifyOtpProvider
from tirodhan.modules.identity.phone_protection import (
    AesGcmPhoneIdentityProtector,
    PhoneDecryptionError,
    PhoneProtectionConfigurationError,
)
from tirodhan.modules.identity.ports import (
    IdentityProviderNotConfiguredError,
    OtpAlreadyVerifiedError,
    OtpAttemptsExceededError,
    OtpExpiredError,
    OtpInvalidError,
    OtpProviderConfigurationError,
    OtpProviderRateLimitedError,
    OtpProviderUnavailableError,
    UnconfiguredOtpProvider,
)

PHONE = "+919876543210"
CODE = "123456"


def protector(active: str = "old", lookup: bytes = b"h" * 32) -> AesGcmPhoneIdentityProtector:
    return AesGcmPhoneIdentityProtector(
        active_key_id=active,
        encryption_keys={"old": b"a" * 32, "new": b"b" * 32},
        lookup_hmac_key=lookup,
    )


@pytest.mark.asyncio
async def test_phone_encryption_nonce_rotation_and_hmac() -> None:
    old = protector()
    first, second = await old.protect(PHONE), await old.protect(PHONE)
    assert first != second
    assert PHONE.encode() not in first
    assert await old.unprotect(first) == PHONE
    rotated = protector("new")
    assert await rotated.unprotect(first) == PHONE
    assert await old.unprotect(await rotated.protect(PHONE)) == PHONE
    digest = await old.lookup_hmac(PHONE)
    assert len(digest) == 32
    assert digest == await rotated.lookup_hmac(PHONE)
    assert digest != await protector(lookup=b"i" * 32).lookup_hmac(PHONE)


@pytest.mark.asyncio
async def test_phone_envelope_fails_closed() -> None:
    implementation = protector()
    original = await implementation.protect(PHONE)
    unknown = AesGcmPhoneIdentityProtector(
        active_key_id="new",
        encryption_keys={"new": b"b" * 32},
        lookup_hmac_key=b"h" * 32,
    )
    with pytest.raises(PhoneDecryptionError):
        await unknown.unprotect(original)
    for damaged in (
        b"",
        original[:10],
        b"TPH\x02" + original[4:],
        original[:-1] + bytes([original[-1] ^ 1]),
        original + b"extra",
    ):
        with pytest.raises(PhoneDecryptionError):
            await implementation.unprotect(damaged)


@pytest.mark.parametrize(
    "active,keys,lookup",
    [
        ("absent", {"a": b"a" * 32}, b"h" * 32),
        ("a", {"a": b"a" * 31}, b"h" * 32),
        ("a", {"a": b"a" * 32}, b"h" * 31),
        ("a", {"a": b"a" * 32}, b"a" * 32),
        ("", {"": b"a" * 32}, b"h" * 32),
    ],
)
def test_phone_keys_must_be_independent_256_bit_material(active, keys, lookup) -> None:
    with pytest.raises(PhoneProtectionConfigurationError):
        AesGcmPhoneIdentityProtector(
            active_key_id=active,
            encryption_keys=keys,
            lookup_hmac_key=lookup,
        )


@pytest.mark.parametrize(
    "keyring,hmac_key",
    [
        ("not-json", "bad"),
        ("[]", "bad"),
        ('{"a": 1}', "bad"),
        ('{"a": "not-base64!"}', "bad"),
        (json.dumps({"a": base64.b64encode(b"a" * 32).decode()}), "bad"),
    ],
)
def test_malformed_phone_runtime_configuration_fails_fast(keyring, hmac_key) -> None:
    with pytest.raises(PhoneProtectionConfigurationError):
        create_app(
            Settings(
                _env_file=None,
                environment="test",
                phone_encryption_active_key_id="a",
                phone_encryption_keys=SecretStr(keyring),
                phone_lookup_hmac_key=SecretStr(hmac_key),
            )
        )


@pytest.mark.asyncio
async def test_missing_phone_configuration_has_no_plaintext_fallback() -> None:
    app = create_app(Settings(_env_file=None, environment="test"))
    with pytest.raises(IdentityProviderNotConfiguredError):
        await app.state.phone_identity_protector.protect(PHONE)


def provider(client: httpx.AsyncClient) -> KaleyraVerifyOtpProvider:
    return KaleyraVerifyOtpProvider(
        api_domain="https://kaleyra.test",
        sid="test-sid",
        api_key="test-secret",
        flow_id="test-flow",
        timeout_seconds=2,
        client=client,
    )


@pytest.mark.asyncio
async def test_kaleyra_transaction_bound_mapping(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["api-key"] == "test-secret"
        assert request.extensions["timeout"] == dict(connect=2, read=2, write=2, pool=2)
        return httpx.Response(200, json={"data": {"verify_id": "remote-reference"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = provider(client)
        challenge = await adapter.start_verification(normalized_phone=PHONE)
        assert challenge.provider_reference == "remote-reference"
        await adapter.verify(provider_reference=challenge.provider_reference, code=CODE)
    assert [str(request.url) for request in requests] == [
        "https://kaleyra.test/v1/test-sid/verify",
        "https://kaleyra.test/v1/test-sid/verify/validate",
    ]
    assert json.loads(requests[0].content) == {"flow_id": "test-flow", "to": {"mobile": PHONE}}
    assert json.loads(requests[1].content) == {"verify_id": "remote-reference", "otp": CODE}
    for sensitive in (PHONE, CODE, "test-secret", "remote-reference"):
        assert sensitive not in caplog.text


@pytest.mark.parametrize(
    "status,body,error",
    [
        (400, {"error": {"code": "E910"}}, OtpAlreadyVerifiedError),
        (400, {"error": {"code": "E911"}}, OtpAttemptsExceededError),
        (400, {"error": {"code": "E912"}}, OtpInvalidError),
        (400, {"error": {"code": "E913"}}, OtpExpiredError),
        (429, {}, OtpProviderRateLimitedError),
        (503, {}, OtpProviderUnavailableError),
        (401, {}, OtpProviderConfigurationError),
        (200, {"error": {"code": []}}, OtpProviderUnavailableError),
        (200, {"data": {}}, OtpProviderUnavailableError),
        (302, {"data": {"verify_id": "remote-reference"}}, OtpProviderUnavailableError),
        (200, {"data": {"verify_id": "wrong-reference"}}, OtpProviderUnavailableError),
    ],
)
@pytest.mark.asyncio
async def test_kaleyra_failures_never_retry(status, body, error) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, json=body, headers={"location": "https://other.test"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(error) as captured:
            await provider(client).verify(provider_reference="remote-reference", code=CODE)
    assert calls == 1
    assert CODE not in str(captured.value)
    assert "test-secret" not in str(captured.value)


@pytest.mark.asyncio
async def test_generate_validation_error_is_controlled_and_not_retried() -> None:
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(400, json={"error": {"code": "E903", "message": "private detail"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(OtpProviderUnavailableError):
            await provider(client).start_verification(normalized_phone=PHONE)
    assert calls == 1


@pytest.mark.parametrize("transport_error", [httpx.ReadTimeout, httpx.ConnectError])
@pytest.mark.parametrize("operation", ["start", "verify"])
@pytest.mark.asyncio
async def test_kaleyra_network_failure_has_no_retry_or_sensitive_exception(
    operation, transport_error
):
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise transport_error("sensitive transport detail", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = provider(client)
        with pytest.raises(OtpProviderUnavailableError) as captured:
            if operation == "start":
                await adapter.start_verification(normalized_phone=PHONE)
            else:
                await adapter.verify(provider_reference="remote-reference", code=CODE)
    assert calls == 1
    assert captured.value.__suppress_context__


@pytest.mark.parametrize(
    "origin",
    [
        "http://kaleyra.test",
        "https://key@kaleyra.test",
        "https://kaleyra.test/path",
        "https://kaleyra.test?key=x",
    ],
)
def test_kaleyra_requires_https_origin(origin) -> None:
    with pytest.raises(OtpProviderConfigurationError):
        Settings(_env_file=None, kaleyra_api_domain=origin)


def runtime_settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        kaleyra_api_domain="https://kaleyra.test",
        kaleyra_sid="sid",
        kaleyra_api_key=SecretStr("test-secret"),
        kaleyra_verify_flow_id="flow",
        kaleyra_http_timeout_seconds=2,
    )


@pytest.mark.asyncio
async def test_runtime_ownership_and_incomplete_configuration() -> None:
    app = create_app(runtime_settings())
    async with app.router.lifespan_context(app):
        owned = app.state.otp_http_client
        assert not owned.is_closed
        assert isinstance(app.state.otp_provider, KaleyraVerifyOtpProvider)
    assert owned.is_closed
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200))
    ) as client:
        injected = create_app(runtime_settings(), otp_http_client=client)
        async with injected.router.lifespan_context(injected):
            assert injected.state.otp_http_client is client
        assert not client.is_closed
    incomplete = create_app(Settings(_env_file=None, kaleyra_sid="sid"))
    async with incomplete.router.lifespan_context(incomplete):
        assert isinstance(incomplete.state.otp_provider, UnconfiguredOtpProvider)
        assert not hasattr(incomplete.state, "otp_http_client")
