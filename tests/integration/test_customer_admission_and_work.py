"""Approved admission policy and incremental work invariants in real PostgreSQL."""

import asyncio
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import event, func, select, text, update
from sqlalchemy.exc import DBAPIError
from test_customer_capabilities import client_setup, config, event_for, setup

from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.customer_account import devices, native_push
from tirodhan.modules.customer_account.models import (
    CustomerNotification,
    CustomerNotificationPending,
    CustomerPushBinding,
    CustomerPushDelivery,
)
from tirodhan.modules.customer_account.notifications import materialize
from tirodhan.modules.customer_account.protection import CustomerDataProtection
from tirodhan.modules.customer_reads.errors import CustomerReadError
from tirodhan.modules.identity.models import RefreshSession
from tirodhan.modules.reliability.models import InboxMessage, OutboxEvent

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def register(factory: Any, owner: Any, device: Any, token: str, web: bool = False) -> Any:
    async with factory() as session, session.begin():
        return await devices.register(
            session,
            principal=owner,
            installation=device,
            platform="WEB" if web else "ANDROID",
            provider="WEBPUSH" if web else "FCM",
            secret=token,
            browser_origin="https://customer.example.test" if web else None,
            settings=config(),
            protection=CustomerDataProtection(config()),
        )


@pytest.mark.parametrize("web,allowance,contenders", [(False, 5, 8), (True, 10, 13)])
async def test_concurrent_distinct_devices_cannot_exceed_approved_cap(
    database_session_factory: Any, web: bool, allowance: int, contenders: int
) -> None:
    factory = database_session_factory
    _, _, owner = await setup(factory)
    outcomes = await asyncio.wait_for(
        asyncio.gather(
            *(register(factory, owner, new_uuid7(), f"token-{n}", web) for n in range(contenders)),
            return_exceptions=True,
        ),
        30,
    )
    admitted = [o for o in outcomes if not isinstance(o, Exception)]
    rejected = [o for o in outcomes if isinstance(o, CustomerReadError)]
    assert len(admitted) == allowance
    assert len(rejected) == contenders - allowance
    assert all(o.status == 429 and o.code == "DEVICE_ACTIVE_LIMIT" for o in rejected)
    async with factory() as session:
        assert (
            await session.scalar(select(func.count()).select_from(CustomerPushBinding)) == allowance
        )
    # Revocation frees capacity without consuming another registration change.
    async with factory() as session, session.begin():
        await devices.revoke(
            session,
            principal=owner,
            installation=admitted[0].client_device_id,
            platform="WEB" if web else "ANDROID",
            browser_origin="https://customer.example.test" if web else None,
        )
    await register(factory, owner, new_uuid7(), "replacement", web)


async def test_combined_rolling_quota_rotation_replay_and_rejection_have_atomic_effects(
    database_session_factory: Any,
) -> None:
    factory = database_session_factory
    _, _, owner = await setup(factory)
    native, browser = new_uuid7(), new_uuid7()
    await register(factory, owner, native, "n-0")
    await register(factory, owner, browser, "b-0", True)
    for n in range(1, 28):
        await register(factory, owner, native, f"n-{n}")
    # Twenty-nine committed changes; two sessions of the same customer share one quota.
    from test_operational_api import _token_for

    from tirodhan.modules.identity.service import AuthenticatedPrincipal

    _, session_id = await _token_for(factory, owner.user_id, roles=("CUSTOMER",))
    newer = AuthenticatedPrincipal(owner.user_id, session_id, frozenset({"CUSTOMER"}))
    outcomes = await asyncio.wait_for(
        asyncio.gather(
            register(factory, owner, native, "last-native"),
            register(factory, newer, browser, "last-browser", True),
            return_exceptions=True,
        ),
        20,
    )
    assert sum(not isinstance(o, Exception) for o in outcomes) == 1
    rejected = next(o for o in outcomes if isinstance(o, Exception))
    assert isinstance(rejected, CustomerReadError) and rejected.status == 429
    assert rejected.code == "DEVICE_CHANGE_LIMIT"
    winner = 0 if not isinstance(outcomes[0], Exception) else 1
    assert (
        await register(
            factory,
            owner if winner == 0 else newer,
            native if winner == 0 else browser,
            "last-native" if winner == 0 else "last-browser",
            winner == 1,
        )
        == outcomes[winner]
    )
    async with factory() as session:
        rows = list(await session.scalars(select(CustomerPushBinding)))
        assert len(rows) == 30 and sum(r.revoked_at is None for r in rows) == 2
    async with factory() as session, session.begin():
        await devices.revoke(
            session,
            principal=owner if winner == 0 else newer,
            installation=native if winner == 0 else browser,
            platform="ANDROID" if winner == 0 else "WEB",
            browser_origin=None if winner == 0 else "https://customer.example.test",
        )
    with pytest.raises(CustomerReadError, match="DEVICE_CHANGE_LIMIT"):
        await register(factory, newer, new_uuid7(), "after-revocation")
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(CustomerPushBinding)) == 30


