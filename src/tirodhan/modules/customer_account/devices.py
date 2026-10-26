from datetime import timedelta
from uuid import UUID

from sqlalchemy import exists, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from tirodhan.core.config import Settings
from tirodhan.db.values import utc_now
from tirodhan.modules.customer_account.models import CustomerPushBinding
from tirodhan.modules.customer_account.protection import CustomerDataProtection
from tirodhan.modules.customer_account.schemas import RegistrationDto
from tirodhan.modules.customer_reads.errors import CustomerReadError
from tirodhan.modules.identity.authorization import lock_active_user_role
from tirodhan.modules.identity.models import RefreshSession
from tirodhan.modules.identity.service import AuthenticatedPrincipal


async def register(
    session: AsyncSession,
    *,
    principal: AuthenticatedPrincipal,
    installation: UUID,
    platform: str,
    provider: str,
    secret: str,
    protection: CustomerDataProtection,
    settings: Settings,
    browser_origin: str | None = None,
    token_identity: str | None = None,
) -> RegistrationDto:
    if not await lock_active_user_role(
        session, user_id=principal.user_id, role_code="CUSTOMER", no_key_update=True
    ):
        raise CustomerReadError(403, "NOT_ELIGIBLE")
    login = await session.scalar(
        select(RefreshSession)
        .where(
            RefreshSession.refresh_session_id == principal.refresh_session_id,
            RefreshSession.user_id == principal.user_id,
        )
        .with_for_update()
    )
    # Database time after the customer lock: app clocks and concurrent sessions cannot
    # move the rolling window or admit changes against an earlier quota snapshot.
    now = await session.scalar(select(func.clock_timestamp()))
    assert now is not None
    if login is None or login.revoked_at is not None or login.expires_at <= now:
        raise CustomerReadError(401, "NOT_ELIGIBLE")
    token_hmac = protection.fingerprint(f"push:{provider}", token_identity or secret)
    credential_hmac = protection.fingerprint(f"push-credential:{provider}", secret)
    # Stable sorted locks cover absent rows, token rotation and account/installation transfers.
    keys = sorted([f"customer-installation:{installation}", f"customer-token:{token_hmac.hex()}"])
    for key in keys:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"), {"key": key}
        )
    identity = or_(
        CustomerPushBinding.client_device_id == installation,
        (CustomerPushBinding.provider == provider) & (CustomerPushBinding.token_hmac == token_hmac),
    )
    latest_login = await session.scalar(
        select(RefreshSession)
        .join(
            CustomerPushBinding, CustomerPushBinding.session_id == RefreshSession.refresh_session_id
        )
        .where(identity)
        .order_by(RefreshSession.created_at.desc(), RefreshSession.refresh_session_id.desc())
        .limit(1)
    )
    if latest_login is not None and (latest_login.created_at, latest_login.refresh_session_id) > (
        login.created_at,
        login.refresh_session_id,
    ):
        raise CustomerReadError(409, "STALE_REGISTRATION")
    generation = (
        await session.scalar(select(func.max(CustomerPushBinding.generation)).where(identity)) or 0
    )
    # Partial unique indexes guarantee at most two live rows in this identity union.
    history = list(
        await session.scalars(
            select(CustomerPushBinding)
            .where(identity, CustomerPushBinding.revoked_at.is_(None))
            .order_by(CustomerPushBinding.binding_id)
            .with_for_update()
        )
    )
    current = next(
        (p for p in history if p.revoked_at is None and p.client_device_id == installation), None
    )
    if (
        current is not None
        and current.user_id == principal.user_id
        and current.session_id == login.refresh_session_id
        and current.provider == provider
        and current.platform == platform
        and current.token_hmac == token_hmac
        and current.credential_hmac == credential_hmac
        and current.browser_origin == browser_origin
    ):
        return RegistrationDto(client_device_id=installation, registered_at=current.updated_at)
    if await session.scalar(
        select(
            exists().where(
                CustomerPushBinding.credential_hmac == credential_hmac,
                CustomerPushBinding.provider == provider,
                CustomerPushBinding.session_id == login.refresh_session_id,
            )
        )
    ):
        # Tombstones stop a lost old registration response from resurrecting a retired token.
        raise CustomerReadError(409, "STALE_REGISTRATION")
    limits = (
        settings.customer_native_registration_limit,
        settings.customer_browser_registration_limit,
        settings.customer_registration_change_limit,
        settings.customer_registration_window_seconds,
    )
    if any(value < 1 for value in limits):
        raise CustomerReadError(503, "DEVICE_POLICY_UNAVAILABLE")
    changes = await session.scalar(
        select(func.count())
        .select_from(CustomerPushBinding)
        .where(
            CustomerPushBinding.user_id == principal.user_id,
            CustomerPushBinding.created_at > now - timedelta(seconds=limits[3]),
        )
    )
    if changes is not None and changes >= limits[2]:
        raise CustomerReadError(429, "DEVICE_CHANGE_LIMIT")
    # Only live sessions hold capacity. Expired bindings are revoked after all admission
    # checks pass; rejected registrations have no effects even if the caller catches errors.
    live_session = exists().where(
        RefreshSession.refresh_session_id == CustomerPushBinding.session_id,
        RefreshSession.expires_at > now,
        RefreshSession.revoked_at.is_(None),
    )
    replacing = {row.binding_id for row in history}
    active_count = await session.scalar(
        select(func.count())
        .select_from(CustomerPushBinding)
        .where(
            CustomerPushBinding.user_id == principal.user_id,
            CustomerPushBinding.revoked_at.is_(None),
            live_session,
            CustomerPushBinding.platform.in_(("ANDROID", "IOS"))
            if platform != "WEB"
            else CustomerPushBinding.platform == "WEB",
            CustomerPushBinding.binding_id.not_in(replacing),
        )
    )
    allowance = limits[1] if platform == "WEB" else limits[0]
    if active_count is not None and active_count >= allowance:
        raise CustomerReadError(429, "DEVICE_ACTIVE_LIMIT")
    await session.execute(
        update(CustomerPushBinding)
        .where(
            CustomerPushBinding.user_id == principal.user_id,
            CustomerPushBinding.revoked_at.is_(None),
            ~live_session,
        )
        .values(revoked_at=now)
    )
    for prior in history:
        if prior.revoked_at is None:
            prior.revoked_at = now
    await session.flush()
    row = CustomerPushBinding(
        client_device_id=installation,
        user_id=principal.user_id,
        session_id=login.refresh_session_id,
        platform=platform,
        provider=provider,
        browser_origin=browser_origin,
        token_hmac=token_hmac,
        credential_hmac=credential_hmac,
        token_encrypted=protection.encrypt(f"push:{principal.user_id}:{installation}", secret),
        generation=1 + generation,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    await session.flush()
    return RegistrationDto(client_device_id=installation, registered_at=now)


async def revoke(
    session: AsyncSession,
    *,
    principal: AuthenticatedPrincipal,
    installation: UUID,
    platform: str,
    browser_origin: str | None = None,
) -> None:
    # Only this session's binding: a delayed logout must not revoke the successor account/session.
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
        {"key": f"customer-installation:{installation}"},
    )
    rows = list(
        await session.scalars(
            select(CustomerPushBinding)
            .where(
                CustomerPushBinding.client_device_id == installation,
                CustomerPushBinding.user_id == principal.user_id,
                CustomerPushBinding.session_id == principal.refresh_session_id,
                CustomerPushBinding.platform == platform,
                CustomerPushBinding.revoked_at.is_(None),
                CustomerPushBinding.browser_origin == browser_origin,
            )
            .with_for_update()
        )
    )
    for row in rows:
        row.revoked_at = utc_now()
