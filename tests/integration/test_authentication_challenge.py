from __future__ import annotations

import asyncio
import base64
import json
import logging
from datetime import timedelta
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient, MockTransport, Response
from pydantic import SecretStr
from sqlalchemy import delete, func, inspect, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from test_identity_authentication import (
    OTP_CODE,
    PHONE,
    DeterministicOtpProvider,
    DeterministicPhoneProtector,
    auth_application,
    begin_challenge,
    counts,
    login,
    require_test_database_url,
    token_codec,
)

from tirodhan.core.config import Settings
from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.main import create_app
from tirodhan.modules.identity import service
from tirodhan.modules.identity.models import AuthenticationChallenge, UserPhone
from tirodhan.modules.identity.phone_protection import AesGcmPhoneIdentityProtector
from tirodhan.modules.identity.ports import (
    OtpAlreadyVerifiedError,
    OtpProviderUnavailableError,
    OtpRequestConflictError,
    OtpStartInProgressError,
    OtpVerificationError,
)
from tirodhan.modules.identity.service import (
    AUTH_START_SCOPE,
    AUTH_VERIFY_SCOPE,
    LoginCredentials,
    start_otp_verification,
)
from tirodhan.modules.reliability.models import IdempotencyRecord

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def start(factory, provider, *, key=None, now=None, protector=None):
    return await start_otp_verification(
        factory,
        provider=provider,
        phone_protector=protector or DeterministicPhoneProtector(),
        phone=PHONE,
        client_request_id=key or new_uuid7(),
        challenge_ttl_seconds=300,
        idempotency_expires_at=(now or utc_now()) + timedelta(days=1),
        now=now,
    )


@pytest.mark.parametrize("terminal", ["CONSUMED", "SUPERSEDED", "EXPIRED"])
async def test_start_terminal_or_expired_key_never_sends_again(database_session_factory, terminal):
    factory = database_session_factory
    provider = DeterministicOtpProvider()
    challenge = await start(factory, provider)
    if terminal == "CONSUMED":
        await login(factory, provider, DeterministicPhoneProtector(), challenge=challenge)
    elif terminal == "SUPERSEDED":
        await start(factory, provider)
    else:
        async with factory() as session, session.begin():
            await session.execute(
                update(AuthenticationChallenge)
                .where(
                    AuthenticationChallenge.challenge_id == challenge.challenge_id,
                )
                .values(
                    created_at=utc_now() - timedelta(hours=2),
                    expires_at=utc_now() - timedelta(hours=1),
                )
            )
    sends = provider.send_count
    with pytest.raises(OtpRequestConflictError):
        await start(factory, provider, key=challenge.client_request_id)
    assert provider.send_count == sends


async def test_failed_provider_start_preserves_active_challenge(database_session_factory):
    factory = database_session_factory
    challenge = await begin_challenge(factory, DeterministicOtpProvider())

    class FailingProvider(DeterministicOtpProvider):
        async def start_verification(self, *, normalized_phone):
            raise OtpProviderUnavailableError("unavailable")

    with pytest.raises(OtpProviderUnavailableError):
        await start(factory, FailingProvider())
    async with factory() as session:
        stored = await session.get(AuthenticationChallenge, challenge.challenge_id)
        assert stored.status == "ACTIVE"
        assert stored.superseded_at is None
        assert await session.scalar(select(func.count()).select_from(AuthenticationChallenge)) == 1


