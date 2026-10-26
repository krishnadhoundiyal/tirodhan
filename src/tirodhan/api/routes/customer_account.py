import json
from collections.abc import Callable, Coroutine
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.api.dependencies import (
    get_current_customer_id,
    get_phone_identity_protector,
    get_session_factory,
    require_role,
)
from tirodhan.core.config import Settings
from tirodhan.modules.customer_account import content, devices, native_push, service, web_push
from tirodhan.modules.customer_account.notifications import notification_page
from tirodhan.modules.customer_account.protection import CustomerDataProtection
from tirodhan.modules.customer_account.schemas import (
    BrowserSubscription,
    ContentDraft,
    ContentDto,
    ContentPublish,
    ContentRevisionDto,
    FavouritesDto,
    FavouritesWrite,
    FeedbackDto,
    FeedbackWrite,
    NativeRegistration,
    NotificationPage,
    PaymentMethodsDto,
    PreferencesDto,
    PreferencesWrite,
    ProfileDto,
    ProfileWrite,
    RegistrationDto,
    Slug,
)
from tirodhan.modules.customer_reads.errors import CustomerReadError
from tirodhan.modules.identity.ports import PhoneIdentityProtector
from tirodhan.modules.identity.service import AuthenticatedPrincipal


class CommittedRoute(APIRoute):
    """Exit the transaction before ASGI sends success, independent of yield-dependency scope."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def committed(request: Request) -> Response:
            factory = cast(
                async_sessionmaker[AsyncSession], request.app.state.database_session_factory
            )
            async with factory() as session, session.begin():
                request.state.customer_account_session = session
                return await handler(request)

        return committed


router = APIRouter(tags=["customer account and content"], route_class=CommittedRoute)
Customer = Annotated[UUID, Depends(get_current_customer_id)]
Principal = Annotated[AuthenticatedPrincipal, Depends(require_role("CUSTOMER"))]
Manager = Annotated[AuthenticatedPrincipal, Depends(require_role("MANAGER"))]
Phone = Annotated[PhoneIdentityProtector, Depends(get_phone_identity_protector)]


def settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def protection(request: Request) -> CustomerDataProtection:
    return CustomerDataProtection(settings(request))


def private_session(response: Response, request: Request) -> AsyncSession:
    response.headers["Cache-Control"] = "private, no-store"
    return cast(AsyncSession, request.state.customer_account_session)


Session = Annotated[AsyncSession, Depends(private_session)]


@router.get("/v1/customer/me/profile", response_model=ProfileDto)
async def get_profile(
    customer: Customer, session: Session, request: Request, phone: Phone
) -> ProfileDto:
    return await service.profile(session, customer, protection(request), phone)


@router.put("/v1/customer/me/profile", response_model=ProfileDto)
async def put_profile(
    body: ProfileWrite, customer: Customer, session: Session, request: Request, phone: Phone
) -> ProfileDto:
    crypto = protection(request)

    async def execute() -> ProfileDto:
        return ProfileDto.model_validate(
            (
                await service.update_account(
                    session, owner=customer, body=body, protection=crypto, phone_protector=phone
                )
            ).model_dump()
        )

    return await service.command(
        session,
        owner=customer,
        operation="profile",
        body=body,
        protection=crypto,
        ttl=settings(request).command_idempotency_ttl_seconds,
        result_type=ProfileDto,
        execute=execute,
    )


@router.get("/v1/customer/me/preferences", response_model=PreferencesDto)
async def get_preferences(customer: Customer, session: Session) -> PreferencesDto:
    return await service.preferences(session, customer)


@router.put("/v1/customer/me/preferences", response_model=PreferencesDto)
async def put_preferences(
    body: PreferencesWrite, customer: Customer, session: Session, request: Request, phone: Phone
) -> PreferencesDto:
    crypto = protection(request)

    async def execute() -> PreferencesDto:
        return PreferencesDto.model_validate(
            (
                await service.update_account(
                    session, owner=customer, body=body, protection=crypto, phone_protector=phone
                )
            ).model_dump()
        )

    return await service.command(
        session,
        owner=customer,
        operation="preferences",
        body=body,
        protection=crypto,
        ttl=settings(request).command_idempotency_ttl_seconds,
        result_type=PreferencesDto,
        execute=execute,
    )


@router.get("/v1/customer/me/favourite-categories", response_model=FavouritesDto)
async def get_favourites(customer: Customer, session: Session) -> FavouritesDto:
    return await service.favourites(session, customer)


@router.put("/v1/customer/me/favourite-categories", response_model=FavouritesDto)
async def put_favourites(
    body: FavouritesWrite, customer: Customer, session: Session, request: Request, phone: Phone
) -> FavouritesDto:
    crypto = protection(request)

    async def execute() -> FavouritesDto:
        return FavouritesDto.model_validate(
            (
                await service.update_account(
                    session, owner=customer, body=body, protection=crypto, phone_protector=phone
                )
            ).model_dump()
        )

    return await service.command(
        session,
        owner=customer,
        operation="favourites",
        body=body,
        protection=crypto,
        ttl=settings(request).command_idempotency_ttl_seconds,
        result_type=FavouritesDto,
        execute=execute,
    )


@router.post("/v1/customer/feedback", response_model=FeedbackDto, status_code=201)
async def post_feedback(
    body: FeedbackWrite, customer: Customer, session: Session, request: Request
) -> FeedbackDto:
    crypto = protection(request)

    async def execute() -> FeedbackDto:
        return await service.feedback(session, customer, body, crypto)

    return await service.command(
        session,
        owner=customer,
        operation="feedback",
        body=body,
        protection=crypto,
        ttl=settings(request).command_idempotency_ttl_seconds,
        result_type=FeedbackDto,
        execute=execute,
    )


@router.get("/v1/customer/payment-methods", response_model=PaymentMethodsDto)
async def get_payment_methods(customer: Customer, session: Session) -> PaymentMethodsDto:
    # No authoritative method-discovery port exists on the configured payment provider.
    return PaymentMethodsDto(methods=[])


@router.get("/v1/content/{slug}", response_model=ContentDto)
async def get_content(
    slug: str,
    response: Response,
    factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> ContentDto:
    if slug not in ("help", "about", "privacy", "terms"):
        raise CustomerReadError(404, "CONTENT_UNPUBLISHED")
    async with factory() as session, session.begin():
        result = await content.read_content(session, cast(Slug, slug))
    response.headers["Cache-Control"] = "public, max-age=300"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return result


@router.post("/v1/manager/content/{slug}/revisions", response_model=ContentRevisionDto)
async def draft_content(
    slug: Slug, body: ContentDraft, actor: Manager, session: Session, request: Request
) -> ContentRevisionDto:
    async def execute() -> ContentRevisionDto:
        return await content.change_content(session, actor=actor.user_id, slug=slug, body=body)

    return await service.command(
        session,
        owner=actor.user_id,
        operation=f"content-draft:{slug}",
        body=body,
        protection=protection(request),
        role="MANAGER",
        ttl=settings(request).command_idempotency_ttl_seconds,
        result_type=ContentRevisionDto,
        execute=execute,
    )


@router.post(
    "/v1/manager/content/{slug}/revisions/{revision_id}/publish", response_model=ContentRevisionDto
)
async def publish_content(
    slug: Slug,
    revision_id: UUID,
    body: ContentPublish,
    actor: Manager,
    session: Session,
    request: Request,
) -> ContentRevisionDto:
    async def execute() -> ContentRevisionDto:
        return await content.change_content(
            session, actor=actor.user_id, slug=slug, body=body, revision_id=revision_id
        )

    return await service.command(
        session,
        owner=actor.user_id,
        operation=f"content-publish:{slug}:{revision_id}",
        body=body,
        protection=protection(request),
        role="MANAGER",
        ttl=settings(request).command_idempotency_ttl_seconds,
        result_type=ContentRevisionDto,
        execute=execute,
    )


@router.get("/v1/customer/notifications", response_model=NotificationPage)
async def get_notifications(
    customer: Customer,
    session: Session,
    request: Request,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
) -> NotificationPage:
    return await notification_page(
        session, owner=customer, limit=limit, token=cursor, settings=settings(request)
    )


@router.put("/v1/customer/me/push-devices/{client_device_id}", response_model=RegistrationDto)
async def put_native_push(
    client_device_id: UUID,
    body: NativeRegistration,
    actor: Principal,
    session: Session,
    request: Request,
) -> RegistrationDto:
    if client_device_id != body.client_device_id:
        raise CustomerReadError(422, "DEVICE_ID_MISMATCH")
    config = settings(request)
    if body.token_provider != "FCM":
        raise CustomerReadError(503, "PUSH_PROVIDER_UNAVAILABLE")
    native_push.require_fcm(config)
    return await devices.register(
        session,
        principal=actor,
        installation=client_device_id,
        platform=body.platform,
        provider=body.token_provider,
        secret=body.token.get_secret_value(),
        protection=protection(request),
    )


@router.delete("/v1/customer/me/push-devices/{client_device_id}", status_code=204)
async def delete_native_push(client_device_id: UUID, actor: Principal, session: Session) -> None:
    await devices.revoke(
        session, principal=actor, installation=client_device_id, platform="ANDROID"
    )


def browser_origin(request: Request, origin_header: str | None) -> str:
    config = settings(request)
    # The gateway must preserve the browser Origin. Bearer auth still owns identity.
    if origin_header not in config.customer_web_push_origins or origin_header is None:
        raise CustomerReadError(403, "ORIGIN_NOT_ALLOWED")
    return origin_header


@router.put(
    "/v1/customer/me/web-push-subscriptions/{client_device_id}", response_model=RegistrationDto
)
async def put_web_push(
    client_device_id: UUID,
    body: BrowserSubscription,
    actor: Principal,
    session: Session,
    request: Request,
    origin_header: Annotated[str | None, Header(alias="Origin")] = None,
) -> RegistrationDto:
    origin = browser_origin(request, origin_header)
    web_push.check_configuration(settings(request))
    web_push.validate_subscription(body, settings(request))
    data = body.model_dump(mode="json")
    data["endpoint"] = body.endpoint.get_secret_value()
    return await devices.register(
        session,
        principal=actor,
        installation=client_device_id,
        platform="WEB",
        provider="WEBPUSH",
        browser_origin=origin,
        secret=json.dumps(data, sort_keys=True),
        token_identity=body.endpoint.get_secret_value(),
        protection=protection(request),
    )


@router.delete("/v1/customer/me/web-push-subscriptions/{client_device_id}", status_code=204)
async def delete_web_push(
    client_device_id: UUID,
    actor: Principal,
    session: Session,
    request: Request,
    origin_header: Annotated[str | None, Header(alias="Origin")] = None,
) -> None:
    origin = browser_origin(request, origin_header)
    await devices.revoke(
        session,
        principal=actor,
        installation=client_device_id,
        platform="WEB",
        browser_origin=origin,
    )
