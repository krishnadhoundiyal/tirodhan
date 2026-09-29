from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated, cast
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.modules.collection_requests.ports import PricingPort
from tirodhan.modules.customers.ports import AddressProtector
from tirodhan.modules.identity.ports import OtpProvider, PhoneIdentityProtector
from tirodhan.modules.identity.service import (
    ROLE_CUSTOMER,
    AccessAuthenticationError,
    AuthenticatedPrincipal,
    authenticate_access_token,
)
from tirodhan.modules.identity.tokens import (
    AccessTokenCodec,
    AccessTokenConfigurationError,
)
from tirodhan.modules.payments.ports import PaymentProvider

_bearer = HTTPBearer(auto_error=False)


async def get_database_session(request: Request) -> AsyncIterator[AsyncSession]:
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.database_session_factory)
    async with factory() as session, session.begin():
        yield session


def get_access_token_codec(request: Request) -> AccessTokenCodec:
    return cast(AccessTokenCodec, request.app.state.access_token_codec)


def get_otp_provider(request: Request) -> OtpProvider:
    return cast(OtpProvider, request.app.state.otp_provider)


def get_phone_identity_protector(request: Request) -> PhoneIdentityProtector:
    return cast(PhoneIdentityProtector, request.app.state.phone_identity_protector)


async def get_authenticated_principal(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    session: Annotated[AsyncSession, Depends(get_database_session)],
    token_codec: Annotated[AccessTokenCodec, Depends(get_access_token_codec)],
) -> AuthenticatedPrincipal:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _unauthorized()
    try:
        return await authenticate_access_token(
            session,
            raw_access_token=credentials.credentials,
            token_codec=token_codec,
        )
    except AccessTokenConfigurationError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication is not configured",
        ) from error
    except AccessAuthenticationError as error:
        raise _unauthorized() from error


def get_current_user_id(
    principal: Annotated[AuthenticatedPrincipal, Depends(get_authenticated_principal)],
) -> UUID:
    return principal.user_id


def require_role(
    role_code: str,
) -> Callable[[AuthenticatedPrincipal], Awaitable[AuthenticatedPrincipal]]:
    async def dependency(
        principal: Annotated[AuthenticatedPrincipal, Depends(get_authenticated_principal)],
    ) -> AuthenticatedPrincipal:
        if role_code not in principal.roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient role",
            )
        return principal

    return dependency


def get_current_customer_id(
    principal: Annotated[AuthenticatedPrincipal, Depends(require_role(ROLE_CUSTOMER))],
) -> UUID:
    return principal.user_id


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication failed",
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_address_protector(request: Request) -> AddressProtector:
    return cast(AddressProtector, request.app.state.address_protector)


def get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    return cast(async_sessionmaker[AsyncSession], request.app.state.database_session_factory)


def get_pricing_port(request: Request) -> PricingPort:
    return cast(PricingPort, request.app.state.pricing_port)


def get_payment_provider(request: Request) -> PaymentProvider:
    return cast(PaymentProvider, request.app.state.payment_provider)