async def test_provider_start_success_local_failure_rolls_back_supersession(
    database_session_factory,
    monkeypatch,
):
    factory = database_session_factory
    provider = DeterministicOtpProvider()
    original = await start(factory, provider)
    failed_key = new_uuid7()
    real_flush = AsyncSession.flush

    async def fail_new_challenge(session, objects=None):
        if objects and any(
            isinstance(row, AuthenticationChallenge) and row.status == "ACTIVE" for row in objects
        ):
            raise RuntimeError("simulated new challenge failure")
        await real_flush(session, objects)

    with monkeypatch.context() as patch:
        patch.setattr(AsyncSession, "flush", fail_new_challenge)
        with pytest.raises(RuntimeError, match="simulated"):
            await start(factory, provider, key=failed_key)
    assert provider.send_count == 2  # Remote send cannot be rolled back with PostgreSQL.
    with pytest.raises(OtpStartInProgressError):
        await start(factory, provider, key=failed_key)
    assert provider.send_count == 2
    async with factory() as session:
        stored = await session.get(AuthenticationChallenge, original.challenge_id)
        assert stored.status == "ACTIVE" and stored.superseded_at is None
        assert await session.scalar(select(func.count()).select_from(AuthenticationChallenge)) == 1
        failed = await session.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.scope == AUTH_START_SCOPE,
                IdempotencyRecord.idempotency_key == str(failed_key),
            )
        )
        assert failed.status == "IN_PROGRESS" and failed.result_resource_id is None


async def test_concurrent_different_start_keys_resolve_one_active_local_challenge(
    database_session_factory,
):
    factory = database_session_factory
    both_remote_calls = asyncio.Event()

    class RacingProvider(DeterministicOtpProvider):
        async def start_verification(self, *, normalized_phone):
            result = await super().start_verification(normalized_phone=normalized_phone)
            if self.send_count == 2:
                both_remote_calls.set()
            await asyncio.wait_for(both_remote_calls.wait(), 5)
            return result

    provider = RacingProvider()
    key = new_uuid7()
    results = await asyncio.gather(
        start(factory, provider, key=key),
        start(factory, provider, key=new_uuid7()),
    )
    async with factory() as session:
        rows = list((await session.scalars(select(AuthenticationChallenge))).all())
    assert sum(row.status == "ACTIVE" for row in rows) == 1
    assert len(rows) == 2
    assert results[0].challenge_id != results[1].challenge_id
    assert provider.send_count == 2  # Different keys are separate authentication intents.
    old = next(row for row in rows if row.status == "SUPERSEDED")
    assert old.superseded_at is not None


async def test_concurrent_same_start_key_calls_generate_once_and_api_replays(
    database_session_factory,
    migrated_database_url,
):
    entered, release = asyncio.Event(), asyncio.Event()
    factory = database_session_factory

    class PausedProvider(DeterministicOtpProvider):
        async def start_verification(self, *, normalized_phone):
            assert factory.kw["bind"].pool.checkedout() == 0
            remote = await super().start_verification(normalized_phone=normalized_phone)
            entered.set()
            await asyncio.wait_for(release.wait(), 10)
            return remote

    provider = PausedProvider()
    app = auth_application(migrated_database_url, provider, DeterministicPhoneProtector())
    key = new_uuid7()
    payload = {"client_request_id": str(key), "phone": PHONE}
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            winner_task = asyncio.create_task(client.post("/v1/auth/otp/start", json=payload))
            try:
                await asyncio.wait_for(entered.wait(), 10)
                loser = await asyncio.wait_for(
                    client.post("/v1/auth/otp/start", json=payload),
                    5,
                )
                assert loser.status_code == 409
                assert loser.json() == {"detail": "OTP start is in progress"}
                changed = await client.post(
                    "/v1/auth/otp/start",
                    json=dict(
                        payload,
                        phone="+14155552671",
                    ),
                )
                assert changed.status_code == 409
                async with factory() as session:
                    record = await session.scalar(
                        select(IdempotencyRecord).where(
                            IdempotencyRecord.scope == AUTH_START_SCOPE,
                        )
                    )
                    assert record.status == "IN_PROGRESS"
                    assert record.result_resource_id is None
                    assert (
                        await session.scalar(
                            select(func.count()).select_from(AuthenticationChallenge),
                        )
                        == 0
                    )
            finally:
                release.set()
                winner = await winner_task
            assert winner.status_code == 202
            replay = await client.post("/v1/auth/otp/start", json=payload)
            assert replay.status_code == 202 and replay.json() == winner.json()
    assert provider.send_count == 1
    async with factory() as session:
        challenge = await session.scalar(select(AuthenticationChallenge))
        record = await session.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.scope == AUTH_START_SCOPE,
            )
        )
        assert record.status == "COMPLETED" and record.result_status_code == 202
        assert record.result_resource_id == challenge.challenge_id
        assert challenge.provider_reference in provider._challenges
        assert await session.scalar(select(func.count()).select_from(AuthenticationChallenge)) == 1
        assert await session.scalar(select(func.count()).select_from(IdempotencyRecord)) == 1
        assert len(record.request_fingerprint) == 32
        assert PHONE.encode() not in record.request_fingerprint
        assert challenge.phone_encrypted not in record.request_fingerprint


