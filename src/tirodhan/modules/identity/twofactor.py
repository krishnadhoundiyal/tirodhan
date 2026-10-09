"""2Factor legacy AUTOGEN/VERIFY pair; provider references stay backend-private."""

import math
import re
from typing import Any
from urllib.parse import quote

import httpx

from tirodhan.modules.identity.ports import (
    OtpProviderConfigurationError,
    OtpProviderRateLimitedError,
    OtpProviderUnavailableError,
    OtpVerificationError,
    ProviderOtpChallenge,
)

API_ORIGIN = "https://2factor.in"


class TwoFactorOtpProvider:
    provider_code = "2FACTOR"

    def __init__(
        self,
        *,
        api_key: str,
        template_name: str | None,
        timeout_seconds: float,
        client: httpx.AsyncClient,
    ) -> None:
        if (
            not api_key.strip()
            or len(api_key) > 200
            or api_key in (".", "..")
            or any(c.isspace() for c in api_key)
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 60
        ):
            raise OtpProviderConfigurationError("2Factor configuration is invalid")
        if template_name is not None and (
            not template_name.strip()
            or len(template_name) > 200
            or template_name in (".", "..")
            or any(ord(c) < 32 for c in template_name)
        ):
            raise OtpProviderConfigurationError("2Factor template configuration is invalid")
        self._key = quote(api_key, safe="")
        self._template = quote(template_name, safe="") if template_name is not None else None
        self._timeout = httpx.Timeout(timeout_seconds)
        self._client = client

    async def start_verification(self, *, normalized_phone: str) -> ProviderOtpChallenge:
        if re.fullmatch(r"\+[1-9][0-9]{7,14}", normalized_phone) is None:
            raise OtpProviderConfigurationError("OTP phone input is invalid")
        path = f"SMS/{quote(normalized_phone, safe='')}/AUTOGEN"
        if self._template is not None:
            path += "/" + self._template
        data = await self._get(path)
        reference = data.get("Details")
        if (
            data.get("Status") != "Success"
            or not isinstance(reference, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{1,200}", reference) is None
        ):
            raise OtpProviderUnavailableError("OTP provider returned an invalid challenge")
        return ProviderOtpChallenge(provider_reference=reference)

    async def verify(self, *, provider_reference: str, code: str) -> None:
        if (
            re.fullmatch(r"[A-Za-z0-9_-]{1,200}", provider_reference) is None
            or re.fullmatch(r"[0-9]{1,32}", code) is None
        ):
            raise OtpVerificationError("OTP verification failed")
        data = await self._get(
            f"SMS/VERIFY/{quote(provider_reference, safe='')}/{quote(code, safe='')}",
            verification=True,
        )
        if data.get("Status") == "Error" and isinstance(data.get("Details"), str):
            raise OtpVerificationError("OTP verification failed")
        if data.get("Status") != "Success" or data.get("Details") != "OTP Matched":
            # The public legacy references do not define a trustworthy error taxonomy.
            # Unknown provider errors, including purported mismatch/expiry, fail closed
            # without guessing authentication, expiry, or account error codes.
            raise OtpProviderUnavailableError(
                "OTP provider returned an unsupported verification result"
            )

    async def _get(self, path: str, *, verification: bool = False) -> dict[str, Any]:
        try:
            response = await self._client.get(
                f"{API_ORIGIN}/API/V1/{self._key}/{path}",
                timeout=self._timeout,
                follow_redirects=False,
            )
        except httpx.RequestError:
            raise OtpProviderUnavailableError("OTP provider is unavailable") from None
        if response.status_code in (401, 403):
            raise OtpProviderConfigurationError("OTP provider configuration is invalid")
        if response.status_code == 429:
            raise OtpProviderRateLimitedError("OTP provider rate limit reached")
        if not response.is_success and not (verification and response.status_code == 400):
            raise OtpProviderUnavailableError("OTP provider is unavailable")
        try:
            data = response.json()
        except (ValueError, UnicodeError):
            raise OtpProviderUnavailableError("OTP provider returned an invalid response") from None
        if not isinstance(data, dict):
            raise OtpProviderUnavailableError("OTP provider returned an invalid response")
        if not response.is_success:
            if data.get("Status") == "Error" and isinstance(data.get("Details"), str):
                raise OtpVerificationError("OTP verification failed")
            raise OtpProviderUnavailableError("OTP provider returned an unsupported response")
        return data
