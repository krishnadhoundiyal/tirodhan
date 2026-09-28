from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_evidence_capture import (
    CAPTURED_AT,
    capture,
    create_handover_fixture,
    pickup_state,
)
from test_handover import assign_and_collect

from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.evidence.media_policy import ConfiguredMediaPolicy
from tirodhan.modules.evidence.media_ports import (
    StoredObjectProperties,
    UploadAuthorization,
)
from tirodhan.modules.evidence.media_service import (
    MEDIA_PHOTO,
    MEDIA_REGISTER_IDEMPOTENCY_SCOPE,
    MEDIA_VIDEO,
    UPLOAD_FINALIZED,
    UPLOAD_PENDING,
    InvalidMediaTypeError,
    MediaActorAttributionError,
    MediaAlreadyRegisteredError,
    MediaAssetNotFoundError,
    MediaContentTypePolicyError,
    MediaEvidenceNotFoundError,
    MediaObjectNotReadyError,
    MediaSizePolicyError,
    MediaWriteAuthorizationUnavailableError,
    finalize_media_asset,
    register_media_asset,
    request_upload_authorization,
)
from tirodhan.modules.evidence.models import (
    EvidenceCapture,
    HandoverEvidenceLink,
    MediaAsset,
    PickupEvidenceLink,
)
from tirodhan.modules.handovers.models import HandoverEvent
from tirodhan.modules.reliability.models import IdempotencyRecord, OutboxEvent
from tirodhan.modules.reliability.primitives import IdempotencyKeyConflictError

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

REGISTERED_AT = datetime(2026, 9, 28, 14, 0, tzinfo=timezone.utc)
FINALIZED_AT = datetime(2026, 9, 28, 14, 5, tzinfo=timezone.utc)


def media_policy() -> ConfiguredMediaPolicy:
    return ConfiguredMediaPolicy(
        allowed_content_types={
            MEDIA_PHOTO: {"image/test-photo"},
            MEDIA_VIDEO: {"video/test-video"},
        },
        maximum_size_bytes={MEDIA_PHOTO: 1_000, MEDIA_VIDEO: 2_000},
    )


def future_expiry() -> datetime:
    return utc_now() + timedelta(days=1)


class FakeMediaStorage:
    def __init__(self) -> None:
        self.objects: dict[str, StoredObjectProperties] = {}
        self.authorization_calls: list[tuple[str, str]] = []
        self.inspection_calls: list[str] = []
        self.authorization_error: Exception | None = None
        self.inspect_callback: object | None = None

    async def create_upload_authorization(
        self,
        *,
        object_key: str,
        expected_content_type: str,
    ) -> UploadAuthorization:
        self.authorization_calls.append((object_key, expected_content_type))
        if self.authorization_error is not None:
            raise self.authorization_error
        return UploadAuthorization(
            opaque_value=f"secret-authorization-{len(self.authorization_calls)}",
            expires_at=FINALIZED_AT + timedelta(minutes=5),
        )

    async def inspect_object(self, *, object_key: str) -> StoredObjectProperties | None:
        self.inspection_calls.append(object_key)
        callback = self.inspect_callback
        if callback is not None:
            await callback()  # type: ignore[operator]
        return self.objects.get(object_key)


