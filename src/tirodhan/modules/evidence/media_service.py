from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.customers.service import command_fingerprint
from tirodhan.modules.evidence.media_ports import (
    MediaPolicy,
    MediaStoragePort,
    StoredObjectProperties,
    UploadAuthorization,
)
from tirodhan.modules.evidence.models import EvidenceCapture, MediaAsset
from tirodhan.modules.reliability.primitives import (
    claim_idempotency_record,
    complete_idempotency_record,
    get_completed_idempotency_result,
)

MEDIA_PHOTO = "PHOTO"
MEDIA_VIDEO = "VIDEO"
MEDIA_TYPES = frozenset({MEDIA_PHOTO, MEDIA_VIDEO})
UPLOAD_PENDING = "PENDING_UPLOAD"
UPLOAD_FINALIZED = "FINALIZED"
MEDIA_REGISTER_IDEMPOTENCY_SCOPE = "media.register"


class MediaAssetError(RuntimeError):
    pass


class InvalidMediaTypeError(ValueError):
    pass


class MediaContentTypePolicyError(MediaAssetError):
    pass


class MediaSizePolicyError(MediaAssetError):
    pass


class MediaEvidenceNotFoundError(MediaAssetError):
    pass


class MediaActorAttributionError(MediaAssetError):
    pass


class MediaAlreadyRegisteredError(MediaAssetError):
    pass


class MediaRegistrationInProgressError(MediaAssetError):
    pass


class MediaRegistrationStateInconsistentError(MediaAssetError):
    pass


class MediaAssetNotFoundError(MediaAssetError):
    pass


class MediaWriteAuthorizationUnavailableError(MediaAssetError):
    pass


class MediaObjectNotReadyError(MediaAssetError):
    pass


class MediaLifecycleStateError(MediaAssetError):
    pass


async def register_media_asset(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    client_media_id: UUID,
    evidence_capture_id: UUID,
    requesting_user_id: UUID,
    media_type: str,
    expected_content_type: str,
    policy: MediaPolicy,
    idempotency_expires_at: datetime,
    now: datetime | None = None,
) -> MediaAsset:
    fingerprint = command_fingerprint(
        {
            "evidence_capture_id": evidence_capture_id,
            "requesting_user_id": requesting_user_id,
            "media_type": media_type,
            "expected_content_type": expected_content_type,
        }
    )
    async with session_factory() as session, session.begin():
        claim = await claim_idempotency_record(
            session,
            scope=MEDIA_REGISTER_IDEMPOTENCY_SCOPE,
            idempotency_key=str(client_media_id),
            request_fingerprint=fingerprint,
            expires_at=idempotency_expires_at,
        )
        if not claim.created:
            result = get_completed_idempotency_result(claim.record)
            if result is None or result.resource_id is None:
                raise MediaRegistrationInProgressError("media registration is in progress")
            asset = await session.get(MediaAsset, result.resource_id)
            if asset is None or asset.client_media_id != client_media_id:
                raise MediaRegistrationStateInconsistentError(
                    "completed media registration references an invalid asset"
                )
            return asset

        _validate_registration_policy(
            policy,
            media_type=media_type,
            expected_content_type=expected_content_type,
        )
        evidence = await session.get(EvidenceCapture, evidence_capture_id)
        if evidence is None:
            raise MediaEvidenceNotFoundError("evidence capture not found")
        if evidence.captured_by_user_id != requesting_user_id:
            raise MediaActorAttributionError("evidence capture belongs to another user")

        created_at = now or utc_now()
        media_asset_id = new_uuid7()
        asset = await session.scalar(
            insert(MediaAsset)
            .values(
                media_asset_id=media_asset_id,
                client_media_id=client_media_id,
                evidence_capture_id=evidence_capture_id,
                media_type=media_type,
                object_key=f"media/{media_asset_id}",
                expected_content_type=expected_content_type,
                stored_content_type=None,
                size_bytes=None,
                upload_status=UPLOAD_PENDING,
                created_at=created_at,
                finalized_at=None,
            )
            .on_conflict_do_nothing(index_elements=[MediaAsset.evidence_capture_id])
            .returning(MediaAsset)
        )
        if asset is None:
            existing = await session.scalar(
                select(MediaAsset).where(MediaAsset.evidence_capture_id == evidence_capture_id)
            )
            if existing is None:
                raise MediaRegistrationStateInconsistentError(
                    "media uniqueness conflict did not resolve to an asset"
                )
            raise MediaAlreadyRegisteredError(
                "evidence capture already has its original media asset"
            )

        await complete_idempotency_record(
            session,
            claim.record,
            result_resource_id=asset.media_asset_id,
            result_status_code=None,
            completed_at=created_at,
        )
        return asset


