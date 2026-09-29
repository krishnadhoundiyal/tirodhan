from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from hmac import compare_digest
from uuid import UUID

from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.customers.service import command_fingerprint
from tirodhan.modules.identity.locking import acquire_phone_identity_advisory_lock
from tirodhan.modules.identity.models import AppUser, RefreshSession, UserPhone, UserRole
from tirodhan.modules.identity.ports import (
    IdentityProviderNotConfiguredError,
    OtpChallenge,
    OtpProvider,
    PhoneIdentityProtector,
)
from tirodhan.modules.identity.tokens import AccessTokenCodec, AccessTokenInvalidError
from tirodhan.modules.reliability.models import IdempotencyRecord
from tirodhan.modules.reliability.primitives import (
    IdempotencyKeyConflictError,
    claim_idempotency_record,
    complete_idempotency_record,
    get_completed_idempotency_result,
)

APP_USER_ACTIVE = "ACTIVE"
ROLE_CUSTOMER = "CUSTOMER"
ROLE_RIDER = "RIDER"
ROLE_MANAGER = "MANAGER"
AUTH_VERIFY_SCOPE = "auth.verify"
_E164_PATTERN = re.compile(r"^\+[1-9][0-9]{7,14}$")


class IdentityInputError(ValueError):
    pass


class LoginCredentialsUnavailableReplayError(RuntimeError):
    pass


class LoginCommandInProgressError(RuntimeError):
    pass


class UserAuthenticationDeniedError(RuntimeError):
    pass


class RefreshAuthenticationError(RuntimeError):
    pass


class AccessAuthenticationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class LoginCredentials:
    user_id: UUID
    refresh_session_id: UUID
    access_token: SecretStr = field(repr=False)
    refresh_token: SecretStr = field(repr=False)
    expires_in: int


@dataclass(frozen=True, slots=True)
class AccessCredentials:
    user_id: UUID
    refresh_session_id: UUID
    access_token: SecretStr = field(repr=False)
    expires_in: int


@dataclass(frozen=True, slots=True)
class AuthenticatedPrincipal:
    user_id: UUID
    refresh_session_id: UUID
    roles: frozenset[str]


def normalize_phone(phone: str) -> str:
    if not _E164_PATTERN.fullmatch(phone):
        raise IdentityInputError("phone must be an explicit E.164 number with 8 to 15 digits")
    return phone


def hash_refresh_credential(raw_credential: str) -> bytes:
    return hashlib.sha256(raw_credential.encode("utf-8")).digest()


async def start_otp_verification(
    provider: OtpProvider,
    *,
    phone: str,
    client_request_id: UUID,
) -> OtpChallenge:
    normalized_phone = normalize_phone(phone)
    challenge = await provider.start_verification(
        normalized_phone=normalized_phone,
        client_request_id=client_request_id,
    )
    if not challenge.challenge_reference:
        raise RuntimeError("OTP provider returned an empty challenge reference")
    return challenge


