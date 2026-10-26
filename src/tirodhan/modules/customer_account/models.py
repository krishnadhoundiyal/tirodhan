from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from tirodhan.db.base import Base
from tirodhan.db.values import new_uuid7, utc_now


class CustomerProfile(Base):
    __tablename__ = "customer_profile"
    __table_args__ = (CheckConstraint("version >= 1", name="ck_customer_profile_version"),)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("app_user.user_id"), primary_key=True)
    details_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary)
    version: Mapped[int] = mapped_column(BigInteger, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class CustomerPreferences(Base):
    __tablename__ = "customer_preferences"
    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_customer_preferences_version"),
        CheckConstraint("language IN ('en-IN', 'hi-IN')", name="ck_customer_preferences_language"),
    )
    user_id: Mapped[UUID] = mapped_column(ForeignKey("app_user.user_id"), primary_key=True)
    language: Mapped[str] = mapped_column(String(8), default="en-IN")
    collection_notifications: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(BigInteger, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class CustomerFavouriteSet(Base):
    __tablename__ = "customer_favourite_set"
    __table_args__ = (CheckConstraint("version >= 1", name="ck_customer_favourite_version"),)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("app_user.user_id"), primary_key=True)
    version: Mapped[int] = mapped_column(BigInteger, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class CustomerFavouriteCategory(Base):
    __tablename__ = "customer_favourite_category"
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("customer_favourite_set.user_id"), primary_key=True
    )
    category_id: Mapped[UUID] = mapped_column(
        ForeignKey("catalogue_category.category_id"), primary_key=True
    )


class CustomerCommandReceipt(Base):
    """Encrypted protocol replay snapshot, never authority for current account state."""

    __tablename__ = "customer_command_receipt"
    receipt_id: Mapped[UUID] = mapped_column(primary_key=True, default=new_uuid7)
    command_id: Mapped[UUID] = mapped_column(
        ForeignKey("idempotency_record.idempotency_record_id"), unique=True
    )
    user_id: Mapped[UUID] = mapped_column(ForeignKey("app_user.user_id"), index=True)
    result_encrypted: Mapped[bytes] = mapped_column(LargeBinary)


