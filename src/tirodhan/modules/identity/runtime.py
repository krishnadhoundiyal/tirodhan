"""Explicit OTP selection: no inferred provider and no cross-provider fallback."""

import httpx

from tirodhan.core.config import Settings
from tirodhan.modules.identity.kaleyra import KaleyraVerifyOtpProvider
from tirodhan.modules.identity.ports import OtpProvider, OtpProviderConfigurationError
from tirodhan.modules.identity.twofactor import TwoFactorOtpProvider


def otp_configured(settings: Settings) -> bool:
    if settings.otp_provider is None:
        return False
    values: tuple[object, ...]
    if settings.otp_provider == "2FACTOR":
        values = (settings.twofactor_api_key, settings.twofactor_http_timeout_seconds)
    else:
        values = (
            settings.kaleyra_api_domain,
            settings.kaleyra_sid,
            settings.kaleyra_api_key,
            settings.kaleyra_verify_flow_id,
            settings.kaleyra_http_timeout_seconds,
        )
    if any(value is None for value in values):
        raise OtpProviderConfigurationError("Selected OTP provider configuration is incomplete")
    return True


def otp_provider_from_settings(settings: Settings, client: httpx.AsyncClient) -> OtpProvider:
    if not otp_configured(settings):
        raise OtpProviderConfigurationError("OTP provider is not selected")
    if settings.otp_provider == "2FACTOR":
        assert settings.twofactor_api_key is not None
        assert settings.twofactor_http_timeout_seconds is not None
        return TwoFactorOtpProvider(
            api_key=settings.twofactor_api_key.get_secret_value(),
            template_name=settings.twofactor_template_name,
            timeout_seconds=settings.twofactor_http_timeout_seconds,
            client=client,
        )
    assert settings.kaleyra_api_domain is not None
    assert settings.kaleyra_sid is not None
    assert settings.kaleyra_api_key is not None
    assert settings.kaleyra_verify_flow_id is not None
    assert settings.kaleyra_http_timeout_seconds is not None
    return KaleyraVerifyOtpProvider(
        api_domain=settings.kaleyra_api_domain,
        sid=settings.kaleyra_sid,
        api_key=settings.kaleyra_api_key.get_secret_value(),
        flow_id=settings.kaleyra_verify_flow_id,
        timeout_seconds=settings.kaleyra_http_timeout_seconds,
        client=client,
    )