async def test_expired_sessions_do_not_hold_capacity_or_amplify_fanout(
    database_session_factory: Any,
) -> None:
    factory = database_session_factory
    _, _, owner = await setup(factory)
    for n in range(5):
        await register(factory, owner, new_uuid7(), f"old-{n}")
    async with factory() as session, session.begin():
        await session.execute(
            update(RefreshSession)
            .where(RefreshSession.refresh_session_id == owner.refresh_session_id)
            .values(
                created_at=utc_now() - timedelta(days=1),
                expires_at=utc_now() - timedelta(seconds=1),
            )
        )
    await event_for(factory, owner.user_id, "expired-only")
    assert await materialize(factory) == 1
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(CustomerPushDelivery)) == 0
    from test_operational_api import _token_for

    from tirodhan.modules.identity.service import AuthenticatedPrincipal

    _, sid = await _token_for(factory, owner.user_id, roles=("CUSTOMER",))
    newer = AuthenticatedPrincipal(owner.user_id, sid, frozenset({"CUSTOMER"}))
    await register(factory, newer, new_uuid7(), "fresh")
    async with factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(CustomerPushBinding)
                .where(CustomerPushBinding.revoked_at.is_(None))
            )
            == 1
        )


async def test_pending_work_survives_late_commit_backdating_and_availability_changes(
    database_session_factory: Any,
) -> None:
    factory = database_session_factory
    _, _, owner = await setup(factory)
    source = await event_for(factory, owner.user_id, "template")
    assert await materialize(factory) == 1
    late = OutboxEvent(
        outbox_event_id=new_uuid7(),
        event_key="late-uncommitted",
        aggregate_type=source.aggregate_type,
        aggregate_id=source.aggregate_id,
        event_type=source.event_type,
        payload=source.payload,
        status="PENDING",
        created_at=utc_now() - timedelta(days=100),
        available_at=utc_now(),
    )
    async with factory() as held, held.begin():
        held.add(late)
        await held.flush()
        assert await materialize(factory) == 0
    assert await materialize(factory) == 1
    async with factory() as session, session.begin():
        future = OutboxEvent(
            outbox_event_id=new_uuid7(),
            event_key="future",
            aggregate_type=source.aggregate_type,
            aggregate_id=source.aggregate_id,
            event_type=source.event_type,
            payload=source.payload,
            status="PENDING",
            created_at=utc_now() - timedelta(days=50),
            available_at=utc_now() + timedelta(days=1),
        )
        session.add(future)
    assert await materialize(factory) == 0
    async with factory() as session, session.begin():
        await session.execute(
            update(OutboxEvent)
            .where(OutboxEvent.outbox_event_id == future.outbox_event_id)
            .values(available_at=utc_now() - timedelta(seconds=1))
        )
    assert sum(await asyncio.gather(materialize(factory), materialize(factory))) == 1
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(CustomerNotification)) == 3
        assert (
            await session.scalar(select(func.count()).select_from(CustomerNotificationPending)) == 0
        )


