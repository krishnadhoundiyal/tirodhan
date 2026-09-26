"""Enable the PostGIS extension.

Revision ID: 0001_enable_postgis
Revises:
Create Date: 2026-09-26
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001_enable_postgis"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")


def downgrade() -> None:
    op.execute("DROP EXTENSION IF EXISTS postgis")
