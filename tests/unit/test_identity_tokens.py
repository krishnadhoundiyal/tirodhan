from __future__ import annotations

from datetime import timedelta
from functools import lru_cache

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.identity.service import (
    IdentityInputError,
    hash_refresh_credential,
    normalize_phone,
)
from tirodhan.modules.identity.tokens import AccessTokenInvalidError, Rs256AccessTokenCodec

ISSUER = "https://identity.test"
AUDIENCE = "tirodhan-test"


@lru_cache
def key_pair() -> tuple[str, str]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private_pem, public_pem


def codec(*, issuer: str = ISSUER, audience: str = AUDIENCE) -> Rs256AccessTokenCodec:
    private_pem, public_pem = key_pair()
    return Rs256AccessTokenCodec(
        private_key_pem=private_pem,
        public_key_pem=public_pem,
        issuer=issuer,
        audience=audience,
    )


@pytest.mark.parametrize(
    "phone",
    [
        "919876543210",
        "+019876543210",
        "+91 9876543210",
        "+91-9876543210",
        "+1234567",
        "+1234567890123456",
    ],
)
def test_phone_normalization_rejects_noncanonical_e164(phone: str) -> None:
    with pytest.raises(IdentityInputError):
        normalize_phone(phone)


def test_phone_normalization_preserves_explicit_canonical_number() -> None:
    assert normalize_phone("+919876543210") == "+919876543210"


def test_refresh_credential_hash_is_sha256_and_not_plaintext() -> None:
    raw = "opaque-refresh-credential"
    digest = hash_refresh_credential(raw)
    assert len(digest) == 32
    assert digest == hash_refresh_credential(raw)
    assert raw.encode() not in digest


def test_rs256_access_token_has_only_frozen_claims_and_round_trips() -> None:
    user_id = new_uuid7()
    session_id = new_uuid7()
    issued_at = utc_now().replace(microsecond=0)
    token = codec().issue(
        user_id=user_id,
        refresh_session_id=session_id,
        issued_at=issued_at,
        ttl_seconds=300,
    )
    unverified = jwt.decode(token, options={"verify_signature": False})

    assert set(unverified) == {"sub", "sid", "iss", "aud", "iat", "exp", "typ"}
    assert unverified["sub"] == str(user_id)
    assert unverified["sid"] == str(session_id)
    assert unverified["typ"] == "access"
    assert "phone" not in unverified and "roles" not in unverified
    assert codec().verify(token).user_id == user_id
    assert codec().verify(token).refresh_session_id == session_id


def test_expired_access_token_is_rejected() -> None:
    token = codec().issue(
        user_id=new_uuid7(),
        refresh_session_id=new_uuid7(),
        issued_at=utc_now() - timedelta(minutes=10),
        ttl_seconds=60,
    )
    with pytest.raises(AccessTokenInvalidError):
        codec().verify(token)


def test_malformed_and_bad_signature_tokens_are_rejected() -> None:
    with pytest.raises(AccessTokenInvalidError):
        codec().verify("not-a-jwt")

    other_private, _other_public = _new_key_pair()
    token = jwt.encode(_valid_payload(), other_private, algorithm="RS256")
    with pytest.raises(AccessTokenInvalidError):
        codec().verify(token)


@pytest.mark.parametrize(
    ("claim", "value"),
    [
        ("iss", "wrong-issuer"),
        ("aud", "wrong-audience"),
        ("typ", "refresh"),
        ("sub", "not-a-uuid"),
        ("sid", "not-a-uuid"),
        ("iat", "not-a-number"),
    ],
)
def test_invalid_frozen_claims_are_rejected(claim: str, value: object) -> None:
    private_pem, _public_pem = key_pair()
    payload = _valid_payload()
    payload[claim] = value
    token = jwt.encode(payload, private_pem, algorithm="RS256")
    with pytest.raises(AccessTokenInvalidError):
        codec().verify(token)


def test_additional_claim_and_non_rs256_algorithm_are_rejected() -> None:
    private_pem, _public_pem = key_pair()
    payload = _valid_payload()
    payload["roles"] = ["CUSTOMER"]
    token = jwt.encode(payload, private_pem, algorithm="RS256")
    with pytest.raises(AccessTokenInvalidError):
        codec().verify(token)

    token = jwt.encode(
        _valid_payload(), "symmetric-test-secret-at-least-32-bytes", algorithm="HS256"
    )
    with pytest.raises(AccessTokenInvalidError):
        codec().verify(token)


def _valid_payload() -> dict[str, object]:
    issued_at = int(utc_now().timestamp())
    return {
        "sub": str(new_uuid7()),
        "sid": str(new_uuid7()),
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": issued_at,
        "exp": issued_at + 300,
        "typ": "access",
    }


def _new_key_pair() -> tuple[str, str]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return (
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode(),
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode(),
    )