async def test_failed_materializer_commit_keeps_work_and_retry_is_single_effect(
    database_session_factory: Any,
) -> None:
    factory = database_session_factory
    _, _, owner = await setup(factory)
    await event_for(factory, owner.user_id, "commit-loss")

    def failing_factory() -> Any:
        session = factory()

        def fail(_: Any) -> None:
            raise RuntimeError("synthetic commit failure")

        event.listen(session.sync_session, "before_commit", fail)
        return session

    with pytest.raises(RuntimeError, match="synthetic commit failure"):
        await materialize(failing_factory)
    async with factory() as session:
        assert (
            await session.scalar(select(func.count()).select_from(CustomerNotificationPending)) == 1
        )
        assert await session.scalar(select(func.count()).select_from(CustomerNotification)) == 0
        assert await session.scalar(select(func.count()).select_from(InboxMessage)) == 0
    assert await materialize(factory) == 1 and await materialize(factory) == 0


async def test_http_exhaustion_is_sanitized_and_does_not_revoke_or_consume_quota(
    database_session_factory: Any,
    monkeypatch: Any,
) -> None:
    factory = database_session_factory
    monkeypatch.setattr(native_push, "require_fcm", lambda _: None)
    client, owner = await client_setup(factory)
    installations = [new_uuid7() for _ in range(6)]
    async with client:
        for device in installations[:5]:
            response = await client.put(
                f"/v1/customer/me/push-devices/{device}",
                json={
                    "client_device_id": str(device),
                    "platform": "ANDROID",
                    "token_provider": "FCM",
                    "token": f"private-{device}",
                },
            )
            assert response.status_code == 200
        rejected = await client.put(
            f"/v1/customer/me/push-devices/{installations[5]}",
            json={
                "client_device_id": str(installations[5]),
                "platform": "ANDROID",
                "token_provider": "FCM",
                "token": "private-rejected-token",
            },
        )
        assert rejected.status_code == 429
        assert rejected.json() == {"error": {"code": "DEVICE_ACTIVE_LIMIT"}}
    async with factory() as session:
        rows = list(
            await session.scalars(
                select(CustomerPushBinding).where(CustomerPushBinding.user_id == owner.user_id)
            )
        )
        assert len(rows) == 5 and all(r.revoked_at is None for r in rows)


async def test_mixed_native_browser_fanout_is_bounded_and_failed_registration_is_not_counted(
    database_session_factory: Any,
) -> None:
    factory = database_session_factory
    _, _, owner = await setup(factory)
    # A failure after INSERT but before COMMIT must consume no registration quota.
    with pytest.raises(RuntimeError):
        async with factory() as session, session.begin():
            await devices.register(
                session,
                principal=owner,
                installation=new_uuid7(),
                platform="ANDROID",
                provider="FCM",
                secret="rollback-token",
                settings=config(),
                protection=CustomerDataProtection(config()),
            )
            raise RuntimeError("synthetic lost commit")
    for n in range(5):
        await register(factory, owner, new_uuid7(), f"native-{n}")
    for n in range(10):
        await register(factory, owner, new_uuid7(), f"browser-{n}", True)
    await event_for(factory, owner.user_id, "bounded-fanout")
    assert await materialize(factory) == 1
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(CustomerPushBinding)) == 15
        assert await session.scalar(select(func.count()).select_from(CustomerPushDelivery)) == 15


async def test_retired_history_outside_rolling_window_does_not_consume_current_quota(
    database_session_factory: Any,
) -> None:
    factory = database_session_factory
    _, _, owner = await setup(factory)
    crypto = CustomerDataProtection(config())
    old = utc_now() - timedelta(days=2)
    async with factory() as session, session.begin():
        for n in range(30):
            installation = new_uuid7()
            session.add(
                CustomerPushBinding(
                    client_device_id=installation,
                    user_id=owner.user_id,
                    session_id=owner.refresh_session_id,
                    platform="ANDROID",
                    provider="FCM",
                    token_hmac=crypto.fingerprint("push:FCM", f"old-{n}"),
                    credential_hmac=crypto.fingerprint("push-credential:FCM", f"old-{n}"),
                    token_encrypted=crypto.encrypt(
                        f"push:{owner.user_id}:{installation}", f"old-{n}"
                    ),
                    generation=1,
                    created_at=old,
                    updated_at=old,
                    revoked_at=old,
                )
            )
    await register(factory, owner, new_uuid7(), "current-window")


