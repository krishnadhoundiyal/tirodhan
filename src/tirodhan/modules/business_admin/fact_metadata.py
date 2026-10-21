"""Attribution metadata on existing immutable facts, without copying business history."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, text
from sqlalchemy.orm import Mapped, mapped_column


class BusinessFactMetadata:
    recorded_command_id: Mapped[UUID | None] = mapped_column(
        server_default=text("nullif(current_setting('tirodhan.command',true),'')::uuid")
    )
    recorded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )
