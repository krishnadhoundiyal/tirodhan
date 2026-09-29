from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from tirodhan.db.base import Base
from tirodhan.db.values import new_uuid7, utc_now


class AppUser(Base):
    """Persisted human identity root without authentication mechanics."""

    __tablename__ = "app_user"

    user_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=new_uuid7,
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )


class UserPhone(Base):
    __tablename__ = "user_phone"
    __table_args__ = (
        Index(
            "uq_user_phone_active_lookup_hmac",
            "phone_lookup_hmac",
            unique=True,
            postgresql_where=text("retired_at IS NULL"),
        ),
        Index(
            "uq_user_phone_active_user",
            "user_id",
            unique=True,
            postgresql_where=text("retired_at IS NULL"),
        ),
    )

    user_phone_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=new_uuid7
    )
    user_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("app_user.user_id"), nullable=False
    )
    phone_encrypted: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    phone_lookup_hmac: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class UserRole(Base):
    __tablename__ = "user_role"
    __table_args__ = (
        CheckConstraint(
            "role_code IN ('CUSTOMER', 'RIDER', 'MANAGER')",
            name="ck_user_role_code",
        ),
        Index(
            "uq_user_role_active_user_code",
            "user_id",
            "role_code",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )

    user_role_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=new_uuid7
    )
    user_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("app_user.user_id"), nullable=False
    )
    role_code: Mapped[str] = mapped_column(String(24), nullable=False)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    granted_by_user_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("app_user.user_id")
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_by_user_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("app_user.user_id")
    )


class RefreshSession(Base):
    __tablename__ = "refresh_session"
    __table_args__ = (
        CheckConstraint("expires_at > created_at", name="ck_refresh_session_expiry"),
        UniqueConstraint("credential_hash", name="uq_refresh_session_credential_hash"),
    )

    refresh_session_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=new_uuid7
    )
    user_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("app_user.user_id"), nullable=False
    )
    credential_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