async def test_migration_backfills_only_unfinished_work_and_preserves_future_availability(
    database_session_factory: Any,
) -> None:
    from alembic import command
    from alembic.config import Config

    factory = database_session_factory
    _, _, owner = await setup(factory)
    await event_for(factory, owner.user_id, "finished-before-upgrade")
    assert await materialize(factory) == 1

    # The old-version algorithm has no work table; upgrade reconstructs membership from facts.
    await asyncio.to_thread(command.downgrade, Config("alembic.ini"), "0039_customer_capabilities")
    pending = await event_for(factory, owner.user_id, "pending-before-upgrade")
    async with factory() as session, session.begin():
        await session.execute(
            update(OutboxEvent)
            .where(OutboxEvent.outbox_event_id == pending.outbox_event_id)
            .values(available_at=utc_now() + timedelta(days=1))
        )
    await asyncio.to_thread(command.upgrade, Config("alembic.ini"), "head")
    async with factory() as session:
        rows = list(await session.scalars(select(CustomerNotificationPending)))
        assert len(rows) == 1 and rows[0].source_event_id == pending.outbox_event_id
    assert await materialize(factory) == 0
    async with factory() as session, session.begin():
        await session.execute(
            update(OutboxEvent)
            .where(OutboxEvent.outbox_event_id == pending.outbox_event_id)
            .values(available_at=utc_now() - timedelta(seconds=1))
        )
    assert await materialize(factory) == 1


async def test_migration_refuses_legacy_excess_without_eviction(
    database_session_factory: Any,
) -> None:
    from alembic import command
    from alembic.config import Config

    factory = database_session_factory
    _, _, owner = await setup(factory)
    installations = [new_uuid7() for _ in range(6)]
    for n, installation in enumerate(installations[:5]):
        await register(factory, owner, installation, f"legacy-{n}")
    await asyncio.to_thread(command.downgrade, Config("alembic.ini"), "0039_customer_capabilities")
    try:
        # Simulate an additional binding admitted by pre-policy code.
        crypto = CustomerDataProtection(config())
        async with factory() as session, session.begin():
            session.add(
                CustomerPushBinding(
                    client_device_id=installations[5],
                    user_id=owner.user_id,
                    session_id=owner.refresh_session_id,
                    platform="ANDROID",
                    provider="FCM",
                    token_hmac=crypto.fingerprint("push:FCM", "legacy-6"),
                    credential_hmac=crypto.fingerprint("push-credential:FCM", "legacy-6"),
                    token_encrypted=crypto.encrypt(
                        f"push:{owner.user_id}:{installations[5]}", "legacy-6"
                    ),
                    generation=1,
                    created_at=utc_now(),
                    updated_at=utc_now(),
                )
            )
        with pytest.raises(DBAPIError, match="CUSTOMER_DEVICE_POLICY_REQUIRES_REVOCATION"):
            await asyncio.to_thread(command.upgrade, Config("alembic.ini"), "head")
        async with factory() as session:
            assert (
                await session.scalar(text("SELECT version_num FROM alembic_version"))
                == "0039_customer_capabilities"
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(CustomerPushBinding)
                    .where(CustomerPushBinding.revoked_at.is_(None))
                )
                == 6
            )
    finally:
        async with factory() as session, session.begin():
            await devices.revoke(
                session, principal=owner, installation=installations[5], platform="ANDROID"
            )
        await asyncio.to_thread(command.upgrade, Config("alembic.ini"), "head")
