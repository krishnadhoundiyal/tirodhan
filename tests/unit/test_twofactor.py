import logging
import traceback

import httpx
import pytest

from tirodhan.core.config import Settings
from tirodhan.core.logging import configure_logging
from tirodhan.main import create_app
from tirodhan.modules.identity.ports import (
    OtpProviderConfigurationError,
    OtpProviderRateLimitedError,
    OtpProviderUnavailableError,
    OtpVerificationError,
    UnconfiguredOtpProvider,
)
from tirodhan.modules.identity.runtime import otp_configured, otp_provider_from_settings
from tirodhan.modules.identity.twofactor import TwoFactorOtpProvider

KEY = "private-twofactor-test-key"
PHONE = "+919876543210"
SESSION = "5D6EBEE6-EC04-4776-846D-3600422BD9EF"


def settings(**changes):
    return Settings(
        _env_file=None,
        **(
            {
                "environment": "test",
                "otp_provider": "2FACTOR",
                "twofactor_api_key": KEY,
                "twofactor_http_timeout_seconds": 2,
            }
            | changes
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("template", [None, "Login template/approved?name#one"])
async def test_exact_legacy_send_verify_and_encoded_template(template):
    requests = []

    def respond(request):
        requests.append(request)
        assert request.method == "GET"
        assert request.url.scheme == "https" and request.url.host == "2factor.in"
        assert not request.url.query
        assert request.extensions["timeout"]["read"] == 2
        if "/VERIFY/" in request.url.path:
            assert request.url.path == f"/API/V1/{KEY}/SMS/VERIFY/{SESSION}/123456"
            return httpx.Response(200, json={"Status": "Success", "Details": "OTP Matched"})
        expected = f"/API/V1/{KEY}/SMS/%2B919876543210/AUTOGEN"
        if template:
            expected += "/Login%20template%2Fapproved%3Fname%23one"
        assert request.url.raw_path.decode() == expected
        return httpx.Response(200, json={"Status": "Success", "Details": SESSION})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = otp_provider_from_settings(settings(twofactor_template_name=template), client)
        challenge = await provider.start_verification(normalized_phone=PHONE)
        assert challenge.provider_reference == SESSION
        await provider.verify(provider_reference=SESSION, code="123456")
    assert len(requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {},
        [],
        {"Status": "Success", "Details": ""},
        {"Status": "Success", "Details": "bad/session"},
        {"Status": "Success", "Details": 1},
        {"Status": "Error", "Details": KEY},
        {"Status": "Success", "Details": "x" * 201},
    ],
)
async def test_malformed_send_reference_fails_without_disclosure(body):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))
    ) as client:
        with pytest.raises(OtpProviderUnavailableError) as error:
            await otp_provider_from_settings(settings(), client).start_verification(
                normalized_phone=PHONE
            )
        assert KEY not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body,error",
    [
        ({"Status": "Error", "Details": "OTP Mismatch"}, OtpVerificationError),
        ({"Status": "Error", "Details": "OTP Expired"}, OtpVerificationError),
        ({"Status": "Success", "Details": "OTP matched"}, OtpProviderUnavailableError),
        ({"Status": "Success", "Details": SESSION}, OtpProviderUnavailableError),
        ({}, OtpProviderUnavailableError),
        ({"Status": "Other", "Details": "OTP Matched"}, OtpProviderUnavailableError),
    ],
)
async def test_only_exact_positive_match_authenticates_and_unknown_taxonomy_is_not_invented(
    body, error
):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))
    ) as client:
        with pytest.raises(error):
            await otp_provider_from_settings(settings(), client).verify(
                provider_reference=SESSION, code="123456"
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,error",
    [
        (401, OtpProviderConfigurationError),
        (403, OtpProviderConfigurationError),
        (429, OtpProviderRateLimitedError),
        (500, OtpProviderUnavailableError),
        (400, OtpProviderUnavailableError),
        (302, OtpProviderUnavailableError),
    ],
)
async def test_http_errors_no_redirects_or_retries(status, error):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            status,
            headers={"Location": "https://attacker.test/"},
            json={"Status": "Error", "Details": KEY + PHONE},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), follow_redirects=True
    ) as client:
        with pytest.raises(error) as failure:
            await otp_provider_from_settings(settings(), client).start_verification(
                normalized_phone=PHONE
            )
        assert KEY not in str(failure.value) and PHONE not in str(failure.value)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_timeout_traceback_and_http_logging_hide_sensitive_url(tmp_path):
    calls = []

    def respond(request):
        calls.append(request)
        raise httpx.ReadTimeout(str(request.url), request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(OtpProviderUnavailableError) as error:
            await otp_provider_from_settings(settings(), client).verify(
                provider_reference=SESSION, code="123456"
            )
    rendered = "".join(traceback.format_exception(error.type, error.value, error.tb))
    assert all(secret not in rendered for secret in (KEY, SESSION, "123456", PHONE))
    assert error.value.__cause__ is None and error.value.__suppress_context__
    assert len(calls) == 1
    path = tmp_path / "safe.log"
    root = logging.getLogger()
    previous_handlers, previous_level = root.handlers[:], root.level
    configure_logging("DEBUG", path)
    try:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={"Status": "Success", "Details": SESSION})
            )
        ) as client:
            await otp_provider_from_settings(settings(), client).start_verification(
                normalized_phone=PHONE
            )
        for handler in root.handlers:
            handler.flush()
        assert all(secret not in path.read_text() for secret in (KEY, PHONE, SESSION))
    finally:
        for handler in root.handlers:
            handler.close()
        root.handlers, root.level = previous_handlers, previous_level


@pytest.mark.parametrize(
    "changes", [{"twofactor_api_key": None}, {"twofactor_http_timeout_seconds": None}]
)
def test_incomplete_selected_provider_fails_at_startup(changes):
    with pytest.raises(OtpProviderConfigurationError):
        create_app(settings(**changes))


@pytest.mark.parametrize("value", [0, -1, 61, float("inf"), float("nan")])
def test_timeout_is_explicit_positive_and_bounded(value):
    with pytest.raises(ValueError):
        settings(twofactor_http_timeout_seconds=value)


@pytest.mark.asyncio
async def test_selection_is_explicit_and_http_client_ownership_is_preserved():
    configured = settings(otp_provider=None)
    assert not otp_configured(configured)
    assert isinstance(create_app(configured).state.otp_provider, UnconfiguredOtpProvider)
    app = create_app(settings())
    async with app.router.lifespan_context(app):
        owned = app.state.otp_http_client
        assert isinstance(app.state.otp_provider, TwoFactorOtpProvider)
    assert owned.is_closed
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200))
    ) as client:
        app = create_app(settings(), otp_http_client=client)
        async with app.router.lifespan_context(app):
            assert app.state.otp_http_client is client
        assert not client.is_closed
    assert KEY not in repr(settings())


@pytest.mark.asyncio
async def test_malformed_json_and_local_inputs_never_authenticate():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"not-json"))
    ) as client:
        provider = otp_provider_from_settings(settings(), client)
        with pytest.raises(OtpProviderUnavailableError):
            await provider.verify(provider_reference=SESSION, code="123456")
        with pytest.raises(OtpVerificationError):
            await provider.verify(provider_reference="bad/session", code="123456")
        with pytest.raises(OtpVerificationError):
            await provider.verify(provider_reference=SESSION, code="not-an-otp")