async def verify_otp_and_login(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    client_login_id: UUID,
    phone: str,
    challenge_reference: str,
    code: str,
    otp_provider: OtpProvider,
    phone_protector: PhoneIdentityProtector,
    token_codec: AccessTokenCodec,
    refresh_session_ttl_seconds: int,
    access_token_ttl_seconds: int,
    idempotency_expires_at: datetime,
    now: datetime | None = None,
) -> LoginCredentials:
    if refresh_session_ttl_seconds <= 0 or access_token_ttl_seconds <= 0:
        raise IdentityInputError("authentication lifetimes must be positive")
    normalized_phone = normalize_phone(phone)
    phone_lookup_hmac = await phone_protector.lookup_hmac(normalized_phone)
    if len(phone_lookup_hmac) < 8:
        raise IdentityProviderNotConfiguredError(
            "phone identity protector returned an invalid lookup representation"
        )
    fingerprint = _login_fingerprint(phone_lookup_hmac, challenge_reference)
    await _reject_completed_login_replay(
        session_factory,
        client_login_id=client_login_id,
        fingerprint=fingerprint,
    )

    await otp_provider.verify(
        challenge_reference=challenge_reference,
        normalized_phone=normalized_phone,
        code=code,
    )
    protected_phone = await phone_protector.protect(normalized_phone)
    raw_refresh_credential = secrets.token_urlsafe(32)
    credential_hash = hash_refresh_credential(raw_refresh_credential)
    established_at = now or utc_now()

    async with session_factory() as session, session.begin():
        claim = await claim_idempotency_record(
            session,
            scope=AUTH_VERIFY_SCOPE,
            idempotency_key=str(client_login_id),
            request_fingerprint=fingerprint,
            expires_at=idempotency_expires_at,
            now=established_at,
        )
        if not claim.created:
            if get_completed_idempotency_result(claim.record) is not None:
                raise LoginCredentialsUnavailableReplayError(
                    "login already completed; start a new OTP verification"
                )
            raise LoginCommandInProgressError("login verification is already in progress")

        await acquire_phone_identity_advisory_lock(session, phone_lookup_hmac=phone_lookup_hmac)
        active_phone = await session.scalar(
            select(UserPhone).where(
                UserPhone.phone_lookup_hmac == phone_lookup_hmac,
                UserPhone.retired_at.is_(None),
            )
        )
        if active_phone is None:
            user = AppUser(
                user_id=new_uuid7(),
                status=APP_USER_ACTIVE,
                created_at=established_at,
                updated_at=established_at,
            )
            session.add(user)
            await session.flush([user])
            session.add_all(
                [
                    UserPhone(
                        user_phone_id=new_uuid7(),
                        user_id=user.user_id,
                        phone_encrypted=protected_phone,
                        phone_lookup_hmac=phone_lookup_hmac,
                        verified_at=established_at,
                        retired_at=None,
                        created_at=established_at,
                    ),
                    UserRole(
                        user_role_id=new_uuid7(),
                        user_id=user.user_id,
                        role_code=ROLE_CUSTOMER,
                        granted_at=established_at,
                        granted_by_user_id=None,
                        revoked_at=None,
                        revoked_by_user_id=None,
                    ),
                ]
            )
        else:
            existing_user = await session.get(AppUser, active_phone.user_id)
            if existing_user is None:
                raise RuntimeError("active phone references a missing application user")
            if existing_user.status != APP_USER_ACTIVE:
                raise UserAuthenticationDeniedError("authentication failed")
            user = existing_user

        refresh_session = RefreshSession(
            refresh_session_id=new_uuid7(),
            user_id=user.user_id,
            credential_hash=credential_hash,
            created_at=established_at,
            expires_at=established_at + timedelta(seconds=refresh_session_ttl_seconds),
            revoked_at=None,
        )
        session.add(refresh_session)
        await session.flush([refresh_session])
        await complete_idempotency_record(
            session,
            claim.record,
            result_resource_id=refresh_session.refresh_session_id,
            result_status_code=200,
            completed_at=established_at,
        )

    access_token = token_codec.issue(
        user_id=user.user_id,
        refresh_session_id=refresh_session.refresh_session_id,
        issued_at=established_at,
        ttl_seconds=access_token_ttl_seconds,
    )
    return LoginCredentials(
        user_id=user.user_id,
        refresh_session_id=refresh_session.refresh_session_id,
        access_token=SecretStr(access_token),
        refresh_token=SecretStr(raw_refresh_credential),
        expires_in=access_token_ttl_seconds,
    )


