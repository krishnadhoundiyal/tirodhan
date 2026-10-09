"""Approved product presentation metadata; no taxonomy or artwork is seeded."""

from uuid import UUID

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from tirodhan.db.base import Base
from tirodhan.db.values import new_uuid7


class CatalogueMedia(Base):
    __tablename__ = "catalogue_media"
    __table_args__ = (
        CheckConstraint("width > 0 AND height > 0", name="ck_catalogue_media_dimensions"),
        CheckConstraint("object_key LIKE 'product-art/%'", name="ck_catalogue_media_product_key"),
    )
    media_id: Mapped[UUID] = mapped_column(primary_key=True, default=new_uuid7)
    object_key: Mapped[str] = mapped_column(String(300), unique=True)
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    alt_text: Mapped[str] = mapped_column(String(300))
    blurhash: Mapped[str | None] = mapped_column(String(100))


class CatalogueGroup(Base):
    __tablename__ = "catalogue_group"
    group_id: Mapped[UUID] = mapped_column(primary_key=True, default=new_uuid7)
    group_code: Mapped[str] = mapped_column(String(100), unique=True)
    display_name: Mapped[str] = mapped_column(String(200))
    display_order: Mapped[int] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean)


class CatalogueCategory(Base):
    __tablename__ = "catalogue_category"
    __table_args__ = (
        CheckConstraint(
            "quantity_input IN ('NONE', 'OPTIONAL')", name="ck_category_quantity_input"
        ),
        CheckConstraint("weight_input IN ('NONE', 'OPTIONAL')", name="ck_category_weight_input"),
    )
    category_id: Mapped[UUID] = mapped_column(primary_key=True, default=new_uuid7)
    category_code: Mapped[str] = mapped_column(String(100), unique=True)
    group_id: Mapped[UUID] = mapped_column(ForeignKey("catalogue_group.group_id"))
    display_name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(String(2000))
    display_order: Mapped[int] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean)
    image_id: Mapped[UUID | None] = mapped_column(ForeignKey("catalogue_media.media_id"))
    thumbnail_id: Mapped[UUID | None] = mapped_column(ForeignKey("catalogue_media.media_id"))
    handling_hints: Mapped[str | None] = mapped_column(String(2000))
    quantity_input: Mapped[str] = mapped_column(String(16))
    weight_input: Mapped[str] = mapped_column(String(16))
    quick_label: Mapped[str | None] = mapped_column(String(200))
    quick_order: Mapped[int | None] = mapped_column(Integer)


class CatalogueArtwork(Base):
    __tablename__ = "catalogue_artwork"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('hero', 'home', 'rickshaw', 'receiving_point')",
            name="ck_catalogue_artwork_kind",
        ),
    )
    artwork_id: Mapped[UUID] = mapped_column(primary_key=True, default=new_uuid7)
    kind: Mapped[str] = mapped_column(String(24), unique=True)
    media_id: Mapped[UUID] = mapped_column(ForeignKey("catalogue_media.media_id"))
