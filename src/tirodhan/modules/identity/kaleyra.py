from __future__ import annotations

import math
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from tirodhan.modules.identity.ports import (
    OtpAlreadyVerifiedError,
    OtpAttemptsExceededError,
    OtpExpiredError,
    OtpInvalidError,
    OtpProviderConfigurationError,
    OtpProviderRateLimitedError,
    OtpProviderUnavailableError,
    OtpVerificationError,
    ProviderOtpChallenge,
)


def validate_api_domain(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise OtpProviderConfigurationError("Kaleyra API domain must be an HTTPS origin")
    try:
        _ = parsed.port
    except ValueError as error:
        raise OtpProviderConfigurationError("Kaleyra API domain is invalid") from error
    return value.rstrip("/")


class KaleyraVerifyOtpProvider:
    provider_code = "KALEYRA_VERIFY"

    def __init__(
        self,
        *,
        api_domain: str,
        sid: str,
        api_key: str,
        flow_id: str,
        timeout_seconds: float,
        client: httpx.AsyncClient,
    ) -> None:
        self._domain = validate_api_domain(api_domain)
        if not all(value.strip() for value in (sid, api_key, flow_id)) or (
            not math.isfinite(timeout_seconds) or timeout_seconds <= 0
        ):
            raise OtpProviderConfigurationError("Kaleyra configuration is invalid")
        self._sid = quote(sid, safe="")
        self._api_key = api_key
        self._flow_id = flow_id
        self._timeout = httpx.Timeout(timeout_seconds)
        self._client = client

    async def start_verification(self, *, normalized_phone: str) -> ProviderOtpChallenge:
        try:
            data = await self._post(
                "verify", {"flow_id": self._flow_id, "to": {"mobile": normalized_phone}}
            )
        except OtpVerificationError:
            # Generate has no submitted OTP. Validation errors here cannot authenticate
            # anyone and must remain a controlled start failure, not an internal error.
            raise OtpProviderUnavailableError("OTP provider could not start verification") from None
        return ProviderOtpChallenge(provider_reference=self._reference(data))

    async def verify(self, *, provider_reference: str, code: str) -> None:
        data = await self._post("verify/validate", {"verify_id": provider_reference, "otp": code})
        if self._reference(data) != provider_reference:
            raise OtpProviderUnavailableError("OTP provider returned an invalid response")

    @staticmethod
    def _reference(data: dict[str, Any]) -> str:
        reference = data.get("verify_id")
        if not isinstance(reference, str) or not reference.strip() or len(reference) > 200:
            raise OtpProviderUnavailableError("OTP provider returned an invalid response")
        return reference

    async def _post(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._client.post(
                f"{self._domain}/v1/{self._sid}/{operation}",
                headers={"api-key": self._api_key},
                json=payload,
                timeout=self._timeout,
                follow_redirects=False,
            )
        except httpx.RequestError:
            # Never chain transport exceptions containing URLs, headers or request bodies.
            raise OtpProviderUnavailableError("OTP provider is unavailable") from None
        if response.status_code == 429:
            raise OtpProviderRateLimitedError("OTP provider rate limit reached")
        if response.status_code >= 500:
            raise OtpProviderUnavailableError("OTP provider is unavailable")
        if response.status_code in (401, 403):
            raise OtpProviderConfigurationError("OTP provider configuration is invalid")
        try:
            body = response.json()
        except ValueError:
            raise OtpProviderUnavailableError("OTP provider returned an invalid response") from None
        if not isinstance(body, dict):
            raise OtpProviderUnavailableError("OTP provider returned an invalid response")
        if "error" in body:
            error = body["error"]
            code = error.get("code") if isinstance(error, dict) else None
            error_types = {
                "E910": OtpAlreadyVerifiedError,
                "E911": OtpAttemptsExceededError,
                "E912": OtpInvalidError,
                "E913": OtpExpiredError,
                "E803": OtpInvalidError,
                "E903": OtpInvalidError,
                "E914": OtpInvalidError,
            }
            if isinstance(code, str) and code in error_types:
                raise error_types[code]("OTP verification failed")
            if code in ("E905", "E909", "E800", "E801", "E804", "E805", "E600"):
                raise OtpProviderConfigurationError("OTP provider configuration is invalid")
            raise OtpProviderUnavailableError("OTP provider returned an unsupported response")
        data = body.get("data")
        if not response.is_success or not isinstance(data, dict):
            raise OtpProviderUnavailableError("OTP provider returned an invalid response")
        return data
