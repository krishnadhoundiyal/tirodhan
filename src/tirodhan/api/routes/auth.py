from __future__ import annotations

from datetime import timedelta
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.api.dependencies import (
    get_access_token_codec,
    get_otp_provider,
    get_phone_identity_protector,
    get_session_factory,
)
from tirodhan.core.config import Settings
from tirodhan.db.values import utc_now
from tirodhan.modules.identity.ports import (
    IdentityProviderNotConfiguredError,
    OtpProvider,
    OtpRequestConflictError,
    OtpVerificationError,
    PhoneIdentityProtector,
)
from tirodhan.modules.identity.service import (
    IdentityInputError,
    LoginCommandInProgressError,
    LoginCredentialsUnavailableReplayError,
    RefreshAuthenticationError,
    UserAuthenticationDeniedError,
    logout_refresh_session,
    refresh_access_token,
    start_otp_verification,
    verify_otp_and_login,
)
from tirodhan.modules.identity.tokens import (
    AccessTokenCodec,
    AccessTokenConfigurationError,
)
from tirodhan.modules.reliability.primitives import IdempotencyKeyConflictError

router = APIRouter(prefix="/v1/auth", tags=["authentication"])


class OtpStartRequest(BaseModel):
    client_request_id: UUID
    phone: SecretStr


class OtpStartResponse(BaseModel):
    challenge_reference: str


class OtpVerifyRequest(BaseModel):
    client_login_id: UUID
    phone: SecretStr
    challenge_reference: str
    code: SecretStr


class AccessTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user_id: UUID


class LoginTokenResponse(AccessTokenResponse):
    refresh_token: str


class RefreshRequest(BaseModel):
    refresh_token: SecretStr


class LogoutRequest(BaseModel):
    refresh_token: SecretStr


def _login_settings(request: Request) -> tuple[int, int, int]:
    settings = cast(Settings, request.app.state.settings)
    access_ttl = settings.auth_access_token_ttl_seconds
    refresh_ttl = settings.auth_refresh_session_ttl_seconds
    idempotency_ttl = settings.command_idempotency_ttl_seconds
    if access_ttl is None or refresh_ttl is None or idempotency_ttl is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication lifetimes are not configured",
        )
    return access_ttl, refresh_ttl, idempotency_ttl


def _access_token_ttl(request: Request) -> int:
    settings = cast(Settings, request.app.state.settings)
    if settings.auth_access_token_ttl_seconds is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Access-token lifetime is not configured",
        )
    return settings.auth_access_token_ttl_seconds


@router.post(
    "/otp/start",
    response_model=OtpStartResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def post_otp_start(
    body: OtpStartRequest,
    provider: Annotated[OtpProvider, Depends(get_otp_provider)],
) -> OtpStartResponse:
    try:
        challenge = await start_otp_verification(
            provider,
            phone=body.phone.get_secret_value(),
            client_request_id=body.client_request_id,
        )
        return OtpStartResponse(challenge_reference=challenge.challenge_reference)
    except IdentityInputError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)
        ) from error
    except OtpRequestConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="OTP request conflicts"
        ) from error
    except IdentityProviderNotConfiguredError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="OTP provider is not configured",
        ) from error


@router.post("/otp/verify", response_model=LoginTokenResponse)
async def post_otp_verify(
    body: OtpVerifyRequest,
    request: Request,
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    provider: Annotated[OtpProvider, Depends(get_otp_provider)],
    protector: Annotated[PhoneIdentityProtector, Depends(get_phone_identity_protector)],
    token_codec: Annotated[AccessTokenCodec, Depends(get_access_token_codec)],
) -> LoginTokenResponse:
    access_ttl, refresh_ttl, idempotency_ttl = _login_settings(request)
    if not token_codec.configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication is not configured",
        )
    try:
        result = await verify_otp_and_login(
            session_factory,
            client_login_id=body.client_login_id,
            phone=body.phone.get_secret_value(),
            challenge_reference=body.challenge_reference,
            code=body.code.get_secret_value(),
            otp_provider=provider,
            phone_protector=protector,
            token_codec=token_codec,
            refresh_session_ttl_seconds=refresh_ttl,
            access_token_ttl_seconds=access_ttl,
            idempotency_expires_at=utc_now() + timedelta(seconds=idempotency_ttl),
        )
        return LoginTokenResponse(
            access_token=result.access_token.get_secret_value(),
            refresh_token=result.refresh_token.get_secret_value(),
            expires_in=result.expires_in,
            user_id=result.user_id,
        )
    except LoginCredentialsUnavailableReplayError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Login already completed; start a new OTP verification",
        ) from error
    except IdentityInputError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)
        ) from error
    except (IdempotencyKeyConflictError, LoginCommandInProgressError) as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Login conflicts"
        ) from error
    except (OtpVerificationError, UserAuthenticationDeniedError) as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication failed"
        ) from error
    except (IdentityProviderNotConfiguredError, AccessTokenConfigurationError) as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication is not configured",
        ) from error


@router.post("/refresh", response_model=AccessTokenResponse)
async def post_refresh(
    body: RefreshRequest,
    request: Request,
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    token_codec: Annotated[AccessTokenCodec, Depends(get_access_token_codec)],
) -> AccessTokenResponse:
    access_ttl = _access_token_ttl(request)
    try:
        result = await refresh_access_token(
            session_factory,
            raw_refresh_credential=body.refresh_token.get_secret_value(),
            token_codec=token_codec,
            access_token_ttl_seconds=access_ttl,
        )
        return AccessTokenResponse(
            access_token=result.access_token.get_secret_value(),
            expires_in=result.expires_in,
            user_id=result.user_id,
        )
    except RefreshAuthenticationError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication failed"
        ) from error
    except AccessTokenConfigurationError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication is not configured",
        ) from error


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def post_logout(
    body: LogoutRequest,
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> Response:
    await logout_refresh_session(
        session_factory,
        raw_refresh_credential=body.refresh_token.get_secret_value(),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
