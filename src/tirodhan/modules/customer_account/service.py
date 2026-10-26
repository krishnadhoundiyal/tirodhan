from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import TypeVar, cast
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from tirodhan.db.values import utc_now
from tirodhan.modules.customer_account.models import (
    CustomerCommandReceipt,
    CustomerFavouriteCategory,
    CustomerFavouriteSet,
    CustomerFeedback,
    CustomerPreferences,
    CustomerProfile,
)
from tirodhan.modules.customer_account.protection import CustomerDataProtection
from tirodhan.modules.customer_account.schemas import (
    Command,
    FavouritesDto,
    FavouritesWrite,
    FeedbackDto,
    FeedbackWrite,
    PreferencesDto,
    PreferencesWrite,
    ProfileDto,
    ProfileWrite,
)
from tirodhan.modules.customer_reads.errors import CustomerReadError
from tirodhan.modules.customer_reads.models import CatalogueCategory, CatalogueGroup
from tirodhan.modules.identity.authorization import lock_active_user_role
from tirodhan.modules.identity.models import UserPhone
from tirodhan.modules.identity.phone_protection import PhoneDecryptionError
from tirodhan.modules.identity.ports import (
    IdentityProviderNotConfiguredError,
    PhoneIdentityProtector,
)
from tirodhan.modules.reliability.primitives import (
    IdempotencyKeyConflictError,
    claim_idempotency_record,
    complete_idempotency_record,
)

T = TypeVar("T", bound=BaseModel)


async def command(
    session: AsyncSession,
    *,
    owner: UUID,
    operation: str,
    body: Command,
    protection: CustomerDataProtection,
    ttl: int | None,
    result_type: type[T],
    execute: Callable[[], Awaitable[T]],
    role: str = "CUSTOMER",
) -> T:
    if ttl is None or ttl <= 0:
        raise CustomerReadError(503, "COMMAND_RETENTION_UNCONFIGURED")
    if not await lock_active_user_role(session, user_id=owner, role_code=role, no_key_update=True):
        raise CustomerReadError(403, "NOT_ELIGIBLE")
    scope = f"customer.{operation}:{owner}"
    try:
        claim = await claim_idempotency_record(
            session,
            scope=scope,
            idempotency_key=str(body.client_request_id),
            request_fingerprint=protection.fingerprint(scope, body.model_dump(mode="json")),
            expires_at=utc_now() + timedelta(seconds=ttl),
        )
    except IdempotencyKeyConflictError:
        raise CustomerReadError(409, "COMMAND_CONFLICT") from None
    if not claim.created:
        receipt = await session.scalar(
            select(CustomerCommandReceipt).where(
                CustomerCommandReceipt.command_id == claim.record.idempotency_record_id,
                CustomerCommandReceipt.user_id == owner,
            )
        )
        if receipt is None or claim.record.status != "COMPLETED":
            raise CustomerReadError(409, "COMMAND_IN_PROGRESS")
        return result_type.model_validate(protection.decrypt(scope, receipt.result_encrypted))
    result = await execute()
    receipt = CustomerCommandReceipt(
        command_id=claim.record.idempotency_record_id,
        user_id=owner,
        result_encrypted=protection.encrypt(scope, result.model_dump(mode="json")),
    )
    session.add(receipt)
    await session.flush()
    await complete_idempotency_record(
        session,
        claim.record,
        result_resource_id=receipt.receipt_id,
        result_status_code=201 if operation == "feedback" else 200,
    )
    return result


async def profile(
    session: AsyncSession,
    owner: UUID,
    protection: CustomerDataProtection,
    phone_protector: PhoneIdentityProtector,
) -> ProfileDto:
    row = await session.get(CustomerProfile, owner)
    details = (
        protection.decrypt(f"profile:{owner}", row.details_encrypted)
        if (row is not None and row.details_encrypted is not None)
        else {"display_name": None, "email": None}
    )
    protected_phone = await session.scalar(
        select(UserPhone.phone_encrypted).where(
            UserPhone.user_id == owner, UserPhone.retired_at.is_(None)
        )
    )
    if protected_phone is None:
        raise CustomerReadError(503, "PHONE_DISPLAY_UNAVAILABLE")
    try:
        phone = await phone_protector.unprotect(protected_phone)
    except (IdentityProviderNotConfiguredError, PhoneDecryptionError):
        raise CustomerReadError(503, "PHONE_DISPLAY_UNAVAILABLE") from None
    return ProfileDto(
        user_id=owner,
        display_name=details["display_name"],
        email=details["email"],
        phone_display=f"•••••• {phone[-4:]}",
        version=row.version if row else 1,
    )


