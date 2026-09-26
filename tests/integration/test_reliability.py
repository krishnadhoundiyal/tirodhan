from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.identity.models import AppUser
from tirodhan.modules.reliability.models import (
    IdempotencyRecord,
    InboxMessage,
    OutboxEvent,
)
from tirodhan.modules.reliability.primitives import (
    IDEMPOTENCY_COMPLETED,
    INBOX_PROCESSED,
    IdempotencyKeyConflictError,
    append_outbox_event,
    claim_idempotency_record,
    claim_inbox_message,
    complete_idempotency_record,
    complete_inbox_message,
    get_completed_idempotency_result,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def command_fingerprint(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


@pytest.mark.asyncio
async def test_app_user_persistence_uses_application_uuid7(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with database_session_factory() as session, session.begin():
        user = AppUser(status="TEST_ACTIVE")
        session.add(user)
        await session.flush()
        user_id = user.user_id

    async with database_session_factory() as session:
        persisted = await session.get(AppUser, user_id)

    assert persisted is not None
    assert persisted.user_id.version == 7
    assert persisted.status == "TEST_ACTIVE"
    assert persisted.created_at.tzinfo is not None
    assert persisted.updated_at.tzinfo is not None


@pytest.mark.asyncio
async def test_idempotency_replay_conflict_and_completed_result(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fingerprint = command_fingerprint("create-app-user:v1")
    resource_id = new_uuid7()
    expires_at = utc_now() + timedelta(hours=1)

    async with database_session_factory() as session, session.begin():
        created = await claim_idempotency_record(
            session,
            scope="app-user.create",
            idempotency_key="command-001",
            request_fingerprint=fingerprint,
            expires_at=expires_at,
        )
        exact_replay = await claim_idempotency_record(
            session,
            scope="app-user.create",
            idempotency_key="command-001",
            request_fingerprint=fingerprint,
            expires_at=expires_at,
        )
        completed = await complete_idempotency_record(
            session,
            created.record,
            result_resource_id=resource_id,
            result_status_code=201,
        )

        record_count = await session.scalar(select(func.count(IdempotencyRecord.scope)))

    assert created.created is True
    assert exact_replay.created is False
    assert exact_replay.record.idempotency_record_id == created.record.idempotency_record_id
    assert record_count == 1
    assert completed.status == IDEMPOTENCY_COMPLETED

    async with database_session_factory() as session, session.begin():
        completed_replay = await claim_idempotency_record(
            session,
            scope="app-user.create",
            idempotency_key="command-001",
            request_fingerprint=fingerprint,
            expires_at=expires_at,
        )
        result = get_completed_idempotency_result(completed_replay.record)

        with pytest.raises(IdempotencyKeyConflictError):
            await claim_idempotency_record(
                session,
                scope="app-user.create",
                idempotency_key="command-001",
                request_fingerprint=command_fingerprint("different-command:v1"),
                expires_at=expires_at,
            )

    assert completed_replay.created is False
    assert result is not None
    assert result.resource_id == resource_id
    assert result.status_code == 201


@pytest.mark.asyncio
async def test_concurrent_duplicate_idempotency_claims_create_one_record(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fingerprint = command_fingerprint("concurrent-command:v1")
    expires_at = utc_now() + timedelta(hours=1)

    async def claim_once() -> bool:
        async with database_session_factory() as session, session.begin():
            claim = await claim_idempotency_record(
                session,
                scope="app-user.create",
                idempotency_key="concurrent-command-001",
                request_fingerprint=fingerprint,
                expires_at=expires_at,
            )
            return claim.created

    created_results = await asyncio.gather(claim_once(), claim_once())

    async with database_session_factory() as session:
        record_count = await session.scalar(select(func.count(IdempotencyRecord.scope)))

    assert sorted(created_results) == [False, True]
    assert record_count == 1


@pytest.mark.asyncio
async def test_duplicate_inbox_identity_is_detected_safely(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    business_key = str(new_uuid7())

    async with database_session_factory() as session, session.begin():
        first_claim = await claim_inbox_message(
            session,
            consumer_name="test-consumer",
            message_id="message-001",
            message_type="app_user.created",
            business_key=business_key,
        )
        processed = await complete_inbox_message(session, first_claim.message)

    async with database_session_factory() as session, session.begin():
        duplicate_claim = await claim_inbox_message(
            session,
            consumer_name="test-consumer",
            message_id="message-001",
            message_type="app_user.created",
            business_key=business_key,
        )
        message_count = await session.scalar(select(func.count(InboxMessage.message_id)))

    assert first_claim.claimed is True
    assert processed.status == INBOX_PROCESSED
    assert duplicate_claim.claimed is False
    assert duplicate_claim.message.status == INBOX_PROCESSED
    assert message_count == 1


@pytest.mark.asyncio
async def test_duplicate_outbox_event_key_is_prevented_with_pii_free_payload(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    aggregate_id = new_uuid7()
    payload = {"user_id": str(aggregate_id)}

    async with database_session_factory() as session, session.begin():
        await append_outbox_event(
            session,
            event_key="app-user-created:test-001",
            aggregate_type="app_user",
            aggregate_id=aggregate_id,
            event_type="app_user.created",
            payload=payload,
        )

    async with database_session_factory() as session:
        with pytest.raises(IntegrityError):
            async with session.begin():
                await append_outbox_event(
                    session,
                    event_key="app-user-created:test-001",
                    aggregate_type="app_user",
                    aggregate_id=aggregate_id,
                    event_type="app_user.created",
                    payload=payload,
                )

    async with database_session_factory() as session:
        event_count = await session.scalar(select(func.count(OutboxEvent.event_key)))
        persisted_payload = await session.scalar(select(OutboxEvent.payload))

    prohibited_terms = {
        "address",
        "coordinate",
        "latitude",
        "longitude",
        "otp",
        "phone",
        "secret",
        "token",
    }
    serialized_payload = json.dumps(persisted_payload, sort_keys=True).lower()

    assert event_count == 1
    assert persisted_payload == payload
    assert not any(term in serialized_payload for term in prohibited_terms)


class ExpectedRollback(Exception):
    pass


@pytest.mark.asyncio
async def test_outbox_event_rolls_back_with_business_state_change(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user_id = new_uuid7()
    event_key = "app-user-created:rollback-test"

    with pytest.raises(ExpectedRollback):
        async with database_session_factory() as session, session.begin():
            session.add(AppUser(user_id=user_id, status="TEST_ACTIVE"))
            await session.flush()
            await append_outbox_event(
                session,
                event_key=event_key,
                aggregate_type="app_user",
                aggregate_id=user_id,
                event_type="app_user.created",
                payload={"user_id": str(user_id)},
            )
            raise ExpectedRollback

    async with database_session_factory() as session:
        user = await session.get(AppUser, user_id)
        event = await session.scalar(select(OutboxEvent).where(OutboxEvent.event_key == event_key))

    assert user is None
    assert event is None