async def refresh_access_token(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    raw_refresh_credential: str,
    token_codec: AccessTokenCodec,
    access_token_ttl_seconds: int,
    now: datetime | None = None,
) -> AccessCredentials:
    if access_token_ttl_seconds <= 0:
        raise IdentityInputError("access-token lifetime must be positive")
    credential_hash = hash_refresh_credential(raw_refresh_credential)
    issued_at = now or utc_now()
    async with session_factory() as session, session.begin():
        refresh_session = await session.scalar(
            select(RefreshSession)
            .where(RefreshSession.credential_hash == credential_hash)
            .with_for_update()
        )
        user = await _require_active_refresh_session(session, refresh_session, now=issued_at)
        assert refresh_session is not None
        access_token = token_codec.issue(
            user_id=user.user_id,
            refresh_session_id=refresh_session.refresh_session_id,
            issued_at=issued_at,
            ttl_seconds=access_token_ttl_seconds,
        )
        return AccessCredentials(
            user_id=user.user_id,
            refresh_session_id=refresh_session.refresh_session_id,
            access_token=SecretStr(access_token),
            expires_in=access_token_ttl_seconds,
        )


async def logout_refresh_session(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    raw_refresh_credential: str,
    now: datetime | None = None,
) -> None:
    credential_hash = hash_refresh_credential(raw_refresh_credential)
    revoked_at = now or utc_now()
    async with session_factory() as session, session.begin():
        refresh_session = await session.scalar(
            select(RefreshSession)
            .where(RefreshSession.credential_hash == credential_hash)
            .with_for_update()
        )
        if (
            refresh_session is not None
            and refresh_session.revoked_at is None
            and refresh_session.expires_at > revoked_at
        ):
            refresh_session.revoked_at = revoked_at
            await session.flush([refresh_session])


async def authenticate_access_token(
    session: AsyncSession,
    *,
    raw_access_token: str,
    token_codec: AccessTokenCodec,
    now: datetime | None = None,
) -> AuthenticatedPrincipal:
    try:
        claims = token_codec.verify(raw_access_token)
    except AccessTokenInvalidError as error:
        raise AccessAuthenticationError("authentication failed") from error
    checked_at = now or utc_now()
    refresh_session = await session.get(RefreshSession, claims.refresh_session_id)
    if (
        refresh_session is None
        or refresh_session.user_id != claims.user_id
        or refresh_session.revoked_at is not None
        or refresh_session.expires_at <= checked_at
    ):
        raise AccessAuthenticationError("authentication failed")
    user = await session.get(AppUser, claims.user_id)
    if user is None or user.status != APP_USER_ACTIVE:
        raise AccessAuthenticationError("authentication failed")
    roles = frozenset(
        await session.scalars(
            select(UserRole.role_code).where(
                UserRole.user_id == user.user_id,
                UserRole.revoked_at.is_(None),
            )
        )
    )
    return AuthenticatedPrincipal(
        user_id=user.user_id,
        refresh_session_id=refresh_session.refresh_session_id,
        roles=roles,
    )


async def _reject_completed_login_replay(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    client_login_id: UUID,
    fingerprint: bytes,
) -> None:
    async with session_factory() as session:
        record = await session.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.scope == AUTH_VERIFY_SCOPE,
                IdempotencyRecord.idempotency_key == str(client_login_id),
            )
        )
        if record is None:
            return
        if not compare_digest(record.request_fingerprint, fingerprint):
            raise IdempotencyKeyConflictError(
                "idempotency key is already associated with a different request"
            )
        if get_completed_idempotency_result(record) is not None:
            raise LoginCredentialsUnavailableReplayError(
                "login already completed; start a new OTP verification"
            )


def _login_fingerprint(phone_lookup_hmac: bytes, challenge_reference: str) -> bytes:
    return command_fingerprint(
        {
            "phone_lookup_hmac": phone_lookup_hmac.hex(),
            "challenge_reference": challenge_reference,
        }
    )


async def _require_active_refresh_session(
    session: AsyncSession,
    refresh_session: RefreshSession | None,
    *,
    now: datetime,
) -> AppUser:
    if (
        refresh_session is None
        or refresh_session.revoked_at is not None
        or refresh_session.expires_at <= now
    ):
        raise RefreshAuthenticationError("authentication failed")
    user = await session.get(AppUser, refresh_session.user_id)
    if user is None or user.status != APP_USER_ACTIVE:
        raise RefreshAuthenticationError("authentication failed")
    return user
