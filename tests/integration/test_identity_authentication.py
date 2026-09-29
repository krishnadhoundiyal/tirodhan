from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Annotated, Any
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text, update
from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tirodhan.api.dependencies import require_role
from tirodhan.core.config import Settings
from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.main import create_app
from tirodhan.modules.identity import service as identity_service
from tirodhan.modules.identity.models import AppUser, RefreshSession, UserPhone, UserRole
from tirodhan.modules.identity.ports import (
    OtpChallenge,
    OtpRequestConflictError,
    OtpVerificationError,
)
from tirodhan.modules.identity.service import (
    AUTH_VERIFY_SCOPE,
    ROLE_CUSTOMER,
    ROLE_MANAGER,
    ROLE_RIDER,
    AccessAuthenticationError,
    AuthenticatedPrincipal,
    LoginCredentials,
    LoginCredentialsUnavailableReplayError,
    RefreshAuthenticationError,
    UserAuthenticationDeniedError,
    authenticate_access_token,
    hash_refresh_credential,
    logout_refresh_session,
    refresh_access_token,
    start_otp_verification,
    verify_otp_and_login,
)
from tirodhan.modules.identity.tokens import Rs256AccessTokenCodec
from tirodhan.modules.reliability.models import IdempotencyRecord, InboxMessage, OutboxEvent
from tirodhan.modules.reliability.primitives import IdempotencyKeyConflictError

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

PHONE = "+919876543210"
OTHER_PHONE = "+14155552671"
OTP_CODE = "123456"
ACCESS_TTL = 300
REFRESH_TTL = 3600


class DeterministicPhoneProtector:
    _encryption_key = b"test-phone-encryption-key"
    _lookup_key = b"test-phone-lookup-key"

    async def protect(self, normalized_phone: str) -> bytes:
        plaintext = normalized_phone.encode()
        encrypted = bytes(
            value ^ self._encryption_key[index % len(self._encryption_key)]
            for index, value in enumerate(plaintext)
        )
        return b"test-envelope:" + encrypted

    async def lookup_hmac(self, normalized_phone: str) -> bytes:
        return hmac.new(self._lookup_key, normalized_phone.encode(), hashlib.sha256).digest()


class DeterministicOtpProvider:
    def __init__(self) -> None:
        self._requests: dict[UUID, tuple[str, OtpChallenge]] = {}
        self._challenges: dict[str, str] = {}
        self.send_count = 0
        self.verify_count = 0
        self.before_verify_return: Callable[[], Awaitable[None]] | None = None

    async def start_verification(
        self, *, normalized_phone: str, client_request_id: UUID
    ) -> OtpChallenge:
        existing = self._requests.get(client_request_id)
        if existing is not None:
            if existing[0] != normalized_phone:
                raise OtpRequestConflictError("OTP request identity was reused")
            return existing[1]
        challenge = OtpChallenge(challenge_reference=f"challenge-{new_uuid7()}")
        self._requests[client_request_id] = (normalized_phone, challenge)
        self._challenges[challenge.challenge_reference] = normalized_phone
        self.send_count += 1
        return challenge

    async def verify(
        self,
        *,
        challenge_reference: str,
        normalized_phone: str,
        code: str,
    ) -> None:
        self.verify_count += 1
        if self._challenges.get(challenge_reference) != normalized_phone or code != OTP_CODE:
            raise OtpVerificationError("OTP verification failed")
        if self.before_verify_return is not None:
            await self.before_verify_return()


@lru_cache
def rsa_keys() -> tuple[str, str]:
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


def token_codec() -> Rs256AccessTokenCodec:
    private_key, public_key = rsa_keys()
    return Rs256AccessTokenCodec(
        private_key_pem=private_key,
        public_key_pem=public_key,
        issuer="https://identity.test",
        audience="tirodhan-test",
    )


async def begin_challenge(provider: DeterministicOtpProvider, phone: str = PHONE) -> OtpChallenge:
    return await start_otp_verification(
        provider,
        phone=phone,
        client_request_id=new_uuid7(),
    )


async def login(
    factory: async_sessionmaker[AsyncSession],
    provider: DeterministicOtpProvider,
    protector: DeterministicPhoneProtector,
    *,
    phone: str = PHONE,
    client_login_id: UUID | None = None,
    challenge: OtpChallenge | None = None,
    now: datetime | None = None,
) -> LoginCredentials:
    established_at = now or utc_now().replace(microsecond=0)
    challenge = challenge or await begin_challenge(provider, phone)
    return await verify_otp_and_login(
        factory,
        client_login_id=client_login_id or new_uuid7(),
        phone=phone,
        challenge_reference=challenge.challenge_reference,
        code=OTP_CODE,
        otp_provider=provider,
        phone_protector=protector,
        token_codec=token_codec(),
        refresh_session_ttl_seconds=REFRESH_TTL,
        access_token_ttl_seconds=ACCESS_TTL,
        idempotency_expires_at=established_at + timedelta(days=1),
        now=established_at,
    )