async def test_concurrent_unseen_same_start_key_has_one_provider_owner(database_session_factory):
    factory = database_session_factory
    entered, release = asyncio.Event(), asyncio.Event()

    class PausedProvider(DeterministicOtpProvider):
        async def start_verification(self, *, normalized_phone):
            remote = await super().start_verification(normalized_phone=normalized_phone)
            entered.set()
            await asyncio.wait_for(release.wait(), 10)
            return remote

    provider, key = PausedProvider(), new_uuid7()
    tasks = [asyncio.create_task(start(factory, provider, key=key)) for _ in range(2)]
    try:
        await asyncio.wait_for(entered.wait(), 10)
        completed, pending = await asyncio.wait(
            tasks, timeout=5, return_when=asyncio.FIRST_COMPLETED
        )
        assert len(completed) == len(pending) == 1
        assert isinstance(next(iter(completed)).exception(), OtpStartInProgressError)
    finally:
        release.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
    assert sum(isinstance(result, AuthenticationChallenge) for result in results) == 1
    assert sum(isinstance(result, OtpStartInProgressError) for result in results) == 1
    assert provider.send_count == 1
    replay = await start(factory, provider, key=key)
    winner = next(result for result in results if isinstance(result, AuthenticationChallenge))
    assert replay.challenge_id == winner.challenge_id and provider.send_count == 1


async def test_pre_reservation_challenge_replays_without_another_generate(database_session_factory):
    factory, provider = database_session_factory, DeterministicOtpProvider()
    existing = await start(factory, provider)
    # Simulate a challenge persisted by the preceding Phase 1R commit.
    async with factory() as session, session.begin():
        await session.execute(
            delete(IdempotencyRecord).where(
                IdempotencyRecord.scope == AUTH_START_SCOPE,
                IdempotencyRecord.idempotency_key == str(existing.client_request_id),
            )
        )
    replay = await start(factory, provider, key=existing.client_request_id)
    assert replay.challenge_id == existing.challenge_id and provider.send_count == 1
    async with factory() as session:
        record = await session.scalar(select(IdempotencyRecord))
        assert record.status == "COMPLETED" and record.result_resource_id == existing.challenge_id


async def test_ambiguous_generate_failure_keeps_start_key_reserved(database_session_factory):
    factory = database_session_factory
    original = await start(factory, DeterministicOtpProvider())
    key = new_uuid7()

    class TimeoutProvider(DeterministicOtpProvider):
        async def start_verification(self, *, normalized_phone):
            await super().start_verification(normalized_phone=normalized_phone)
            raise OtpProviderUnavailableError("lost Generate response")

    provider = TimeoutProvider()
    with pytest.raises(OtpProviderUnavailableError):
        await start(factory, provider, key=key)
    # Even elapsed reliability retention metadata does not permit automatic takeover.
    async with factory() as session, session.begin():
        await session.execute(
            update(IdempotencyRecord)
            .where(
                IdempotencyRecord.scope == AUTH_START_SCOPE,
                IdempotencyRecord.idempotency_key == str(key),
            )
            .values(expires_at=utc_now() - timedelta(seconds=1))
        )
    with pytest.raises(OtpStartInProgressError):
        await start(factory, provider, key=key)
    assert provider.send_count == 1
    async with factory() as session:
        record = await session.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.scope == AUTH_START_SCOPE,
                IdempotencyRecord.idempotency_key == str(key),
            )
        )
        assert record.status == "IN_PROGRESS" and record.result_resource_id is None
        old = await session.get(AuthenticationChallenge, original.challenge_id)
        assert old.status == "ACTIVE" and old.superseded_at is None
    replacement = await start(factory, DeterministicOtpProvider(), key=new_uuid7())
    assert replacement.challenge_id != original.challenge_id


