from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol
from uuid import UUID

import jwt
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


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
        try:
            private_key = serialization.load_pem_private_key(
                private_key_pem.encode("utf-8"), password=None
            )
            public_key = serialization.load_pem_public_key(public_key_pem.encode("utf-8"))
        except (TypeError, ValueError, UnsupportedAlgorithm) as error:
            raise AccessTokenConfigurationError(
                "JWT signing key configuration is invalid"
            ) from error
        if not isinstance(private_key, rsa.RSAPrivateKey) or not isinstance(
            public_key, rsa.RSAPublicKey
        ):
            raise AccessTokenConfigurationError("JWT signing keys must be RSA keys")
        if private_key.public_key().public_numbers() != public_key.public_numbers():
            raise AccessTokenConfigurationError("JWT signing key pair does not match")
        self._private_key = private_key
        self._public_key = public_key
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
            return jwt.encode(payload, self._private_key, algorithm="RS256")
        except (jwt.PyJWTError, TypeError, ValueError) as error:
            raise AccessTokenConfigurationError("JWT signing configuration is invalid") from error

    def verify(self, token: str) -> AccessTokenClaims:
        try:
            payload = jwt.decode(
                token,
                self._public_key,
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