async def counts(factory: async_sessionmaker[AsyncSession]) -> tuple[int, int, int, int]:
    async with factory() as session:
        return tuple(
            int(value or 0)
            for value in (
                await session.scalar(select(func.count()).select_from(AppUser)),
                await session.scalar(select(func.count()).select_from(UserPhone)),
                await session.scalar(select(func.count()).select_from(UserRole)),
                await session.scalar(select(func.count()).select_from(RefreshSession)),
            )
        )  # type: ignore[return-value]


async def test_otp_start_provider_contract_converges_and_requires_explicit_resend() -> None:
    provider = DeterministicOtpProvider()
    command_id = new_uuid7()

    first = await start_otp_verification(provider, phone=PHONE, client_request_id=command_id)
    replay = await start_otp_verification(provider, phone=PHONE, client_request_id=command_id)
    with pytest.raises(OtpRequestConflictError):
        await start_otp_verification(provider, phone=OTHER_PHONE, client_request_id=command_id)
    resend = await start_otp_verification(provider, phone=PHONE, client_request_id=new_uuid7())

    assert replay == first
    assert resend != first
    assert provider.send_count == 2


async def test_identity_schema_constraints(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    assert set(UserPhone.__table__.columns.keys()) == {
        "user_phone_id",
        "user_id",
        "phone_encrypted",
        "phone_lookup_hmac",
        "verified_at",
        "retired_at",
        "created_at",
    }
    now = utc_now()
    user_a, user_b = AppUser(status="ACTIVE"), AppUser(status="ACTIVE")
    async with database_session_factory() as session, session.begin():
        session.add_all([user_a, user_b])
        await session.flush()

    active_phone = UserPhone(
        user_phone_id=new_uuid7(),
        user_id=user_a.user_id,
        phone_encrypted=b"encrypted-a",
        phone_lookup_hmac=b"hmac-a",
        verified_at=now,
        retired_at=None,
        created_at=now,
    )
    await persist(database_session_factory, active_phone)
    with pytest.raises(IntegrityError):
        await persist(
            database_session_factory,
            UserPhone(
                user_phone_id=new_uuid7(),
                user_id=user_b.user_id,
                phone_encrypted=b"encrypted-b",
                phone_lookup_hmac=b"hmac-a",
                verified_at=now,
                retired_at=None,
                created_at=now,
            ),
        )
    with pytest.raises(IntegrityError):
        await persist(
            database_session_factory,
            UserPhone(
                user_phone_id=new_uuid7(),
                user_id=user_a.user_id,
                phone_encrypted=b"encrypted-c",
                phone_lookup_hmac=b"hmac-c",
                verified_at=now,
                retired_at=None,
                created_at=now,
            ),
        )
    async with database_session_factory() as session, session.begin():
        await session.execute(
            update(UserPhone)
            .where(UserPhone.user_phone_id == active_phone.user_phone_id)
            .values(retired_at=now)
        )
    await persist(
        database_session_factory,
        UserPhone(
            user_phone_id=new_uuid7(),
            user_id=user_a.user_id,
            phone_encrypted=b"encrypted-new",
            phone_lookup_hmac=b"hmac-new",
            verified_at=now,
            retired_at=None,
            created_at=now,
        ),
    )
    with pytest.raises(IntegrityError):
        await persist(
            database_session_factory,
            UserPhone(
                user_phone_id=new_uuid7(),
                user_id=new_uuid7(),
                phone_encrypted=b"encrypted-orphan",
                phone_lookup_hmac=b"hmac-orphan",
                verified_at=now,
                retired_at=None,
                created_at=now,
            ),
        )

    role = UserRole(
        user_role_id=new_uuid7(),
        user_id=user_a.user_id,
        role_code=ROLE_CUSTOMER,
        granted_at=now,
        granted_by_user_id=None,
        revoked_at=None,
        revoked_by_user_id=None,
    )
    await persist(database_session_factory, role)
    with pytest.raises(IntegrityError):
        await persist(
            database_session_factory,
            UserRole(
                user_role_id=new_uuid7(),
                user_id=user_a.user_id,
                role_code=ROLE_CUSTOMER,
                granted_at=now,
                granted_by_user_id=None,
                revoked_at=None,
                revoked_by_user_id=None,
            ),
        )
    async with database_session_factory() as session, session.begin():
        await session.execute(
            update(UserRole)
            .where(UserRole.user_role_id == role.user_role_id)
            .values(revoked_at=now)
        )
    await persist(
        database_session_factory,
        UserRole(
            user_role_id=new_uuid7(),
            user_id=user_a.user_id,
            role_code=ROLE_CUSTOMER,
            granted_at=now,
            granted_by_user_id=None,
            revoked_at=None,
            revoked_by_user_id=None,
        ),
    )
    async with database_session_factory() as session:
        customer_grants = await session.scalar(
            select(func.count())
            .select_from(UserRole)
            .where(
                UserRole.user_id == user_a.user_id,
                UserRole.role_code == ROLE_CUSTOMER,
            )
        )
    assert customer_grants == 2
    with pytest.raises(IntegrityError):
        await persist(
            database_session_factory,
            UserRole(
                user_role_id=new_uuid7(),
                user_id=user_b.user_id,
                role_code="ADMIN",
                granted_at=now,
                granted_by_user_id=None,
                revoked_at=None,
                revoked_by_user_id=None,
            ),
        )

    session_a = RefreshSession(
        refresh_session_id=new_uuid7(),
        user_id=user_a.user_id,
        credential_hash=b"credential-a",
        created_at=now,
        expires_at=now + timedelta(hours=1),
        revoked_at=None,
    )
    await persist(database_session_factory, session_a)
    await persist(
        database_session_factory,
        RefreshSession(
            refresh_session_id=new_uuid7(),
            user_id=user_a.user_id,
            credential_hash=b"credential-b",
            created_at=now,
            expires_at=now + timedelta(hours=1),
            revoked_at=None,
        ),
    )
    with pytest.raises(IntegrityError):
        await persist(
            database_session_factory,
            RefreshSession(
                refresh_session_id=new_uuid7(),
                user_id=user_b.user_id,
                credential_hash=b"credential-a",
                created_at=now,
                expires_at=now + timedelta(hours=1),
                revoked_at=None,
            ),
        )
    with pytest.raises(IntegrityError):
        await persist(
            database_session_factory,
            RefreshSession(
                refresh_session_id=new_uuid7(),
                user_id=user_b.user_id,
                credential_hash=b"credential-invalid-expiry",
                created_at=now,
                expires_at=now,
                revoked_at=None,
            ),
        )


async def persist(factory: async_sessionmaker[AsyncSession], instance: object) -> None:
    async with factory() as session, session.begin():
        session.add(instance)
        await session.flush()


async def test_first_and_existing_phone_login_use_one_identity_and_independent_sessions(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    provider = DeterministicOtpProvider()
    protector = DeterministicPhoneProtector()
    first = await login(database_session_factory, provider, protector)
    second = await login(database_session_factory, provider, protector)

    async with database_session_factory() as session:
        phone = await session.scalar(select(UserPhone))
        role = await session.scalar(select(UserRole))
        sessions = list(await session.scalars(select(RefreshSession)))
    assert first.user_id == second.user_id
    assert first.refresh_session_id != second.refresh_session_id
    assert await counts(database_session_factory) == (1, 1, 1, 2)
    assert phone is not None and PHONE.encode() not in phone.phone_encrypted
    assert role is not None
    assert (role.role_code, role.granted_by_user_id) == (ROLE_CUSTOMER, None)
    assert {item.user_id for item in sessions} == {first.user_id}


async def test_existing_user_without_customer_is_not_silently_regranted(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    provider = DeterministicOtpProvider()
    protector = DeterministicPhoneProtector()
    first = await login(database_session_factory, provider, protector)
    async with database_session_factory() as session, session.begin():
        await session.execute(
            update(UserRole)
            .where(UserRole.user_id == first.user_id, UserRole.role_code == ROLE_CUSTOMER)
            .values(revoked_at=utc_now())
        )

    second = await login(database_session_factory, provider, protector)
    async with database_session_factory() as session:
        active_customer_count = await session.scalar(
            select(func.count())
            .select_from(UserRole)
            .where(
                UserRole.user_id == first.user_id,
                UserRole.role_code == ROLE_CUSTOMER,
                UserRole.revoked_at.is_(None),
            )
        )
    assert second.user_id == first.user_id
    assert active_customer_count == 0
    assert await counts(database_session_factory) == (1, 1, 1, 2)


async def test_nonactive_existing_user_cannot_create_session(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    provider = DeterministicOtpProvider()
    protector = DeterministicPhoneProtector()
    first = await login(database_session_factory, provider, protector)
    async with database_session_factory() as session, session.begin():
        await session.execute(
            update(AppUser).where(AppUser.user_id == first.user_id).values(status="DISABLED")
        )

    with pytest.raises(UserAuthenticationDeniedError):
        await login(database_session_factory, provider, protector)
    assert await counts(database_session_factory) == (1, 1, 1, 1)


async def test_concurrent_first_logins_same_phone_create_one_identity_without_orphan(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    provider = DeterministicOtpProvider()
    protector = DeterministicPhoneProtector()
    challenges = await asyncio.gather(begin_challenge(provider), begin_challenge(provider))

    results = await asyncio.gather(
        login(database_session_factory, provider, protector, challenge=challenges[0]),
        login(database_session_factory, provider, protector, challenge=challenges[1]),
    )

    assert results[0].user_id == results[1].user_id
    assert results[0].refresh_session_id != results[1].refresh_session_id
    assert await counts(database_session_factory) == (1, 1, 1, 2)


async def test_concurrent_exact_login_creates_one_session_and_conflicting_fingerprint_fails(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    provider = DeterministicOtpProvider()
    protector = DeterministicPhoneProtector()
    challenge = await begin_challenge(provider)
    client_login_id = new_uuid7()

    results = await asyncio.gather(
        *(
            login(
                database_session_factory,
                provider,
                protector,
                challenge=challenge,
                client_login_id=client_login_id,
            )
            for _ in range(2)
        ),
        return_exceptions=True,
    )
    successes = [item for item in results if isinstance(item, LoginCredentials)]
    failures = [item for item in results if isinstance(item, Exception)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], LoginCredentialsUnavailableReplayError)
    assert await counts(database_session_factory) == (1, 1, 1, 1)

    changed_challenge = await begin_challenge(provider)
    verify_count = provider.verify_count
    with pytest.raises(IdempotencyKeyConflictError):
        await login(
            database_session_factory,
            provider,
            protector,
            challenge=changed_challenge,
            client_login_id=client_login_id,
        )
    assert provider.verify_count == verify_count


async def test_completed_login_replay_never_reverifies_or_persists_credentials(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    provider = DeterministicOtpProvider()
    protector = DeterministicPhoneProtector()
    challenge = await begin_challenge(provider)
    client_login_id = new_uuid7()
    first = await login(
        database_session_factory,
        provider,
        protector,
        challenge=challenge,
        client_login_id=client_login_id,
    )
    calls_after_success = provider.verify_count

    with pytest.raises(LoginCredentialsUnavailableReplayError):
        await login(
            database_session_factory,
            provider,
            protector,
            challenge=challenge,
            client_login_id=client_login_id,
        )

    raw_refresh = first.refresh_token.get_secret_value()
    raw_access = first.access_token.get_secret_value()
    async with database_session_factory() as session:
        record = await session.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.scope == AUTH_VERIFY_SCOPE,
                IdempotencyRecord.idempotency_key == str(client_login_id),
            )
        )
        refresh_session = await session.get(RefreshSession, first.refresh_session_id)
        outbox_count = await session.scalar(select(func.count()).select_from(OutboxEvent))
        inbox_count = await session.scalar(select(func.count()).select_from(InboxMessage))
    assert provider.verify_count == calls_after_success
    assert await counts(database_session_factory) == (1, 1, 1, 1)
    assert record is not None and record.result_resource_id == first.refresh_session_id
    assert refresh_session is not None
    assert refresh_session.credential_hash == hash_refresh_credential(raw_refresh)
    assert raw_refresh.encode() not in refresh_session.credential_hash
    assert raw_refresh.encode() not in record.request_fingerprint
    assert raw_access.encode() not in record.request_fingerprint
    assert outbox_count == inbox_count == 0


async def test_refresh_is_stable_repeatable_and_concurrent_without_session_mutation(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    provider = DeterministicOtpProvider()
    first = await login(database_session_factory, provider, DeterministicPhoneProtector())
    raw_refresh = first.refresh_token.get_secret_value()
    async with database_session_factory() as session:
        before = await session.get(RefreshSession, first.refresh_session_id)
        assert before is not None
        state_before = (before.credential_hash, before.expires_at, before.revoked_at)

    issued_at = utc_now().replace(microsecond=0)
    sequential = await refresh_access_token(
        database_session_factory,
        raw_refresh_credential=raw_refresh,
        token_codec=token_codec(),
        access_token_ttl_seconds=ACCESS_TTL,
        now=issued_at,
    )
    repeated = await refresh_access_token(
        database_session_factory,
        raw_refresh_credential=raw_refresh,
        token_codec=token_codec(),
        access_token_ttl_seconds=ACCESS_TTL,
        now=issued_at + timedelta(seconds=1),
    )
    concurrent = await asyncio.gather(
        *(
            refresh_access_token(
                database_session_factory,
                raw_refresh_credential=raw_refresh,
                token_codec=token_codec(),
                access_token_ttl_seconds=ACCESS_TTL,
            )
            for _ in range(2)
        )
    )

    async with database_session_factory() as session:
        after = await session.get(RefreshSession, first.refresh_session_id)
    assert sequential.refresh_session_id == repeated.refresh_session_id == first.refresh_session_id
    assert all(item.refresh_session_id == first.refresh_session_id for item in concurrent)
    assert after is not None
    assert (after.credential_hash, after.expires_at, after.revoked_at) == state_before
    assert await counts(database_session_factory) == (1, 1, 1, 1)


async def test_refresh_rejects_unknown_revoked_expired_and_nonactive_sessions(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    provider = DeterministicOtpProvider()
    protector = DeterministicPhoneProtector()
    with pytest.raises(RefreshAuthenticationError):
        await refresh_access_token(
            database_session_factory,
            raw_refresh_credential="unknown-credential",
            token_codec=token_codec(),
            access_token_ttl_seconds=ACCESS_TTL,
        )

    revoked = await login(database_session_factory, provider, protector, phone=PHONE)
    await logout_refresh_session(
        database_session_factory,
        raw_refresh_credential=revoked.refresh_token.get_secret_value(),
    )
    with pytest.raises(RefreshAuthenticationError):
        await refresh_access_token(
            database_session_factory,
            raw_refresh_credential=revoked.refresh_token.get_secret_value(),
            token_codec=token_codec(),
            access_token_ttl_seconds=ACCESS_TTL,
        )

    expired = await login(database_session_factory, provider, protector, phone=PHONE)
    past = utc_now() - timedelta(hours=2)
    async with database_session_factory() as session, session.begin():
        await session.execute(
            update(RefreshSession)
            .where(RefreshSession.refresh_session_id == expired.refresh_session_id)
            .values(created_at=past, expires_at=past + timedelta(hours=1))
        )
    with pytest.raises(RefreshAuthenticationError):
        await refresh_access_token(
            database_session_factory,
            raw_refresh_credential=expired.refresh_token.get_secret_value(),
            token_codec=token_codec(),
            access_token_ttl_seconds=ACCESS_TTL,
        )

    disabled = await login(database_session_factory, provider, protector, phone=PHONE)
    async with database_session_factory() as session, session.begin():
        await session.execute(
            update(AppUser).where(AppUser.user_id == disabled.user_id).values(status="DISABLED")
        )
    with pytest.raises(RefreshAuthenticationError):
        await refresh_access_token(
            database_session_factory,
            raw_refresh_credential=disabled.refresh_token.get_secret_value(),
            token_codec=token_codec(),
            access_token_ttl_seconds=ACCESS_TTL,
        )


async def test_logout_is_idempotent_and_unknown_credential_is_generic_success(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    credentials = await login(
        database_session_factory,
        DeterministicOtpProvider(),
        DeterministicPhoneProtector(),
    )
    raw_refresh = credentials.refresh_token.get_secret_value()
    await logout_refresh_session(database_session_factory, raw_refresh_credential=raw_refresh)
    await logout_refresh_session(database_session_factory, raw_refresh_credential=raw_refresh)
    await logout_refresh_session(
        database_session_factory, raw_refresh_credential="unknown-credential"
    )
    async with database_session_factory() as session:
        refresh_session = await session.get(RefreshSession, credentials.refresh_session_id)
    assert refresh_session is not None and refresh_session.revoked_at is not None
    assert raw_refresh.encode() not in refresh_session.credential_hash


async def test_refresh_then_logout_race_revokes_returned_access_immediately(
    database_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credentials = await login(
        database_session_factory,
        DeterministicOtpProvider(),
        DeterministicPhoneProtector(),
    )
    raw_refresh = credentials.refresh_token.get_secret_value()
    refresh_holds_lock = asyncio.Event()
    release_refresh = asyncio.Event()
    original = identity_service._require_active_refresh_session

    async def held_validation(
        session: AsyncSession,
        refresh_session: RefreshSession | None,
        *,
        now: datetime,
    ) -> AppUser:
        user = await original(session, refresh_session, now=now)
        refresh_holds_lock.set()
        await release_refresh.wait()
        return user

    monkeypatch.setattr(identity_service, "_require_active_refresh_session", held_validation)
    refresh_task = asyncio.create_task(
        refresh_access_token(
            database_session_factory,
            raw_refresh_credential=raw_refresh,
            token_codec=token_codec(),
            access_token_ttl_seconds=ACCESS_TTL,
        )
    )
    await refresh_holds_lock.wait()
    logout_task = asyncio.create_task(
        logout_refresh_session(
            database_session_factory,
            raw_refresh_credential=raw_refresh,
        )
    )
    release_refresh.set()
    refreshed, _ = await asyncio.gather(refresh_task, logout_task)

    async with database_session_factory() as session:
        with pytest.raises(AccessAuthenticationError):
            await authenticate_access_token(
                session,
                raw_access_token=refreshed.access_token.get_secret_value(),
                token_codec=token_codec(),
            )


async def test_logout_then_refresh_race_rejects_refresh_after_waiting_for_row_lock(
    database_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credentials = await login(
        database_session_factory,
        DeterministicOtpProvider(),
        DeterministicPhoneProtector(),
    )
    raw_refresh = credentials.refresh_token.get_secret_value()
    logout_holds_lock = asyncio.Event()
    release_logout = asyncio.Event()
    original_flush = AsyncSession.flush

    async def held_logout_flush(session: AsyncSession, objects: Any = None) -> None:
        await original_flush(session, objects)
        if objects and any(
            isinstance(value, RefreshSession) and value.revoked_at is not None for value in objects
        ):
            logout_holds_lock.set()
            await release_logout.wait()

    monkeypatch.setattr(AsyncSession, "flush", held_logout_flush)
    codec = token_codec()
    issue_count = 0
    original_issue = codec.issue

    def tracked_issue(**kwargs: Any) -> str:
        nonlocal issue_count
        issue_count += 1
        return original_issue(**kwargs)

    monkeypatch.setattr(codec, "issue", tracked_issue)

    async with database_session_factory() as session:
        before = await session.get(RefreshSession, credentials.refresh_session_id)
        assert before is not None
        original_hash = before.credential_hash
        original_expiry = before.expires_at

    logout_task = asyncio.create_task(
        logout_refresh_session(
            database_session_factory,
            raw_refresh_credential=raw_refresh,
        )
    )
    await logout_holds_lock.wait()
    refresh_task = asyncio.create_task(
        refresh_access_token(
            database_session_factory,
            raw_refresh_credential=raw_refresh,
            token_codec=codec,
            access_token_ttl_seconds=ACCESS_TTL,
        )
    )
    await asyncio.sleep(0.1)
    assert not refresh_task.done()

    release_logout.set()
    await logout_task
    with pytest.raises(RefreshAuthenticationError):
        await refresh_task

    async with database_session_factory() as session:
        sessions = (
            await session.scalars(
                select(RefreshSession).where(RefreshSession.user_id == credentials.user_id)
            )
        ).all()
    assert len(sessions) == 1
    assert sessions[0].revoked_at is not None
    assert sessions[0].credential_hash == original_hash
    assert sessions[0].expires_at == original_expiry
    assert issue_count == 0


async def test_live_database_authorization_reflects_role_user_and_session_changes(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    credentials = await login(
        database_session_factory,
        DeterministicOtpProvider(),
        DeterministicPhoneProtector(),
    )
    second_credentials = await login(
        database_session_factory,
        DeterministicOtpProvider(),
        DeterministicPhoneProtector(),
    )
    token = credentials.access_token.get_secret_value()
    async with database_session_factory() as session:
        principal = await authenticate_access_token(
            session, raw_access_token=token, token_codec=token_codec()
        )
    assert principal.roles == frozenset({ROLE_CUSTOMER})

    async with database_session_factory() as session, session.begin():
        await session.execute(
            update(UserRole)
            .where(UserRole.user_id == credentials.user_id)
            .values(revoked_at=utc_now())
        )
    async with database_session_factory() as session:
        principal = await authenticate_access_token(
            session, raw_access_token=token, token_codec=token_codec()
        )
    assert principal.roles == frozenset()

    async with database_session_factory() as session, session.begin():
        past = utc_now() - timedelta(hours=2)
        await session.execute(
            update(RefreshSession)
            .where(RefreshSession.refresh_session_id == credentials.refresh_session_id)
            .values(created_at=past, expires_at=past + timedelta(hours=1))
        )
    async with database_session_factory() as session:
        with pytest.raises(AccessAuthenticationError):
            await authenticate_access_token(
                session, raw_access_token=token, token_codec=token_codec()
            )

    async with database_session_factory() as session, session.begin():
        await session.execute(
            update(AppUser).where(AppUser.user_id == credentials.user_id).values(status="DISABLED")
        )
    async with database_session_factory() as session:
        with pytest.raises(AccessAuthenticationError):
            await authenticate_access_token(
                session,
                raw_access_token=second_credentials.access_token.get_secret_value(),
                token_codec=token_codec(),
            )


async def test_auth_api_flow_customer_route_rbac_and_sensitive_logs(
    database_session_factory: async_sessionmaker[AsyncSession],
    migrated_database_url: str,
    address_protector: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider = DeterministicOtpProvider()
    phone_protector = DeterministicPhoneProtector()
    application = auth_application(
        migrated_database_url,
        provider,
        phone_protector,
        address_protector=address_protector,
    )
    add_role_test_routes(application)
    caplog.set_level(logging.INFO)

    async with application.router.lifespan_context(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://test"
        ) as client:
            assert (await client.get("/health")).status_code == 200
            assert (await client.get("/v1/addresses")).status_code == 401
            assert (
                await client.get("/v1/addresses", headers={"Authorization": "Bearer malformed"})
            ).status_code == 401
            start = await client.post(
                "/v1/auth/otp/start",
                json={"client_request_id": str(new_uuid7()), "phone": PHONE},
            )
            assert start.status_code == 202
            invalid = await client.post(
                "/v1/auth/otp/verify",
                json={
                    "client_login_id": str(new_uuid7()),
                    "phone": PHONE,
                    "challenge_reference": start.json()["challenge_reference"],
                    "code": "000000",
                },
            )
            assert invalid.status_code == 401
            verify = await client.post(
                "/v1/auth/otp/verify",
                json={
                    "client_login_id": str(new_uuid7()),
                    "phone": PHONE,
                    "challenge_reference": start.json()["challenge_reference"],
                    "code": OTP_CODE,
                },
            )
            assert verify.status_code == 200
            body = verify.json()
            access_token = body["access_token"]
            refresh_token = body["refresh_token"]
            headers = {"Authorization": f"Bearer {access_token}"}
            assert (await client.get("/v1/addresses", headers=headers)).status_code == 200
            assert (await client.get("/test/rider", headers=headers)).status_code == 403

            user_id = UUID(body["user_id"])
            async with database_session_factory() as session, session.begin():
                session.add_all(
                    [
                        UserRole(
                            user_role_id=new_uuid7(),
                            user_id=user_id,
                            role_code=ROLE_RIDER,
                            granted_at=utc_now(),
                            granted_by_user_id=None,
                            revoked_at=None,
                            revoked_by_user_id=None,
                        ),
                        UserRole(
                            user_role_id=new_uuid7(),
                            user_id=user_id,
                            role_code=ROLE_MANAGER,
                            granted_at=utc_now(),
                            granted_by_user_id=None,
                            revoked_at=None,
                            revoked_by_user_id=None,
                        ),
                    ]
                )
            assert (await client.get("/test/rider", headers=headers)).status_code == 200
            assert (await client.get("/test/manager", headers=headers)).status_code == 200

            async with database_session_factory() as session, session.begin():
                await session.execute(
                    update(UserRole)
                    .where(UserRole.user_id == user_id, UserRole.role_code == ROLE_RIDER)
                    .values(revoked_at=utc_now())
                )
            assert (await client.get("/test/rider", headers=headers)).status_code == 403
            assert (await client.get("/v1/addresses", headers=headers)).status_code == 200

            async with database_session_factory() as session, session.begin():
                await session.execute(
                    update(UserRole)
                    .where(UserRole.user_id == user_id, UserRole.role_code == ROLE_MANAGER)
                    .values(revoked_at=utc_now())
                )
            assert (await client.get("/test/manager", headers=headers)).status_code == 403

            async with database_session_factory() as session, session.begin():
                await session.execute(
                    update(UserRole)
                    .where(UserRole.user_id == user_id, UserRole.role_code == ROLE_CUSTOMER)
                    .values(revoked_at=utc_now())
                )
            assert (await client.get("/v1/addresses", headers=headers)).status_code == 403

            refreshed = await client.post("/v1/auth/refresh", json={"refresh_token": refresh_token})
            assert refreshed.status_code == 200
            assert "refresh_token" not in refreshed.json()
            refreshed_access = refreshed.json()["access_token"]

            assert (
                await client.post("/v1/auth/logout", json={"refresh_token": refresh_token})
            ).status_code == 204
            assert (
                await client.post("/v1/auth/logout", json={"refresh_token": refresh_token})
            ).status_code == 204
            assert (
                await client.post("/v1/auth/logout", json={"refresh_token": "unknown-token"})
            ).status_code == 204
            assert (
                await client.post("/v1/auth/refresh", json={"refresh_token": refresh_token})
            ).status_code == 401
            assert (
                await client.post("/v1/auth/refresh", json={"refresh_token": "unknown-token"})
            ).status_code == 401
            assert (
                await client.get(
                    "/v1/addresses",
                    headers={"Authorization": f"Bearer {refreshed_access}"},
                )
            ).status_code == 401

    logs = caplog.text
    for sensitive in (PHONE, OTP_CODE, refresh_token, access_token, refreshed_access):
        assert sensitive not in logs


async def test_auth_api_rejects_malformed_phone_before_otp_verification(
    migrated_database_url: str,
) -> None:
    provider = DeterministicOtpProvider()
    application = auth_application(
        migrated_database_url,
        provider,
        DeterministicPhoneProtector(),
    )
    malformed_phone = "9876543210"

    async with application.router.lifespan_context(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://test"
        ) as client:
            start = await client.post(
                "/v1/auth/otp/start",
                json={"client_request_id": str(new_uuid7()), "phone": malformed_phone},
            )
            verify = await client.post(
                "/v1/auth/otp/verify",
                json={
                    "client_login_id": str(new_uuid7()),
                    "phone": malformed_phone,
                    "challenge_reference": "untrusted-challenge",
                    "code": OTP_CODE,
                },
            )

    assert start.status_code == 422
    assert verify.status_code == 422
    assert provider.send_count == 0
    assert provider.verify_count == 0


async def test_api_completed_login_replay_and_runtime_configuration_failures(
    database_session_factory: async_sessionmaker[AsyncSession],
    migrated_database_url: str,
) -> None:
    provider = DeterministicOtpProvider()
    protector = DeterministicPhoneProtector()
    application = auth_application(migrated_database_url, provider, protector)
    client_login_id = new_uuid7()
    async with application.router.lifespan_context(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://test"
        ) as client:
            start = await client.post(
                "/v1/auth/otp/start",
                json={"client_request_id": str(new_uuid7()), "phone": PHONE},
            )
            payload = {
                "client_login_id": str(client_login_id),
                "phone": PHONE,
                "challenge_reference": start.json()["challenge_reference"],
                "code": OTP_CODE,
            }
            assert (await client.post("/v1/auth/otp/verify", json=payload)).status_code == 200
            verify_count = provider.verify_count
            replay = await client.post("/v1/auth/otp/verify", json=payload)
            assert replay.status_code == 409
            assert replay.json() == {
                "detail": "Login already completed; start a new OTP verification"
            }
            assert provider.verify_count == verify_count
    assert await counts(database_session_factory) == (1, 1, 1, 1)

    unconfigured = create_app(
        Settings(
            _env_file=None,
            environment="test",
            database_url=migrated_database_url,
            auth_access_token_ttl_seconds=ACCESS_TTL,
            auth_refresh_session_ttl_seconds=REFRESH_TTL,
            command_idempotency_ttl_seconds=3600,
        )
    )
    async with unconfigured.router.lifespan_context(unconfigured):
        async with AsyncClient(
            transport=ASGITransport(app=unconfigured), base_url="http://test"
        ) as client:
            response = await client.post(
                "/v1/auth/otp/start",
                json={"client_request_id": str(new_uuid7()), "phone": PHONE},
            )
            assert response.status_code == 503

    base_settings = Settings(
        _env_file=None,
        environment="test",
        database_url=migrated_database_url,
        auth_access_token_ttl_seconds=ACCESS_TTL,
        auth_refresh_session_ttl_seconds=REFRESH_TTL,
        command_idempotency_ttl_seconds=3600,
    )
    challenge = await begin_challenge(provider)
    missing_protector = create_app(
        base_settings,
        otp_provider=provider,
        access_token_codec=token_codec(),
    )
    missing_codec = create_app(
        base_settings,
        otp_provider=provider,
        phone_identity_protector=protector,
    )
    verify_payload = {
        "client_login_id": str(new_uuid7()),
        "phone": PHONE,
        "challenge_reference": challenge.challenge_reference,
        "code": OTP_CODE,
    }
    for misconfigured in (missing_protector, missing_codec):
        async with misconfigured.router.lifespan_context(misconfigured):
            async with AsyncClient(
                transport=ASGITransport(app=misconfigured), base_url="http://test"
            ) as client:
                assert (
                    await client.post("/v1/auth/otp/verify", json=verify_payload)
                ).status_code == 503
    assert await counts(database_session_factory) == (1, 1, 1, 1)


def auth_application(
    database_url: str,
    provider: DeterministicOtpProvider,
    protector: DeterministicPhoneProtector,
    *,
    address_protector: Any = None,
) -> FastAPI:
    return create_app(
        Settings(
            _env_file=None,
            environment="test",
            database_url=database_url,
            command_idempotency_ttl_seconds=3600,
            auth_access_token_ttl_seconds=ACCESS_TTL,
            auth_refresh_session_ttl_seconds=REFRESH_TTL,
        ),
        otp_provider=provider,
        phone_identity_protector=protector,
        access_token_codec=token_codec(),
        address_protector=address_protector,
    )


def add_role_test_routes(application: FastAPI) -> None:
    @application.get("/test/rider")
    async def rider_role(
        principal: Annotated[AuthenticatedPrincipal, Depends(require_role(ROLE_RIDER))],
    ) -> dict[str, str]:
        return {"user_id": str(principal.user_id)}

    @application.get("/test/manager")
    async def manager_role(
        principal: Annotated[AuthenticatedPrincipal, Depends(require_role(ROLE_MANAGER))],
    ) -> dict[str, str]:
        return {"user_id": str(principal.user_id)}


async def test_phase_1o_migration_roundtrip_preserves_existing_schema_and_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = require_test_database_url()
    monkeypatch.setenv("TIRODHAN_DATABASE_URL", database_url)
    configuration = Config("alembic.ini")
    await asyncio.to_thread(command.upgrade, configuration, "head")
    user_id = new_uuid7()

    async def seed_and_schema() -> tuple[set[str], int]:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO app_user (user_id, status, created_at, updated_at) "
                        "VALUES (:user_id, 'ACTIVE', now(), now()) "
                        "ON CONFLICT (user_id) DO NOTHING"
                    ),
                    {"user_id": user_id},
                )
            async with engine.connect() as connection:
                tables = await connection.run_sync(
                    lambda sync: set(sqlalchemy_inspect(sync).get_table_names())
                )
                count = int(
                    await connection.scalar(
                        text("SELECT count(*) FROM app_user WHERE user_id = :user_id"),
                        {"user_id": user_id},
                    )
                    or 0
                )
                return tables, count
        finally:
            await engine.dispose()

    tables, count = await seed_and_schema()
    assert {"user_phone", "user_role", "refresh_session"}.issubset(tables)
    assert count == 1
    await asyncio.to_thread(command.downgrade, configuration, "0013_media_asset_foundation")
    tables, count = await seed_and_schema()
    assert {"user_phone", "user_role", "refresh_session"}.isdisjoint(tables)
    assert {"app_user", "idempotency_record", "collection_request", "media_asset"}.issubset(tables)
    assert count == 1
    await asyncio.to_thread(command.upgrade, configuration, "0014_identity_authentication")
    tables, count = await seed_and_schema()
    assert {"user_phone", "user_role", "refresh_session"}.issubset(tables)
    assert count == 1


def require_test_database_url() -> str:
    import os

    value = os.getenv("TIRODHAN_TEST_DATABASE_URL")
    if not value:
        pytest.skip("TIRODHAN_TEST_DATABASE_URL is not configured")
    return value
