import asyncio
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from test_authentication_challenge import start
from test_identity_authentication import (
    OTP_CODE,
    PHONE,
    DeterministicPhoneProtector,
    counts,
    token_codec,
)

from tirodhan.core.config import Settings
from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.main import create_app
from tirodhan.modules.identity.models import AuthenticationChallenge
from tirodhan.modules.identity.ports import (
    OtpProviderConfigurationError,
    OtpProviderUnavailableError,
    OtpStartInProgressError,
    OtpVerificationError,
)
from tirodhan.modules.identity.service import verify_otp_and_login
from tirodhan.modules.identity.twofactor import TwoFactorOtpProvider

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
REFERENCE = "private-provider-session"


def provider(client):
    return TwoFactorOtpProvider(
        api_key="private-provider-key", template_name=None, timeout_seconds=5, client=client
    )


async def test_runtime_public_challenge_and_verified_login_only(
    database_session_factory, migrated_database_url
):
    factory = database_session_factory
    calls = []
    matched = False

    def remote(request):
        assert factory.kw["bind"].pool.checkedout() == 0
        assert app.state.database_engine.pool.checkedout() == 0
        calls.append(request)
        if "VERIFY" in request.url.path:
            return httpx.Response(
                200 if matched else 400,
                json={
                    "Status": "Success" if matched else "Error",
                    "Details": "OTP Matched" if matched else "OTP Mismatch",
                },
            )
        return httpx.Response(200, json={"Status": "Success", "Details": REFERENCE})

    async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as client:
        app = create_app(
            Settings(
                _env_file=None,
                database_url=migrated_database_url,
                otp_provider="2FACTOR",
                twofactor_api_key="private-provider-key",
                twofactor_http_timeout_seconds=5,
                auth_otp_challenge_ttl_seconds=300,
                auth_access_token_ttl_seconds=300,
                auth_refresh_session_ttl_seconds=3600,
                command_idempotency_ttl_seconds=3600,
            ),
            otp_http_client=client,
            phone_identity_protector=DeterministicPhoneProtector(),
            access_token_codec=token_codec(),
        )
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as api,
        ):
            assert isinstance(app.state.otp_provider, TwoFactorOtpProvider)
            command = {"client_request_id": str(new_uuid7()), "phone": PHONE}
            response = await api.post("/v1/auth/otp/start", json=command)
            assert response.status_code == 202
            assert set(response.json()) == {"challenge_reference"}
            assert REFERENCE not in response.text and PHONE not in response.text
            assert (await api.post("/v1/auth/otp/start", json=command)).json() == response.json()
            assert len(calls) == 1
            verification = {
                **response.json(),
                "client_login_id": str(new_uuid7()),
                "code": OTP_CODE,
            }
            assert (await api.post("/v1/auth/otp/verify", json=verification)).status_code == 401
            assert await counts(factory) == (0, 0, 0, 0)
            matched = True
            response = await api.post("/v1/auth/otp/verify", json=verification)
            assert response.status_code == 200
            assert await counts(factory) == (1, 1, 1, 1)
            assert (await api.post("/v1/auth/otp/verify", json=verification)).status_code == 409
            assert len(calls) == 3
        assert not client.is_closed
    async with factory() as session:
        challenge = await session.scalar(select(AuthenticationChallenge))
        assert challenge.provider_reference == REFERENCE and challenge.provider_code == "2FACTOR"
        assert challenge.status == "CONSUMED"
        assert PHONE.encode() not in challenge.phone_encrypted


async def test_concurrent_start_reservation_prevents_duplicate_get(database_session_factory):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def remote(request):
        calls.append(request)
        entered.set()
        await release.wait()
        return httpx.Response(200, json={"Status": "Success", "Details": REFERENCE})

    async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as client:
        adapter = provider(client)
        key = new_uuid7()
        task = asyncio.create_task(start(database_session_factory, adapter, key=key))
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            with pytest.raises(OtpStartInProgressError):
                await start(database_session_factory, adapter, key=key)
        finally:
            release.set()
        challenge = await task
        assert (await start(database_session_factory, adapter, key=key)).challenge_id == (
            challenge.challenge_id
        )
    assert len(calls) == 1


@pytest.mark.parametrize("failure", ["timeout", "local_commit"])
async def test_send_ambiguity_or_local_failure_never_resends_same_key(
    database_session_factory, monkeypatch, failure
):
    calls = []

    def remote(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("sensitive URL", request=request)
        return httpx.Response(200, json={"Status": "Success", "Details": REFERENCE})

    real_flush = AsyncSession.flush

    async def fail_challenge(session, objects=None):
        if objects and any(isinstance(row, AuthenticationChallenge) for row in objects):
            raise RuntimeError("simulated commit failure")
        await real_flush(session, objects)

    async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as client:
        adapter, key = provider(client), new_uuid7()
        with monkeypatch.context() as patch:
            if failure == "local_commit":
                patch.setattr(AsyncSession, "flush", fail_challenge)
            with pytest.raises(
                OtpProviderUnavailableError if failure == "timeout" else RuntimeError
            ):
                await start(database_session_factory, adapter, key=key)
        with pytest.raises(OtpStartInProgressError):
            await start(database_session_factory, adapter, key=key)
    assert len(calls) == 1 and await counts(database_session_factory) == (0, 0, 0, 0)


@pytest.mark.parametrize("block", ["provider_switch", "local_expiry"])
async def test_local_challenge_guards_prevent_verification_http(database_session_factory, block):
    calls = []

    def remote(request):
        calls.append(request)
        return httpx.Response(200, json={"Status": "Success", "Details": REFERENCE})

    async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as client:
        adapter = provider(client)
        challenge = await start(database_session_factory, adapter)
        if block == "provider_switch":
            adapter.provider_code = "KALEYRA_VERIFY"
        else:
            async with database_session_factory() as session, session.begin():
                await session.execute(
                    update(AuthenticationChallenge)
                    .where(AuthenticationChallenge.challenge_id == challenge.challenge_id)
                    .values(
                        created_at=utc_now() - timedelta(hours=2),
                        expires_at=utc_now() - timedelta(hours=1),
                    )
                )
        with pytest.raises(
            OtpProviderConfigurationError if block == "provider_switch" else OtpVerificationError
        ):
            await verify_otp_and_login(
                database_session_factory,
                client_login_id=new_uuid7(),
                challenge_reference=challenge.challenge_id,
                code=OTP_CODE,
                otp_provider=adapter,
                token_codec=token_codec(),
                refresh_session_ttl_seconds=3600,
                access_token_ttl_seconds=300,
                idempotency_expires_at=utc_now() + timedelta(days=1),
            )
    assert len(calls) == 1 and await counts(database_session_factory) == (0, 0, 0, 0)
