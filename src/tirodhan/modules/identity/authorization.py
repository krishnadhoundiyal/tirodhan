from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tirodhan.modules.identity.models import AppUser, UserRole
from tirodhan.modules.identity.service import APP_USER_ACTIVE


async def lock_active_user_role(
    session: AsyncSession,
    *,
    user_id: UUID,
    role_code: str,
) -> bool:
    """Lock current user/role eligibility for a fresh-work transaction."""
    user = await session.scalar(select(AppUser).where(AppUser.user_id == user_id).with_for_update())
    if user is None or user.status != APP_USER_ACTIVE:
        return False
    role = await session.scalar(
        select(UserRole)
        .where(
            UserRole.user_id == user_id,
            UserRole.role_code == role_code,
            UserRole.revoked_at.is_(None),
        )
        .with_for_update()
    )
    return role is not None