class CustomerFeedback(Base):
    __tablename__ = "customer_feedback"
    __table_args__ = (
        UniqueConstraint("user_id", "client_request_id", name="uq_customer_feedback_command"),
    )
    feedback_id: Mapped[UUID] = mapped_column(primary_key=True, default=new_uuid7)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("app_user.user_id"), index=True)
    client_request_id: Mapped[UUID]
    message_encrypted: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ContentRevision(Base):
    __tablename__ = "content_revision"
    __table_args__ = (
        UniqueConstraint("slug", "revision", name="uq_content_slug_revision"),
        CheckConstraint("slug IN ('help','about','privacy','terms')", name="ck_content_slug"),
        CheckConstraint("revision > 0", name="ck_content_revision"),
        CheckConstraint("status IN ('DRAFT','PUBLISHED','RETIRED')", name="ck_content_status"),
        CheckConstraint(
            "(status = 'DRAFT' AND published_at IS NULL AND approved_by IS NULL "
            "AND approval_reference IS NULL) OR (status IN ('PUBLISHED','RETIRED') "
            "AND published_at IS NOT NULL AND approved_by IS NOT NULL "
            "AND approval_reference IS NOT NULL)",
            name="ck_content_approval",
        ),
        Index(
            "uq_content_published_slug",
            "slug",
            unique=True,
            postgresql_where=text("status = 'PUBLISHED'"),
        ),
    )
    revision_id: Mapped[UUID] = mapped_column(primary_key=True, default=new_uuid7)
    slug: Mapped[str] = mapped_column(String(16))
    revision: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(16), default="DRAFT")
    authored_by: Mapped[UUID] = mapped_column(ForeignKey("app_user.user_id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    approved_by: Mapped[UUID | None] = mapped_column(ForeignKey("app_user.user_id"))
    approval_reference: Mapped[str | None] = mapped_column(String(200))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ContentParagraph(Base):
    __tablename__ = "content_paragraph"
    __table_args__ = (
        CheckConstraint("position >= 0", name="ck_content_paragraph_position"),
        CheckConstraint("length(body) BETWEEN 1 AND 4000", name="ck_content_paragraph_body"),
    )
    revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("content_revision.revision_id"), primary_key=True
    )
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    body: Mapped[str] = mapped_column(String(4000))


class CustomerPushBinding(Base):
    __tablename__ = "customer_push_binding"
    __table_args__ = (
        UniqueConstraint("binding_id", "user_id", name="uq_customer_binding_owner"),
        ForeignKeyConstraint(
            ["session_id", "user_id"],
            ["refresh_session.refresh_session_id", "refresh_session.user_id"],
            name="fk_customer_binding_session_owner",
        ),
        CheckConstraint(
            "(platform = 'ANDROID' AND provider = 'FCM' AND browser_origin IS NULL) OR "
            "(platform = 'WEB' AND provider = 'WEBPUSH' AND browser_origin IS NOT NULL)",
            name="ck_customer_binding_platform",
        ),
        CheckConstraint("generation >= 1", name="ck_customer_binding_generation"),
        Index(
            "uq_customer_binding_installation",
            "client_device_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
        Index(
            "uq_customer_binding_token",
            "provider",
            "token_hmac",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
        Index("ix_customer_binding_user", "user_id", "session_id"),
        Index("ix_customer_binding_session", "session_id"),
        Index("ix_customer_binding_installation_history", "client_device_id", "session_id"),
        Index("ix_customer_binding_token_history", "provider", "token_hmac", "session_id"),
        Index(
            "ix_customer_binding_credential_history", "provider", "credential_hmac", "session_id"
        ),
    )
    binding_id: Mapped[UUID] = mapped_column(primary_key=True, default=new_uuid7)
    client_device_id: Mapped[UUID]
    user_id: Mapped[UUID] = mapped_column(ForeignKey("app_user.user_id"))
    session_id: Mapped[UUID]
    platform: Mapped[str] = mapped_column(String(16))
    provider: Mapped[str] = mapped_column(String(16))
    browser_origin: Mapped[str | None] = mapped_column(String(300))
    token_hmac: Mapped[bytes] = mapped_column(LargeBinary)
    credential_hmac: Mapped[bytes] = mapped_column(LargeBinary)
    token_encrypted: Mapped[bytes] = mapped_column(LargeBinary)
    generation: Mapped[int] = mapped_column(BigInteger, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CustomerNotification(Base):
    __tablename__ = "customer_notification"
    __table_args__ = (
        UniqueConstraint("notification_id", "user_id", name="uq_customer_notification_owner"),
        ForeignKeyConstraint(
            ["request_id", "user_id"],
            ["collection_request.request_id", "collection_request.customer_id"],
            name="fk_customer_notification_request_owner",
        ),
        Index("ix_customer_notification_page", "user_id", "created_at", "notification_id"),
    )
    notification_id: Mapped[UUID] = mapped_column(primary_key=True, default=new_uuid7)
    source_event_id: Mapped[UUID] = mapped_column(
        ForeignKey("outbox_event.outbox_event_id"), unique=True
    )
    user_id: Mapped[UUID] = mapped_column(ForeignKey("app_user.user_id"))
    request_id: Mapped[UUID]
    event_type: Mapped[str] = mapped_column(String(100))
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class CustomerPushDelivery(Base):
    __tablename__ = "customer_push_delivery"
    __table_args__ = (
        ForeignKeyConstraint(
            ["notification_id", "user_id"],
            ["customer_notification.notification_id", "customer_notification.user_id"],
            name="fk_customer_delivery_notification_owner",
        ),
        ForeignKeyConstraint(
            ["binding_id", "user_id"],
            ["customer_push_binding.binding_id", "customer_push_binding.user_id"],
            name="fk_customer_delivery_binding_owner",
        ),
        UniqueConstraint("notification_id", "binding_id", name="uq_customer_delivery_device"),
        CheckConstraint(
            "status IN ('PENDING','SENDING','SENT','SUPPRESSED','PERMANENTLY_FAILED')",
            name="ck_customer_delivery_status",
        ),
        CheckConstraint("attempts >= 0", name="ck_customer_delivery_attempts"),
        CheckConstraint(
            "(status = 'SENDING' AND lease_token IS NOT NULL AND lease_until IS NOT NULL) OR "
            "(status <> 'SENDING' AND lease_token IS NULL AND lease_until IS NULL)",
            name="ck_customer_delivery_lease",
        ),
        Index("ix_customer_delivery_due", "status", "next_attempt_at"),
    )
    delivery_id: Mapped[UUID] = mapped_column(primary_key=True, default=new_uuid7)
    notification_id: Mapped[UUID]
    user_id: Mapped[UUID] = mapped_column(ForeignKey("app_user.user_id"))
    binding_id: Mapped[UUID]
    binding_generation: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(24), default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    lease_token: Mapped[UUID | None]
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(String(32))