async def test_known_pre_invocation_failure_rolls_back_start_claim(database_session_factory):
    factory = database_session_factory
    key, provider = new_uuid7(), DeterministicOtpProvider()

    class BrokenProtector(DeterministicPhoneProtector):
        async def protect(self, normalized_phone):
            raise RuntimeError("local phone protection failure")

    with pytest.raises(RuntimeError, match="local phone protection"):
        await start(factory, provider, key=key, protector=BrokenProtector())
    assert provider.send_count == 0
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(IdempotencyRecord)) == 0
    challenge = await start(factory, provider, key=key)
    assert challenge.client_request_id == key and provider.send_count == 1


@pytest.mark.parametrize("unusable", ["MISSING", "CONSUMED", "SUPERSEDED", "EXPIRED"])
async def test_unusable_challenge_never_calls_provider(database_session_factory, unusable):
    factory = database_session_factory
    provider = DeterministicOtpProvider()
    challenge = await start(factory, provider)
    if unusable == "MISSING":
        challenge.challenge_id = new_uuid7()  # Detached test input, not a persistence mutation.
    elif unusable == "CONSUMED":
        await login(factory, provider, DeterministicPhoneProtector(), challenge=challenge)
    elif unusable == "SUPERSEDED":
        await start(factory, provider)
    else:
        async with factory() as session, session.begin():
            await session.execute(
                update(AuthenticationChallenge)
                .where(
                    AuthenticationChallenge.challenge_id == challenge.challenge_id,
                )
                .values(
                    created_at=utc_now() - timedelta(hours=2),
                    expires_at=utc_now() - timedelta(hours=1),
                )
            )
    calls = provider.verify_count
    with pytest.raises(OtpVerificationError):
        await login(factory, provider, DeterministicPhoneProtector(), challenge=challenge)
    assert provider.verify_count == calls


async def test_supersession_during_provider_verify_cannot_commit_login(database_session_factory):
    factory = database_session_factory
    provider = DeterministicOtpProvider()
    old = await start(factory, provider)
    entered, release = asyncio.Event(), asyncio.Event()

    async def pause():
        entered.set()
        await asyncio.wait_for(release.wait(), 5)

    provider.before_verify_return = pause
    task = asyncio.create_task(
        login(factory, provider, DeterministicPhoneProtector(), challenge=old)
    )
    try:
        await asyncio.wait_for(entered.wait(), 5)
        replacement = await start(factory, provider)
    finally:
        release.set()
    with pytest.raises(OtpVerificationError):
        await task
    async with factory() as session:
        old_row = await session.get(AuthenticationChallenge, old.challenge_id)
        new_row = await session.get(AuthenticationChallenge, replacement.challenge_id)
        assert old_row.status == "SUPERSEDED" and old_row.consumed_at is None
        assert new_row.status == "ACTIVE"
        assert (
            await session.scalar(
                select(func.count())
                .select_from(IdempotencyRecord)
                .where(
                    IdempotencyRecord.scope == AUTH_VERIFY_SCOPE,
                )
            )
            == 0
        )
    assert await counts(factory) == (0, 0, 0, 0)