async def preferences(session: AsyncSession, owner: UUID) -> PreferencesDto:
    row = await session.get(CustomerPreferences, owner)
    return (
        PreferencesDto.model_validate(
            {
                "language": row.language,
                "collection_notifications": row.collection_notifications,
                "version": row.version,
            }
        )
        if row
        else PreferencesDto()
    )


async def favourites(session: AsyncSession, owner: UUID) -> FavouritesDto:
    row = await session.get(CustomerFavouriteSet, owner)
    codes = list(
        await session.scalars(
            select(CatalogueCategory.category_code)
            .join(
                CustomerFavouriteCategory,
                CustomerFavouriteCategory.category_id == CatalogueCategory.category_id,
            )
            .join(CatalogueGroup)
            .where(
                CustomerFavouriteCategory.user_id == owner,
                CatalogueCategory.active,
                CatalogueGroup.active,
            )
            .order_by(
                CatalogueGroup.display_order,
                CatalogueGroup.group_code,
                CatalogueCategory.display_order,
                CatalogueCategory.category_code,
            )
        )
    )
    return FavouritesDto(category_codes=codes, version=row.version if row else 1)


async def update_account(
    session: AsyncSession,
    *,
    owner: UUID,
    body: ProfileWrite | PreferencesWrite | FavouritesWrite,
    protection: CustomerDataProtection,
    phone_protector: PhoneIdentityProtector,
) -> ProfileDto | PreferencesDto | FavouritesDto:
    # The caller holds the user NO KEY UPDATE lock, including first-write creation.
    model: type[CustomerProfile] | type[CustomerPreferences] | type[CustomerFavouriteSet] = (
        CustomerProfile
        if isinstance(body, ProfileWrite)
        else CustomerPreferences
        if isinstance(body, PreferencesWrite)
        else CustomerFavouriteSet
    )
    row: CustomerProfile | CustomerPreferences | CustomerFavouriteSet | None = await session.get(
        model, owner
    )
    if body.expected_version != (row.version if row else 1):
        raise CustomerReadError(409, "VERSION_CONFLICT")
    if row is None:
        row = model(user_id=owner, version=1)
        session.add(row)
    if isinstance(body, ProfileWrite):
        cast(CustomerProfile, row).details_encrypted = protection.encrypt(
            f"profile:{owner}", {"display_name": body.display_name, "email": body.email}
        )
    elif isinstance(body, PreferencesWrite):
        pref = cast(CustomerPreferences, row)
        pref.language = body.language
        pref.collection_notifications = body.collection_notifications
    else:
        # Lock both levels so retirement cannot race validation/commit.
        categories = list(
            await session.scalars(
                select(CatalogueCategory)
                .join(CatalogueGroup)
                .where(
                    CatalogueCategory.category_code.in_(body.category_codes),
                    CatalogueCategory.active,
                    CatalogueGroup.active,
                )
                .with_for_update(read=True)
            )
        )
        if len(categories) != len(body.category_codes):
            raise CustomerReadError(422, "INVALID_CATEGORY")
        await session.flush()
        await session.execute(
            delete(CustomerFavouriteCategory).where(CustomerFavouriteCategory.user_id == owner)
        )
        session.add_all(
            [
                CustomerFavouriteCategory(user_id=owner, category_id=c.category_id)
                for c in categories
            ]
        )
    row.version += 1
    row.updated_at = utc_now()
    await session.flush()
    if isinstance(body, ProfileWrite):
        return await profile(session, owner, protection, phone_protector)
    if isinstance(body, PreferencesWrite):
        return await preferences(session, owner)
    return await favourites(session, owner)


async def feedback(
    session: AsyncSession,
    owner: UUID,
    body: FeedbackWrite,
    protection: CustomerDataProtection,
) -> FeedbackDto:
    row = CustomerFeedback(
        user_id=owner,
        client_request_id=body.client_request_id,
        message_encrypted=protection.encrypt(f"feedback:{owner}", body.message),
    )
    session.add(row)
    await session.flush()
    return FeedbackDto(feedback_id=row.feedback_id)