async def request_upload_authorization(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    media_asset_id: UUID,
    requesting_user_id: UUID,
    storage: MediaStoragePort,
) -> UploadAuthorization:
    async with session_factory() as session:
        asset = await _load_owned_asset(
            session,
            media_asset_id=media_asset_id,
            requesting_user_id=requesting_user_id,
        )
        if asset.upload_status != UPLOAD_PENDING:
            raise MediaWriteAuthorizationUnavailableError(
                "write authorization is unavailable after media finalization"
            )
        object_key = asset.object_key
        expected_content_type = asset.expected_content_type

    return await storage.create_upload_authorization(
        object_key=object_key,
        expected_content_type=expected_content_type,
    )


async def finalize_media_asset(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    media_asset_id: UUID,
    requesting_user_id: UUID,
    storage: MediaStoragePort,
    policy: MediaPolicy,
    now: datetime | None = None,
) -> MediaAsset:
    async with session_factory() as session:
        asset = await _load_owned_asset(
            session,
            media_asset_id=media_asset_id,
            requesting_user_id=requesting_user_id,
        )
        if asset.upload_status == UPLOAD_FINALIZED:
            return asset
        if asset.upload_status != UPLOAD_PENDING:
            raise MediaLifecycleStateError("media asset is not in a finalizable state")
        object_key = asset.object_key
        media_type = asset.media_type
        expected_content_type = asset.expected_content_type

    properties = await storage.inspect_object(object_key=object_key)
    if properties is None:
        raise MediaObjectNotReadyError("media object is not available in storage")
    _validate_stored_properties(
        policy,
        media_type=media_type,
        expected_content_type=expected_content_type,
        properties=properties,
    )

    async with session_factory() as session, session.begin():
        durable = await session.scalar(
            select(MediaAsset).where(MediaAsset.media_asset_id == media_asset_id).with_for_update()
        )
        if durable is None:
            raise MediaAssetNotFoundError("media asset not found during finalization")
        if durable.upload_status == UPLOAD_FINALIZED:
            return durable
        if durable.upload_status != UPLOAD_PENDING:
            raise MediaLifecycleStateError("media asset is not in a finalizable state")
        durable.stored_content_type = properties.content_type
        durable.size_bytes = properties.size_bytes
        durable.upload_status = UPLOAD_FINALIZED
        durable.finalized_at = now or utc_now()
        await session.flush([durable])
        return durable


async def _load_owned_asset(
    session: AsyncSession,
    *,
    media_asset_id: UUID,
    requesting_user_id: UUID,
) -> MediaAsset:
    asset = await session.get(MediaAsset, media_asset_id)
    if asset is None:
        raise MediaAssetNotFoundError("media asset not found")
    evidence = await session.get(EvidenceCapture, asset.evidence_capture_id)
    if evidence is None:
        raise MediaRegistrationStateInconsistentError(
            "media asset references a missing evidence capture"
        )
    if evidence.captured_by_user_id != requesting_user_id:
        raise MediaActorAttributionError("media asset belongs to another user")
    return asset


def _validate_registration_policy(
    policy: MediaPolicy,
    *,
    media_type: str,
    expected_content_type: str,
) -> None:
    if media_type not in MEDIA_TYPES:
        raise InvalidMediaTypeError("media_type must be PHOTO or VIDEO")
    if not policy.is_content_type_allowed(media_type, expected_content_type):
        raise MediaContentTypePolicyError("expected content type is not allowed for the media type")


def _validate_stored_properties(
    policy: MediaPolicy,
    *,
    media_type: str,
    expected_content_type: str,
    properties: StoredObjectProperties,
) -> None:
    if properties.content_type != expected_content_type or not policy.is_content_type_allowed(
        media_type, properties.content_type
    ):
        raise MediaContentTypePolicyError(
            "storage content type does not match the registered media type"
        )
    if properties.size_bytes < 0 or properties.size_bytes > policy.max_size_bytes(media_type):
        raise MediaSizePolicyError("stored media size violates configured policy")
