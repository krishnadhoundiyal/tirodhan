"""Require PostGIS to be provisioned before application migrations.

Revision ID: 0001_enable_postgis
Revises:
Create Date: 2026-09-26
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0001_enable_postgis"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _postgis_is_installed() -> bool:
    result = op.get_bind().execute(
        text(
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_extension
                WHERE extname = 'postgis'
            )
            """
        )
    )
    return bool(result.scalar_one())


def upgrade() -> None:
    if not _postgis_is_installed():
        raise RuntimeError(
            "PostGIS must be provisioned before Alembic migrations; "
            "run the database bootstrap for Azure environments"
        )


def downgrade() -> None:
    # PostGIS is platform/bootstrap-owned, not application-migration-owned.
    pass
