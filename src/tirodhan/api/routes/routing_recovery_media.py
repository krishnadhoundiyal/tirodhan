"""Manager self-upload for witnessed recovery facts; existing ownership checks apply."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.api.dependencies import (
    get_media_policy,
    get_media_storage,
    get_session_factory,
    require_role,
)
from tirodhan.api.routes.rider import (
    MediaAssetResponse,
    MediaRegistrationRequest,
    UploadAuthorizationResponse,
    post_finalize_media_asset,
    post_media_asset,
    post_media_upload_authorization,
)
from tirodhan.modules.evidence.media_ports import MediaPolicy, MediaStoragePort
from tirodhan.modules.identity.service import AuthenticatedPrincipal

router = APIRouter(prefix="/v2/manager", tags=["recovery evidence"])
Manager = Annotated[AuthenticatedPrincipal, Depends(require_role("MANAGER"))]
Factory = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]
Policy = Annotated[MediaPolicy, Depends(get_media_policy)]
Storage = Annotated[MediaStoragePort, Depends(get_media_storage)]


@router.post("/media-assets", response_model=MediaAssetResponse)
async def register(
    body: MediaRegistrationRequest,
    request: Request,
    actor: Manager,
    factory: Factory,
    policy: Policy,
) -> MediaAssetResponse:
    return await post_media_asset(body, request, actor, factory, policy)


@router.post(
    "/media-assets/{media_asset_id}/upload-authorization",
    response_model=UploadAuthorizationResponse,
)
async def upload(
    media_asset_id: UUID, actor: Manager, factory: Factory, storage: Storage
) -> UploadAuthorizationResponse:
    return await post_media_upload_authorization(media_asset_id, actor, factory, storage)


@router.post("/media-assets/{media_asset_id}/finalize", response_model=MediaAssetResponse)
async def finalize(
    media_asset_id: UUID, actor: Manager, factory: Factory, storage: Storage, policy: Policy
) -> MediaAssetResponse:
    return await post_finalize_media_asset(media_asset_id, actor, factory, storage, policy)