async def test_different_login_commands_consume_one_challenge_once(database_session_factory):
    factory = database_session_factory
    provider = DeterministicOtpProvider()
    challenge = await start(factory, provider)
    both_verified = asyncio.Event()

    async def barrier():
        if provider.verify_count == 2:
            both_verified.set()
        await asyncio.wait_for(both_verified.wait(), 5)

    provider.before_verify_return = barrier
    results = await asyncio.gather(
        *(
            login(factory, provider, DeterministicPhoneProtector(), challenge=challenge)
            for _ in range(2)
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, LoginCredentials) for result in results) == 1
    assert sum(isinstance(result, OtpVerificationError) for result in results) == 1
    assert await counts(factory) == (1, 1, 1, 1)
    async with factory() as session:
        stored = await session.get(AuthenticationChallenge, challenge.challenge_id)
        assert stored.status == "CONSUMED"
        assert stored.consumed_at is not None and stored.superseded_at is None


async def test_local_expiry_revalidated_after_provider_returns(database_session_factory):
    factory = database_session_factory
    provider = DeterministicOtpProvider()
    challenge = await start(factory, provider)

    async def expire():
        async with factory() as session, session.begin():
            await session.execute(
                update(AuthenticationChallenge)
                .where(
                    AuthenticationChallenge.challenge_id == challenge.challenge_id,
                )
                .values(
                    created_at=utc_now() - timedelta(hours=2),
                    expires_at=utc_now() - timedelta(hours=1),
                )
            )

    provider.before_verify_return = expire
    with pytest.raises(OtpVerificationError):
        # Use the real clock; login test helper normally fixes now for TTL regression tests.
        await service.verify_otp_and_login(
            factory,
            client_login_id=new_uuid7(),
            challenge_reference=challenge.challenge_id,
            code=OTP_CODE,
            otp_provider=provider,
            token_codec=token_codec(),
            refresh_session_ttl_seconds=3600,
            access_token_ttl_seconds=300,
            idempotency_expires_at=utc_now() + timedelta(days=1),
        )
    assert await counts(factory) == (0, 0, 0, 0)


async def test_provider_success_local_rollback_is_not_committed_login(
    database_session_factory,
    monkeypatch,
):
    factory = database_session_factory

    class SingleUseProvider(DeterministicOtpProvider):
        async def verify(self, *, provider_reference, code):
            if self.verify_count:
                raise OtpAlreadyVerifiedError("already verified")
            await super().verify(provider_reference=provider_reference, code=code)

    provider = SingleUseProvider()
    challenge = await start(factory, provider)

    async def fail_commit(*args, **kwargs):
        raise RuntimeError("simulated local persistence failure")

    with monkeypatch.context() as patch:
        patch.setattr(service, "complete_idempotency_record", fail_commit)
        with pytest.raises(RuntimeError, match="simulated"):
            await login(factory, provider, DeterministicPhoneProtector(), challenge=challenge)
    async with factory() as session:
        stored = await session.get(AuthenticationChallenge, challenge.challenge_id)
        assert stored.status == "ACTIVE" and stored.consumed_at is None
        assert (
            await session.scalar(
                select(func.count())
                .select_from(IdempotencyRecord)
                .where(
                    IdempotencyRecord.scope == AUTH_VERIFY_SCOPE,
                )
            )
            == 0
        )
    assert await counts(factory) == (0, 0, 0, 0)
    with pytest.raises(OtpAlreadyVerifiedError):
        await login(factory, provider, DeterministicPhoneProtector(), challenge=challenge)
    assert await counts(factory) == (0, 0, 0, 0)


async def test_provider_calls_do_not_hold_database_connections(database_session_factory):
    factory = database_session_factory
    engine = factory.kw["bind"]

    class ConnectionCheckingProvider(DeterministicOtpProvider):
        async def start_verification(self, *, normalized_phone):
            assert engine.pool.checkedout() == 0
            return await super().start_verification(normalized_phone=normalized_phone)

        async def verify(self, *, provider_reference, code):
            assert engine.pool.checkedout() == 0
            await super().verify(provider_reference=provider_reference, code=code)

    provider = ConnectionCheckingProvider()
    challenge = await start(factory, provider)
    await login(factory, provider, DeterministicPhoneProtector(), challenge=challenge)


async def test_challenge_constraints_are_postgresql_authoritative(database_session_factory):
    factory = database_session_factory
    challenge = await start(factory, DeterministicOtpProvider())
    values = {
        column.name: getattr(challenge, column.name)
        for column in AuthenticationChallenge.__table__.columns
    }
    for invalid in (
        {"client_request_id": challenge.client_request_id},
        {"provider_reference": challenge.provider_reference},
        {"phone_lookup_hmac": challenge.phone_lookup_hmac},
        {"expires_at": challenge.created_at},
        {"status": "EXPIRED"},
        {"status": "CONSUMED", "consumed_at": None},
        {"status": "SUPERSEDED", "superseded_at": None},
        {"consumed_at": utc_now()},
    ):
        candidate = dict(
            values,
            challenge_id=new_uuid7(),
            client_request_id=new_uuid7(),
            provider_reference=str(new_uuid7()),
            phone_lookup_hmac=b"other-hmac",
        )
        candidate.update(invalid)
        with pytest.raises(IntegrityError):
            async with factory() as session, session.begin():
                await session.execute(
                    AuthenticationChallenge.__table__.insert().values(**candidate)
                )


async def test_api_contract_and_protected_persistence(
    database_session_factory, migrated_database_url
):
    factory = database_session_factory
    provider = DeterministicOtpProvider()
    protector = AesGcmPhoneIdentityProtector(
        active_key_id="test",
        encryption_keys={"test": b"a" * 32},
        lookup_hmac_key=b"h" * 32,
    )
    application = auth_application(migrated_database_url, provider, protector)
    command_id = new_uuid7()
    async with application.router.lifespan_context(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://test"
        ) as client:
            payload = {"client_request_id": str(command_id), "phone": PHONE}
            first = await client.post("/v1/auth/otp/start", json=payload)
            replay = await client.post("/v1/auth/otp/start", json=payload)
            assert first.status_code == replay.status_code == 202
            assert first.json() == replay.json()
            assert set(first.json()) == {"challenge_reference"}
            public_id = UUID(first.json()["challenge_reference"])
            assert public_id.version == 7
            verify_payload = {
                "client_login_id": str(new_uuid7()),
                "challenge_reference": str(public_id),
                "code": OTP_CODE,
            }
            extra = await client.post("/v1/auth/otp/verify", json=dict(verify_payload, phone=PHONE))
            assert extra.status_code == 422 and provider.verify_count == 0
            verified = await client.post("/v1/auth/otp/verify", json=verify_payload)
            assert verified.status_code == 200
            assert "provider" not in verified.text
    assert provider.send_count == provider.verify_count == 1
    async with factory() as session:
        challenge = await session.get(AuthenticationChallenge, public_id)
        phone = await session.scalar(select(UserPhone))
        record = await session.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.scope == AUTH_VERIFY_SCOPE,
            )
        )
        assert phone.phone_encrypted == challenge.phone_encrypted
        assert await protector.unprotect(phone.phone_encrypted) == PHONE
        assert challenge.provider_reference != str(public_id)
        assert set(AuthenticationChallenge.__table__.columns.keys()) == {
            "challenge_id",
            "client_request_id",
            "phone_encrypted",
            "phone_lookup_hmac",
            "provider_code",
            "provider_reference",
            "status",
            "expires_at",
            "created_at",
            "consumed_at",
            "superseded_at",
        }
        for row in (challenge, phone, record):
            for column in row.__table__.columns:
                value = getattr(row, column.name)
                if isinstance(value, bytes):
                    assert PHONE.encode() not in value
                    assert OTP_CODE.encode() not in value
                elif isinstance(value, (str, dict)):
                    assert PHONE not in str(value) and OTP_CODE not in str(value)