async def create_pickup_capture(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[object, object, EvidenceCapture]:
    fixture, assignment = await assign_and_collect(factory, pickup_count=1)
    evidence = await capture(
        factory,
        actor_id=fixture.rider_ids[0],
        target_kind="PICKUP",
        target_id=fixture.pickup_ids[0][0],
    )
    return fixture, assignment, evidence


async def register(
    factory: async_sessionmaker[AsyncSession],
    *,
    evidence: EvidenceCapture,
    actor_id: UUID | None = None,
    client_media_id: UUID | None = None,
    media_type: str = MEDIA_PHOTO,
    expected_content_type: str = "image/test-photo",
    now: datetime | None = REGISTERED_AT,
    policy: ConfiguredMediaPolicy | None = None,
) -> MediaAsset:
    return await register_media_asset(
        factory,
        client_media_id=client_media_id or new_uuid7(),
        evidence_capture_id=evidence.evidence_capture_id,
        requesting_user_id=actor_id or evidence.captured_by_user_id,
        media_type=media_type,
        expected_content_type=expected_content_type,
        policy=policy or media_policy(),
        idempotency_expires_at=future_expiry(),
        now=now,
    )


async def asset_state(
    factory: async_sessionmaker[AsyncSession], media_asset_id: UUID
) -> tuple[object, ...]:
    async with factory() as session:
        asset = await session.get(MediaAsset, media_asset_id)
        assert asset is not None
        return (
            asset.media_asset_id,
            asset.client_media_id,
            asset.evidence_capture_id,
            asset.media_type,
            asset.object_key,
            asset.expected_content_type,
            asset.stored_content_type,
            asset.size_bytes,
            asset.upload_status,
            asset.created_at,
            asset.finalized_at,
        )


async def test_registration_persists_pending_asset_with_stable_opaque_object_key(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _fixture, _assignment, evidence = await create_pickup_capture(database_session_factory)
    asset = await register(database_session_factory, evidence=evidence)

    assert asset.object_key == f"media/{asset.media_asset_id}"
    assert asset.upload_status == UPLOAD_PENDING
    assert asset.created_at == REGISTERED_AT
    assert asset.stored_content_type is None
    assert asset.size_bytes is None
    assert asset.finalized_at is None
    assert str(evidence.captured_by_user_id) not in asset.object_key


async def test_registration_rejects_missing_evidence_wrong_actor_and_policy_errors(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture, _assignment, evidence = await create_pickup_capture(database_session_factory)
    with pytest.raises(MediaEvidenceNotFoundError):
        await register_media_asset(
            database_session_factory,
            client_media_id=new_uuid7(),
            evidence_capture_id=new_uuid7(),
            requesting_user_id=evidence.captured_by_user_id,
            media_type=MEDIA_PHOTO,
            expected_content_type="image/test-photo",
            policy=media_policy(),
            idempotency_expires_at=future_expiry(),
        )
    with pytest.raises(MediaActorAttributionError):
        await register(
            database_session_factory,
            evidence=evidence,
            actor_id=fixture.rider_ids[1],
        )
    with pytest.raises(InvalidMediaTypeError):
        await register(
            database_session_factory,
            evidence=evidence,
            media_type="AUDIO",
        )
    with pytest.raises(MediaContentTypePolicyError):
        await register(
            database_session_factory,
            evidence=evidence,
            expected_content_type="image/not-allowed",
        )

    async with database_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(MediaAsset)) == 0


async def test_exact_registration_replay_returns_same_asset_without_revalidation(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _fixture, _assignment, evidence = await create_pickup_capture(database_session_factory)
    client_media_id = new_uuid7()
    first = await register(
        database_session_factory,
        evidence=evidence,
        client_media_id=client_media_id,
    )
    storage = FakeMediaStorage()
    storage.objects[first.object_key] = StoredObjectProperties("image/test-photo", 500)
    finalized = await finalize_media_asset(
        database_session_factory,
        media_asset_id=first.media_asset_id,
        requesting_user_id=evidence.captured_by_user_id,
        storage=storage,
        policy=media_policy(),
        now=FINALIZED_AT,
    )

    class ExplodingPolicy:
        def is_content_type_allowed(self, media_type: str, content_type: str) -> bool:
            raise AssertionError("exact replay must not revalidate policy")

        def max_size_bytes(self, media_type: str) -> int:
            raise AssertionError("exact replay must not revalidate policy")

    replay = await register_media_asset(
        database_session_factory,
        client_media_id=client_media_id,
        evidence_capture_id=evidence.evidence_capture_id,
        requesting_user_id=evidence.captured_by_user_id,
        media_type=MEDIA_PHOTO,
        expected_content_type="image/test-photo",
        policy=ExplodingPolicy(),
        idempotency_expires_at=future_expiry(),
        now=REGISTERED_AT + timedelta(hours=1),
    )

    assert replay.media_asset_id == finalized.media_asset_id
    assert replay.object_key == finalized.object_key
    assert replay.upload_status == UPLOAD_FINALIZED
    assert replay.finalized_at == FINALIZED_AT


async def test_registration_fingerprint_conflicts_on_each_changed_field(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture, _assignment, first_evidence = await create_pickup_capture(database_session_factory)
    second_evidence = await capture(
        database_session_factory,
        actor_id=fixture.rider_ids[0],
        target_kind="PICKUP",
        target_id=fixture.pickup_ids[0][0],
        captured_at=CAPTURED_AT + timedelta(seconds=1),
    )
    client_media_id = new_uuid7()
    await register(
        database_session_factory,
        evidence=first_evidence,
        client_media_id=client_media_id,
    )
    changes = (
        (second_evidence, fixture.rider_ids[0], MEDIA_PHOTO, "image/test-photo"),
        (first_evidence, fixture.rider_ids[1], MEDIA_PHOTO, "image/test-photo"),
        (first_evidence, fixture.rider_ids[0], MEDIA_VIDEO, "image/test-photo"),
        (first_evidence, fixture.rider_ids[0], MEDIA_PHOTO, "image/changed"),
    )
    for evidence, actor_id, media_type, content_type in changes:
        with pytest.raises(IdempotencyKeyConflictError):
            await register(
                database_session_factory,
                evidence=evidence,
                actor_id=actor_id,
                client_media_id=client_media_id,
                media_type=media_type,
                expected_content_type=content_type,
            )


async def test_concurrent_exact_registration_converges_on_one_asset(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _fixture, _assignment, evidence = await create_pickup_capture(database_session_factory)
    client_media_id = new_uuid7()
    results = await asyncio.gather(
        *(
            register(
                database_session_factory,
                evidence=evidence,
                client_media_id=client_media_id,
            )
            for _ in range(2)
        )
    )

    async with database_session_factory() as session:
        asset_count = await session.scalar(select(func.count()).select_from(MediaAsset))
        command_count = await session.scalar(
            select(func.count())
            .select_from(IdempotencyRecord)
            .where(
                IdempotencyRecord.scope == MEDIA_REGISTER_IDEMPOTENCY_SCOPE,
                IdempotencyRecord.status == "COMPLETED",
            )
        )
    assert results[0].media_asset_id == results[1].media_asset_id
    assert results[0].object_key == results[1].object_key
    assert (asset_count, command_count) == (1, 1)


async def test_second_logical_media_asset_for_same_capture_is_rejected(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _fixture, _assignment, evidence = await create_pickup_capture(database_session_factory)
    established = await register(database_session_factory, evidence=evidence)
    with pytest.raises(MediaAlreadyRegisteredError):
        await register(database_session_factory, evidence=evidence)

    async with database_session_factory() as session:
        assets = (await session.scalars(select(MediaAsset))).all()
        commands = (
            await session.scalars(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.scope == MEDIA_REGISTER_IDEMPOTENCY_SCOPE
                )
            )
        ).all()
    assert [item.media_asset_id for item in assets] == [established.media_asset_id]
    assert len(commands) == 1


async def test_pending_authorization_is_ephemeral_retryable_and_actor_scoped(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture, _assignment, evidence = await create_pickup_capture(database_session_factory)
    asset = await register(database_session_factory, evidence=evidence)
    storage = FakeMediaStorage()
    before = await asset_state(database_session_factory, asset.media_asset_id)

    first = await request_upload_authorization(
        database_session_factory,
        media_asset_id=asset.media_asset_id,
        requesting_user_id=evidence.captured_by_user_id,
        storage=storage,
    )
    second = await request_upload_authorization(
        database_session_factory,
        media_asset_id=asset.media_asset_id,
        requesting_user_id=evidence.captured_by_user_id,
        storage=storage,
    )
    with pytest.raises(MediaActorAttributionError):
        await request_upload_authorization(
            database_session_factory,
            media_asset_id=asset.media_asset_id,
            requesting_user_id=fixture.rider_ids[1],
            storage=storage,
        )

    assert first.opaque_value != second.opaque_value
    assert storage.authorization_calls == [
        (asset.object_key, asset.expected_content_type),
        (asset.object_key, asset.expected_content_type),
    ]
    assert await asset_state(database_session_factory, asset.media_asset_id) == before
    assert "secret-authorization" not in repr(before)


async def test_authorization_failure_leaves_pending_asset_unchanged(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _fixture, _assignment, evidence = await create_pickup_capture(database_session_factory)
    asset = await register(database_session_factory, evidence=evidence)
    storage = FakeMediaStorage()
    storage.authorization_error = RuntimeError("authorization unavailable")
    before = await asset_state(database_session_factory, asset.media_asset_id)

    with pytest.raises(RuntimeError, match="authorization unavailable"):
        await request_upload_authorization(
            database_session_factory,
            media_asset_id=asset.media_asset_id,
            requesting_user_id=evidence.captured_by_user_id,
            storage=storage,
        )
    assert await asset_state(database_session_factory, asset.media_asset_id) == before


async def test_finalize_missing_or_invalid_storage_metadata_keeps_asset_pending(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _fixture, _assignment, evidence = await create_pickup_capture(database_session_factory)
    asset = await register(database_session_factory, evidence=evidence)
    storage = FakeMediaStorage()
    before = await asset_state(database_session_factory, asset.media_asset_id)

    with pytest.raises(MediaObjectNotReadyError):
        await finalize_media_asset(
            database_session_factory,
            media_asset_id=asset.media_asset_id,
            requesting_user_id=evidence.captured_by_user_id,
            storage=storage,
            policy=media_policy(),
        )
    storage.objects[asset.object_key] = StoredObjectProperties("image/wrong", 500)
    with pytest.raises(MediaContentTypePolicyError):
        await finalize_media_asset(
            database_session_factory,
            media_asset_id=asset.media_asset_id,
            requesting_user_id=evidence.captured_by_user_id,
            storage=storage,
            policy=media_policy(),
        )
    storage.objects[asset.object_key] = StoredObjectProperties("image/test-photo", 1_001)
    with pytest.raises(MediaSizePolicyError):
        await finalize_media_asset(
            database_session_factory,
            media_asset_id=asset.media_asset_id,
            requesting_user_id=evidence.captured_by_user_id,
            storage=storage,
            policy=media_policy(),
        )
    assert await asset_state(database_session_factory, asset.media_asset_id) == before


async def test_valid_finalize_is_terminal_replay_without_second_inspection_or_authorization(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _fixture, _assignment, evidence = await create_pickup_capture(database_session_factory)
    asset = await register(database_session_factory, evidence=evidence)
    storage = FakeMediaStorage()
    storage.objects[asset.object_key] = StoredObjectProperties("image/test-photo", 777)

    first = await finalize_media_asset(
        database_session_factory,
        media_asset_id=asset.media_asset_id,
        requesting_user_id=evidence.captured_by_user_id,
        storage=storage,
        policy=media_policy(),
        now=FINALIZED_AT,
    )
    replay = await finalize_media_asset(
        database_session_factory,
        media_asset_id=asset.media_asset_id,
        requesting_user_id=evidence.captured_by_user_id,
        storage=storage,
        policy=media_policy(),
        now=FINALIZED_AT + timedelta(hours=1),
    )

    assert first.media_asset_id == replay.media_asset_id
    assert replay.upload_status == UPLOAD_FINALIZED
    assert replay.stored_content_type == "image/test-photo"
    assert replay.size_bytes == 777
    assert replay.finalized_at == FINALIZED_AT
    assert storage.inspection_calls == [asset.object_key]
    with pytest.raises(MediaWriteAuthorizationUnavailableError):
        await request_upload_authorization(
            database_session_factory,
            media_asset_id=asset.media_asset_id,
            requesting_user_id=evidence.captured_by_user_id,
            storage=storage,
        )


async def test_finalize_requires_owner_and_existing_asset(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture, _assignment, evidence = await create_pickup_capture(database_session_factory)
    asset = await register(database_session_factory, evidence=evidence)
    storage = FakeMediaStorage()
    with pytest.raises(MediaActorAttributionError):
        await finalize_media_asset(
            database_session_factory,
            media_asset_id=asset.media_asset_id,
            requesting_user_id=fixture.rider_ids[1],
            storage=storage,
            policy=media_policy(),
        )
    with pytest.raises(MediaAssetNotFoundError):
        await finalize_media_asset(
            database_session_factory,
            media_asset_id=new_uuid7(),
            requesting_user_id=evidence.captured_by_user_id,
            storage=storage,
            policy=media_policy(),
        )
    assert storage.inspection_calls == []


async def test_storage_inspection_occurs_without_media_row_lock(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _fixture, _assignment, evidence = await create_pickup_capture(database_session_factory)
    asset = await register(database_session_factory, evidence=evidence)
    storage = FakeMediaStorage()
    storage.objects[asset.object_key] = StoredObjectProperties("image/test-photo", 500)

    async def acquire_same_row_lock() -> None:
        async with database_session_factory() as session, session.begin():
            locked = await session.scalar(
                select(MediaAsset)
                .where(MediaAsset.media_asset_id == asset.media_asset_id)
                .with_for_update()
            )
            assert locked is not None

    storage.inspect_callback = acquire_same_row_lock
    finalized = await asyncio.wait_for(
        finalize_media_asset(
            database_session_factory,
            media_asset_id=asset.media_asset_id,
            requesting_user_id=evidence.captured_by_user_id,
            storage=storage,
            policy=media_policy(),
        ),
        timeout=2,
    )
    assert finalized.upload_status == UPLOAD_FINALIZED


async def test_concurrent_finalize_calls_converge_on_one_terminal_transition(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _fixture, _assignment, evidence = await create_pickup_capture(database_session_factory)
    asset = await register(database_session_factory, evidence=evidence)
    storage = FakeMediaStorage()
    storage.objects[asset.object_key] = StoredObjectProperties("image/test-photo", 500)

    results = await asyncio.gather(
        *(
            finalize_media_asset(
                database_session_factory,
                media_asset_id=asset.media_asset_id,
                requesting_user_id=evidence.captured_by_user_id,
                storage=storage,
                policy=media_policy(),
                now=FINALIZED_AT + timedelta(seconds=index),
            )
            for index in range(2)
        )
    )

    async with database_session_factory() as session:
        assets = (await session.scalars(select(MediaAsset))).all()
    assert 1 <= len(storage.inspection_calls) <= 2
    assert len(assets) == 1
    assert results[0].media_asset_id == results[1].media_asset_id
    assert results[0].finalized_at == results[1].finalized_at == assets[0].finalized_at
    assert assets[0].upload_status == UPLOAD_FINALIZED


async def test_late_database_failure_leaves_pending_asset_retryable(
    database_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fixture, _assignment, evidence = await create_pickup_capture(database_session_factory)
    asset = await register(database_session_factory, evidence=evidence)
    storage = FakeMediaStorage()
    storage.objects[asset.object_key] = StoredObjectProperties("image/test-photo", 500)
    original_flush = AsyncSession.flush
    fail_once = True

    async def fail_media_flush(
        session: AsyncSession,
        objects: object | None = None,
    ) -> None:
        nonlocal fail_once
        if fail_once and objects is not None:
            fail_once = False
            raise RuntimeError("injected media persistence failure")
        await original_flush(session, objects)  # type: ignore[arg-type]

    monkeypatch.setattr(AsyncSession, "flush", fail_media_flush)
    with pytest.raises(RuntimeError, match="persistence failure"):
        await finalize_media_asset(
            database_session_factory,
            media_asset_id=asset.media_asset_id,
            requesting_user_id=evidence.captured_by_user_id,
            storage=storage,
            policy=media_policy(),
        )
    pending = await asset_state(database_session_factory, asset.media_asset_id)
    assert pending[6:9] == (None, None, UPLOAD_PENDING)
    assert pending[10] is None

    recovered = await finalize_media_asset(
        database_session_factory,
        media_asset_id=asset.media_asset_id,
        requesting_user_id=evidence.captured_by_user_id,
        storage=storage,
        policy=media_policy(),
        now=FINALIZED_AT,
    )
    assert recovered.media_asset_id == asset.media_asset_id
    assert recovered.object_key == asset.object_key
    assert recovered.upload_status == UPLOAD_FINALIZED


async def test_pickup_media_failures_do_not_mutate_evidence_or_fulfilment(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture, assignment, evidence = await create_pickup_capture(database_session_factory)
    pickup_id = fixture.pickup_ids[0][0]
    before = await pickup_state(
        database_session_factory,
        pickup_id=pickup_id,
        assignment_id=assignment.assignment_id,
        rider_id=fixture.rider_ids[0],
    )
    asset = await register(database_session_factory, evidence=evidence)
    storage = FakeMediaStorage()
    storage.authorization_error = RuntimeError("authorization unavailable")
    with pytest.raises(RuntimeError):
        await request_upload_authorization(
            database_session_factory,
            media_asset_id=asset.media_asset_id,
            requesting_user_id=evidence.captured_by_user_id,
            storage=storage,
        )
    storage.objects[asset.object_key] = StoredObjectProperties("image/wrong", 500)
    with pytest.raises(MediaContentTypePolicyError):
        await finalize_media_asset(
            database_session_factory,
            media_asset_id=asset.media_asset_id,
            requesting_user_id=evidence.captured_by_user_id,
            storage=storage,
            policy=media_policy(),
        )
    after = await pickup_state(
        database_session_factory,
        pickup_id=pickup_id,
        assignment_id=assignment.assignment_id,
        rider_id=fixture.rider_ids[0],
    )
    async with database_session_factory() as session:
        persisted_evidence = await session.get(EvidenceCapture, evidence.evidence_capture_id)
        link = await session.get(PickupEvidenceLink, (pickup_id, evidence.evidence_capture_id))
        outbox_count = await session.scalar(select(func.count()).select_from(OutboxEvent))
    assert before == after
    assert persisted_evidence is not None and persisted_evidence.captured_at == evidence.captured_at
    assert link is not None
    assert outbox_count == before[-1]


async def test_handover_media_failures_do_not_mutate_event_or_evidence(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fixture, _point, event = await create_handover_fixture(
        database_session_factory,
        validated=False,
    )
    evidence = await capture(
        database_session_factory,
        actor_id=fixture.rider_ids[0],
        target_kind="HANDOVER",
        target_id=event.handover_event_id,
    )
    asset = await register(database_session_factory, evidence=evidence)
    async with database_session_factory() as session:
        before = await session.get(HandoverEvent, event.handover_event_id)
        outbox_before = await session.scalar(select(func.count()).select_from(OutboxEvent))
        assert before is not None
        before_state = (
            before.status,
            before.validation_code,
            before.distance_m,
            before.evaluated_at,
        )
    storage = FakeMediaStorage()
    with pytest.raises(MediaObjectNotReadyError):
        await finalize_media_asset(
            database_session_factory,
            media_asset_id=asset.media_asset_id,
            requesting_user_id=evidence.captured_by_user_id,
            storage=storage,
            policy=media_policy(),
        )

    async with database_session_factory() as session:
        after = await session.get(HandoverEvent, event.handover_event_id)
        link = await session.get(
            HandoverEvidenceLink,
            (event.handover_event_id, evidence.evidence_capture_id),
        )
        outbox_count = await session.scalar(select(func.count()).select_from(OutboxEvent))
        assert after is not None
        after_state = (
            after.status,
            after.validation_code,
            after.distance_m,
            after.evaluated_at,
        )
    assert after_state == before_state
    assert link is not None
    assert outbox_count == outbox_before
