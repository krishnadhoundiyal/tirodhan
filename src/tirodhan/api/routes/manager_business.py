"""Manager business configuration and attributable historical comparisons."""

from datetime import timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.api.dependencies import get_session_factory, require_role
from tirodhan.db.values import utc_now
from tirodhan.modules.business_admin.commands import BusinessCommand, MediaRegister
from tirodhan.modules.business_admin.history import compare, version, versions
from tirodhan.modules.business_admin.service import MODELS, execute_business_command
from tirodhan.modules.customer_reads.errors import CustomerReadError
from tirodhan.modules.customer_reads.models import CatalogueMedia
from tirodhan.modules.evidence.media_policy import MediaPolicyNotConfiguredError
from tirodhan.modules.evidence.media_ports import (
    MediaStorageNotConfiguredError,
    MediaStorageUnavailableError,
)
from tirodhan.modules.identity.service import AuthenticatedPrincipal
from tirodhan.modules.reliability.primitives import IdempotencyKeyConflictError
from tirodhan.modules.routing_operations.commands import OperationalInputError

router = APIRouter(prefix="/v1/manager/business", tags=["manager business configuration"])
Manager = Annotated[AuthenticatedPrincipal, Depends(require_role("MANAGER"))]
Factory = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]


def expiry(request: Request) -> Any:
    seconds = request.app.state.settings.command_idempotency_ttl_seconds
    if seconds is None or seconds <= 0:
        raise HTTPException(503, "COMMAND_RETENTION_REQUIRED")
    return utc_now() + timedelta(seconds=seconds)


@router.post("/commands")
async def mutate(
    command: BusinessCommand, request: Request, actor: Manager, factory: Factory
) -> dict[str, UUID]:
    verified = False
    try:
        if isinstance(command, MediaRegister):
            async with factory() as session:
                exists = await session.get(CatalogueMedia, command.resource_id)
            if exists is None:
                properties = await request.app.state.media_storage.inspect_object(
                    object_key=f"product-art/{command.resource_id}"
                )
                policy = request.app.state.media_policy
                verified = bool(
                    properties
                    and policy.is_content_type_allowed("PHOTO", properties.content_type)
                    and 0 < properties.size_bytes <= policy.max_size_bytes("PHOTO")
                )
                if not verified:
                    raise HTTPException(409, "VALID_PRODUCT_IMAGE_REQUIRED")
        result = await execute_business_command(
            factory,
            actor_id=actor.user_id,
            command=command,
            expires_at=expiry(request),
            media_verified=verified,
        )
        return {"resource_id": result}
    except (
        MediaStorageNotConfiguredError,
        MediaStorageUnavailableError,
        MediaPolicyNotConfiguredError,
    ):
        raise HTTPException(503, "PRODUCT_STORAGE_UNAVAILABLE") from None
    except (OperationalInputError, IdempotencyKeyConflictError) as error:
        raise HTTPException(409, str(error)) from None
    except (DBAPIError, IntegrityError):
        raise HTTPException(409, "RETRYABLE_CONFIGURATION_CONFLICT") from None


class ProductUpload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_command_id: UUID
    content_type: str = Field(min_length=1, max_length=100)


@router.post("/product-images/upload")
async def upload_product(
    body: ProductUpload, request: Request, actor: Manager, factory: Factory
) -> Any:
    async with factory() as session:
        if await session.get(CatalogueMedia, body.client_command_id):
            raise HTTPException(409, "PRODUCT_ASSET_ALREADY_FINALIZED")
    policy = request.app.state.media_policy
    try:
        allowed = policy.is_content_type_allowed("PHOTO", body.content_type)
    except MediaPolicyNotConfiguredError:
        raise HTTPException(503, "PRODUCT_MEDIA_POLICY_REQUIRED") from None
    if not allowed:
        raise HTTPException(422, "PRODUCT_CONTENT_TYPE_NOT_ALLOWED")
    storage = request.app.state.media_storage
    method = getattr(storage, "create_product_upload_authorization", None)
    if method is None:
        raise HTTPException(503, "PRODUCT_STORAGE_UNAVAILABLE")
    try:
        authorization = await method(
            object_key=f"product-art/{body.client_command_id}",
            expected_content_type=body.content_type,
        )
    except (MediaStorageNotConfiguredError, MediaStorageUnavailableError):
        raise HTTPException(503, "PRODUCT_STORAGE_UNAVAILABLE") from None
    return {
        "media_id": body.client_command_id,
        "upload_authorization": authorization.opaque_value,
        "expires_at": authorization.expires_at,
        "overwrite_allowed": False,
    }


@router.get("/bookings/{request_id}/lineage")
async def read_booking_lineage(
    request_id: UUID,
    actor: Manager,
    factory: Factory,
    after_id: UUID | None = None,
    limit: int = Query(50, ge=1, le=100),
) -> Any:
    from tirodhan.modules.business_admin.lineage import booking_lineage

    async with factory() as session:
        try:
            return await booking_lineage(session, request_id, after_id=after_id, limit=limit)
        except ValueError as error:
            raise HTTPException(404, str(error)) from None


@router.get("/routes/{route_id}/versions")
async def read_route_versions(
    route_id: UUID,
    actor: Manager,
    factory: Factory,
    after: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
) -> Any:
    from tirodhan.modules.business_admin.lineage import route_history

    async with factory() as session:
        try:
            return await route_history(session, route_id, after=after, limit=limit)
        except ValueError as error:
            raise HTTPException(404, str(error)) from None