async def test_configured_runtime_login_uses_kaleyra_and_encrypted_phone(
    database_session_factory,
    migrated_database_url,
    caplog,
):
    calls = []

    def respond(request):
        calls.append(request)
        return Response(200, json={"data": {"verify_id": "private-provider-reference"}})

    async with AsyncClient(transport=MockTransport(respond)) as runtime:
        application = create_app(
            Settings(
                _env_file=None,
                environment="test",
                database_url=migrated_database_url,
                auth_otp_challenge_ttl_seconds=300,
                auth_access_token_ttl_seconds=300,
                auth_refresh_session_ttl_seconds=3600,
                command_idempotency_ttl_seconds=3600,
                kaleyra_api_domain="https://kaleyra.test",
                kaleyra_sid="test-sid",
                kaleyra_api_key=SecretStr("private-test-api-key"),
                kaleyra_verify_flow_id="test-flow",
                kaleyra_http_timeout_seconds=2,
                phone_encryption_active_key_id="test",
                phone_encryption_keys=SecretStr(
                    json.dumps(
                        {
                            "test": base64.b64encode(b"a" * 32).decode(),
                        }
                    )
                ),
                phone_lookup_hmac_key=SecretStr(base64.b64encode(b"h" * 32).decode()),
            ),
            otp_http_client=runtime,
            access_token_codec=token_codec(),
        )
        # create_app deliberately replaces log handlers; attach capture after configuration.
        logging.getLogger().addHandler(caplog.handler)
        caplog.set_level(logging.INFO)
        async with application.router.lifespan_context(application):
            async with AsyncClient(
                transport=ASGITransport(app=application), base_url="http://test"
            ) as client:
                started = await client.post(
                    "/v1/auth/otp/start",
                    json={
                        "client_request_id": str(new_uuid7()),
                        "phone": PHONE,
                    },
                )
                assert started.status_code == 202
                assert "private-provider-reference" not in started.text
                verified = await client.post(
                    "/v1/auth/otp/verify",
                    json={
                        "client_login_id": str(new_uuid7()),
                        "challenge_reference": started.json()["challenge_reference"],
                        "code": OTP_CODE,
                    },
                )
                assert verified.status_code == 200
        assert not runtime.is_closed
    assert len(calls) == 2
    assert json.loads(calls[1].content) == {
        "verify_id": "private-provider-reference",
        "otp": OTP_CODE,
    }
    async with database_session_factory() as session:
        challenge = await session.scalar(select(AuthenticationChallenge))
        phone = await session.scalar(select(UserPhone))
        assert challenge.provider_code == "KALEYRA_VERIFY" and challenge.status == "CONSUMED"
        assert phone.phone_encrypted == challenge.phone_encrypted
        assert PHONE.encode() not in phone.phone_encrypted
    for sensitive in (
        PHONE,
        OTP_CODE,
        "private-provider-reference",
        "private-test-api-key",
        verified.json()["refresh_token"],
        verified.json()["access_token"],
    ):
        assert sensitive not in caplog.text


async def test_authentication_challenge_migration_roundtrip(monkeypatch):
    url = require_test_database_url()
    monkeypatch.setenv("TIRODHAN_DATABASE_URL", url)
    configuration = Config("alembic.ini")
    engine = create_async_engine(url)

    async def tables():
        async with engine.connect() as connection:
            return await connection.run_sync(lambda sync: set(inspect(sync).get_table_names()))

    try:
        await asyncio.to_thread(command.upgrade, configuration, "0015_authentication_challenge")
        assert "authentication_challenge" in await tables()
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO authentication_challenge VALUES "
                    "(:id, :key, :encrypted, :hmac, 'TEST', 'migration-reference', 'ACTIVE', "
                    "now() + interval '5 minutes', now(), NULL, NULL)"
                ),
                {
                    "id": new_uuid7(),
                    "key": new_uuid7(),
                    "encrypted": b"encrypted-test",
                    "hmac": b"test-hmac",
                },
            )
        await asyncio.to_thread(command.downgrade, configuration, "0014_identity_authentication")
        remaining = await tables()
        assert "authentication_challenge" not in remaining
        assert {"user_phone", "refresh_session", "media_asset"}.issubset(remaining)
        await asyncio.to_thread(command.upgrade, configuration, "0015_authentication_challenge")
        assert "authentication_challenge" in await tables()
    finally:
        await asyncio.to_thread(command.upgrade, configuration, "head")
        await engine.dispose()
