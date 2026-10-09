import hashlib
import json
from typing import Protocol, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tirodhan.modules.customer_reads.errors import CustomerReadError
from tirodhan.modules.customer_reads.models import (
    CatalogueArtwork,
    CatalogueCategory,
    CatalogueGroup,
    CatalogueMedia,
)
from tirodhan.modules.customer_reads.schemas import (
    ArtworkDto,
    CatalogueCategoryDto,
    CatalogueDto,
    CatalogueGroupDto,
    CategoryInputDto,
    MediaDto,
    QuickCategoryDto,
)
from tirodhan.modules.evidence.media_ports import UploadAuthorization


class ProductMediaPort(Protocol):
    async def authorize_product_image(self, *, object_key: str) -> UploadAuthorization: ...


class UnconfiguredProductMedia:
    async def authorize_product_image(self, *, object_key: str) -> UploadAuthorization:
        raise CustomerReadError(503, "NOT_ELIGIBLE")


async def load_catalogue(
    session: AsyncSession,
) -> tuple[
    list[CatalogueGroup], list[CatalogueCategory], list[CatalogueMedia], list[CatalogueArtwork]
]:
    groups = list(
        await session.scalars(
            select(CatalogueGroup)
            .where(CatalogueGroup.active.is_(True))
            .order_by(CatalogueGroup.display_order, CatalogueGroup.group_code)
            .limit(201)
        )
    )
    categories = list(
        await session.scalars(
            select(CatalogueCategory)
            .join(CatalogueGroup)
            .where(CatalogueCategory.active.is_(True), CatalogueGroup.active.is_(True))
            .order_by(CatalogueCategory.display_order, CatalogueCategory.category_code)
            .limit(1001)
        )
    )
    artwork = list(await session.scalars(select(CatalogueArtwork).order_by(CatalogueArtwork.kind)))
    ids = {x for c in categories for x in (c.image_id, c.thumbnail_id) if x is not None}
    ids.update(a.media_id for a in artwork)
    media = (
        list(
            await session.scalars(
                select(CatalogueMedia)
                .where(CatalogueMedia.media_id.in_(ids))
                .order_by(CatalogueMedia.media_id)
            )
        )
        if ids
        else []
    )
    if len(groups) > 200 or len(categories) > 1000:
        raise CustomerReadError(503, "NOT_ELIGIBLE")
    return groups, categories, media, artwork


async def project_catalogue(
    data: tuple[
        list[CatalogueGroup], list[CatalogueCategory], list[CatalogueMedia], list[CatalogueArtwork]
    ],
    storage: ProductMediaPort,
) -> CatalogueDto:
    groups, categories, assets, artwork = data
    # Hash approved metadata rather than ephemeral signed URLs; edits/activation
    # change the version, refreshing SAS for unchanged artwork does not.
    metadata = [
        [
            {column.name: str(getattr(row, column.name)) for column in row.__table__.columns}
            for row in rows
        ]
        for rows in data
    ]
    version = hashlib.sha256(json.dumps(metadata, sort_keys=True).encode()).hexdigest()
    media: dict[UUID, MediaDto] = {}
    for asset in assets:
        authorization = await storage.authorize_product_image(object_key=asset.object_key)
        media[asset.media_id] = MediaDto(
            url=authorization.opaque_value,
            thumbnail_url=None,
            width=asset.width,
            height=asset.height,
            alt_text=asset.alt_text,
            blurhash=asset.blurhash,
            expires_at=authorization.expires_at,
        )
    group_by_id = {g.group_id: g.group_code for g in groups}
    offered = [c for c in categories if c.image_id in media and c.thumbnail_id in media]
    return CatalogueDto(
        version=version,
        groups=[
            CatalogueGroupDto.model_validate(
                {
                    "group_code": g.group_code,
                    "display_name": g.display_name,
                    "display_order": g.display_order,
                    "active": g.active,
                }
            )
            for g in groups
        ],
        categories=[
            CatalogueCategoryDto(
                category_code=c.category_code,
                group_code=group_by_id[c.group_id],
                display_name=c.display_name,
                description=c.description,
                display_order=c.display_order,
                active=True,
                image=media[cast(UUID, c.image_id)],
                thumbnail=media[cast(UUID, c.thumbnail_id)],
                handling_hints=c.handling_hints,
                input=CategoryInputDto.model_validate(
                    {
                        "quantity": c.quantity_input,
                        "weight_grams": c.weight_input,
                    }
                ),
            )
            for c in offered
        ],
        quick_categories=[
            QuickCategoryDto(
                category_code=c.category_code,
                label=c.quick_label,
                display_order=c.quick_order,
            )
            for c in sorted(offered, key=lambda c: (c.quick_order or 0, c.category_code))
            if c.quick_label is not None and c.quick_order is not None
        ],
        artwork=ArtworkDto.model_validate({a.kind: media.get(a.media_id) for a in artwork}),
    )