@router.get("/bookings/{request_id}/events/{kind}")
async def read_booking_facts(
    request_id: UUID,
    kind: str,
    actor: Manager,
    factory: Factory,
    after_id: UUID | None = None,
    limit: int = Query(50, ge=1, le=100),
) -> Any:
    from tirodhan.modules.business_admin.lineage import booking_facts

    async with factory() as session:
        try:
            return await booking_facts(session, request_id, kind, after_id=after_id, limit=limit)
        except ValueError as error:
            raise HTTPException(404, str(error)) from None


@router.get("/routes/{route_id}/compare")
async def compare_route_versions(
    route_id: UUID,
    actor: Manager,
    factory: Factory,
    before: int = Query(ge=1),
    after: int = Query(ge=1),
) -> Any:
    from tirodhan.modules.business_admin.lineage import route_history

    async with factory() as session:
        old = await route_history(session, route_id, after=before - 1, limit=1)
        new = await route_history(session, route_id, after=after - 1, limit=1)
    if (
        not old["versions"]
        or old["versions"][0]["version"] != before
        or not new["versions"]
        or new["versions"][0]["version"] != after
    ):
        raise HTTPException(404, "ROUTE_VERSION_UNAVAILABLE")
    a, b = old["versions"][0], new["versions"][0]
    return {
        "route_id": route_id,
        "before": a,
        "after": b,
        "changed_fields": [k for k in sorted(a.keys() | b.keys()) if a.get(k) != b.get(k)],
    }


@router.get("/{kind}")
async def entities(
    kind: str,
    actor: Manager,
    factory: Factory,
    after_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> Any:
    lookup = {model.__tablename__: (model, key) for model, key in MODELS.values()}
    if kind not in lookup:
        raise HTTPException(404, "BUSINESS_ENTITY_UNAVAILABLE")
    model, key = lookup[kind]
    column = getattr(model, key)
    async with factory() as session:
        query = select(column).order_by(column).limit(limit + 1)
        if after_id:
            query = query.where(column > after_id)
        ids = list(await session.scalars(query))
        values = []
        for identity in ids[:limit]:
            history = await versions(session, kind, identity, limit=1)
            current = history["current_revision"]
            values.append(await version(session, kind, identity, current))
        return {"items": values, "next_cursor": ids[limit - 1] if len(ids) > limit else None}


@router.get("/{kind}/{identity}/versions")
async def timeline(
    kind: str,
    identity: str,
    actor: Manager,
    factory: Factory,
    after: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
) -> Any:
    async with factory() as session:
        try:
            return await versions(session, kind, identity, after=after, limit=limit)
        except ValueError as error:
            raise HTTPException(404, str(error)) from None


@router.get("/{kind}/{identity}/versions/{revision}")
async def historical(
    kind: str, identity: str, revision: int, actor: Manager, factory: Factory
) -> Any:
    async with factory() as session:
        try:
            return await version(session, kind, identity, revision)
        except ValueError as error:
            raise HTTPException(404, str(error)) from None


@router.get("/{kind}/{identity}/compare")
async def difference(
    kind: str, identity: str, before: int, after: int, actor: Manager, factory: Factory
) -> Any:
    async with factory() as session:
        try:
            return await compare(session, kind, identity, before, after)
        except ValueError as error:
            raise HTTPException(404, str(error)) from None


@router.get("/product-images/{media_id}/read")
async def historical_product_media(
    media_id: UUID, request: Request, actor: Manager, factory: Factory
) -> Any:
    async with factory() as session:
        asset = await session.get(CatalogueMedia, media_id)
        if asset is None:
            raise HTTPException(404, "PRODUCT_ASSET_UNAVAILABLE")
    try:
        authorization = await request.app.state.product_media.authorize_product_image(
            object_key=asset.object_key
        )
    except (MediaStorageNotConfiguredError, MediaStorageUnavailableError, CustomerReadError):
        raise HTTPException(503, "PRODUCT_STORAGE_UNAVAILABLE") from None
    return {
        "media_id": media_id,
        "url": authorization.opaque_value,
        "expires_at": authorization.expires_at,
        "width": asset.width,
        "height": asset.height,
        "alt_text": asset.alt_text,
        "blurhash": asset.blurhash,
    }


@router.get("/events/{kind}/{identity}")
async def duty_events(
    kind: str,
    identity: UUID,
    actor: Manager,
    factory: Factory,
    after: str | None = None,
    limit: int = Query(50, ge=1, le=100),
) -> Any:
    from tirodhan.modules.business_admin.events import events

    async with factory() as session:
        try:
            return await events(session, kind, identity, after=after, limit=limit)
        except ValueError as error:
            raise HTTPException(404, str(error)) from None


@router.get("/events/{kind}/{identity}/compare")
async def compare_duty_events(
    kind: str, identity: UUID, before: str, after: str, actor: Manager, factory: Factory
) -> Any:
    from tirodhan.modules.business_admin.events import event

    async with factory() as session:
        try:
            old = await event(session, kind, identity, before)
            new = await event(session, kind, identity, after)
        except ValueError as error:
            raise HTTPException(404, str(error)) from None
    a, b = old["state"], new["state"]
    return {
        "history_semantics": "IMMUTABLE_FACT_COMPARISON",
        "before": old,
        "after": new,
        "changes": {
            k: {"before": a.get(k), "after": b.get(k)}
            for k in sorted(a.keys() | b.keys())
            if a.get(k) != b.get(k)
        },
    }


@router.get("/events/{kind}/{identity}/{event_id}")
async def duty_event(
    kind: str, identity: UUID, event_id: str, actor: Manager, factory: Factory
) -> Any:
    from tirodhan.modules.business_admin.events import event

    async with factory() as session:
        try:
            return await event(session, kind, identity, event_id)
        except ValueError as error:
            raise HTTPException(404, str(error)) from None
