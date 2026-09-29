from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol
from uuid import UUID

import jwt


class AccessTokenConfigurationError(RuntimeError):
    pass


class AccessTokenInvalidError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AccessTokenClaims:
    user_id: UUID
    refresh_session_id: UUID


class AccessTokenCodec(Protocol):
    @property
    def configured(self) -> bool: ...

    def issue(
        self,
        *,
        user_id: UUID,
        refresh_session_id: UUID,
        issued_at: datetime,
        ttl_seconds: int,
    ) -> str: ...

    def verify(self, token: str) -> AccessTokenClaims: ...


class UnconfiguredAccessTokenCodec:
    @property
    def configured(self) -> bool:
        return False

    def issue(
        self,
        *,
        user_id: UUID,
        refresh_session_id: UUID,
        issued_at: datetime,
        ttl_seconds: int,
    ) -> str:
        raise AccessTokenConfigurationError("JWT signing is not configured")

    def verify(self, token: str) -> AccessTokenClaims:
        raise AccessTokenConfigurationError("JWT verification is not configured")


class Rs256AccessTokenCodec:
    _claim_names = {"sub", "sid", "iss", "aud", "iat", "exp", "typ"}

    def __init__(
        self,
        *,
        private_key_pem: str,
        public_key_pem: str,
        issuer: str,
        audience: str,
    ) -> None:
        if not all((private_key_pem, public_key_pem, issuer, audience)):
            raise AccessTokenConfigurationError("complete JWT configuration is required")
        self._private_key_pem = private_key_pem
        self._public_key_pem = public_key_pem
        self._issuer = issuer
        self._audience = audience

    @property
    def configured(self) -> bool:
        return True

    def issue(
        self,
        *,
        user_id: UUID,
        refresh_session_id: UUID,
        issued_at: datetime,
        ttl_seconds: int,
    ) -> str:
        if ttl_seconds <= 0:
            raise AccessTokenConfigurationError("access-token lifetime must be positive")
        if issued_at.tzinfo is None:
            raise AccessTokenConfigurationError("access-token issue time must be timezone-aware")
        issued_at = issued_at.astimezone(timezone.utc)
        payload = {
            "sub": str(user_id),
            "sid": str(refresh_session_id),
            "iss": self._issuer,
            "aud": self._audience,
            "iat": int(issued_at.timestamp()),
            "exp": int((issued_at + timedelta(seconds=ttl_seconds)).timestamp()),
            "typ": "access",
        }
        try:
            return jwt.encode(payload, self._private_key_pem, algorithm="RS256")
        except (jwt.PyJWTError, TypeError, ValueError) as error:
            raise AccessTokenConfigurationError("JWT signing configuration is invalid") from error

    def verify(self, token: str) -> AccessTokenClaims:
        try:
            payload = jwt.decode(
                token,
                self._public_key_pem,
                algorithms=["RS256"],
                issuer=self._issuer,
                audience=self._audience,
                options={
                    "require": ["sub", "sid", "iss", "aud", "iat", "exp", "typ"],
                    "verify_iat": True,
                    "verify_exp": True,
                },
            )
            if set(payload) != self._claim_names:
                raise AccessTokenInvalidError("access token has unsupported claims")
            if payload["typ"] != "access":
                raise AccessTokenInvalidError("access token type is invalid")
            if not _is_numeric_date(payload["iat"]) or not _is_numeric_date(payload["exp"]):
                raise AccessTokenInvalidError("access token timestamps are invalid")
            return AccessTokenClaims(
                user_id=UUID(payload["sub"]),
                refresh_session_id=UUID(payload["sid"]),
            )
        except AccessTokenInvalidError:
            raise
        except (jwt.PyJWTError, KeyError, TypeError, ValueError) as error:
            raise AccessTokenInvalidError("access token is invalid") from error


def _is_numeric_date(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)
