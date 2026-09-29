from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from tirodhan.api.router import api_router
from tirodhan.core.config import Settings, get_settings
from tirodhan.core.logging import configure_logging
from tirodhan.db.session import create_database_engine, create_session_factory
from tirodhan.modules.collection_requests.ports import PricingPort, UnconfiguredPricingPort
from tirodhan.modules.customers.ports import AddressProtector, UnconfiguredAddressProtector
from tirodhan.modules.evidence.azure_media import AzureBlobMediaStorage
from tirodhan.modules.evidence.media_policy import ConfiguredMediaPolicy, UnconfiguredMediaPolicy
from tirodhan.modules.evidence.media_ports import (
    MediaPolicy,
    MediaStoragePort,
    UnconfiguredMediaStoragePort,
)
from tirodhan.modules.identity.ports import (
    OtpProvider,
    PhoneIdentityProtector,
    UnconfiguredOtpProvider,
    UnconfiguredPhoneIdentityProtector,
)
from tirodhan.modules.identity.tokens import (
    AccessTokenCodec,
    Rs256AccessTokenCodec,
    UnconfiguredAccessTokenCodec,
)
from tirodhan.modules.payments.ports import PaymentProvider, UnconfiguredPaymentProvider
from tirodhan.modules.serviceability.ports import (
    CellIdDeriver,
    LocationResolver,
    UnconfiguredCellIdDeriver,
    UnconfiguredLocationResolver,
)

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    address_protector: AddressProtector | None = None,
    location_resolver: LocationResolver | None = None,
    cell_id_deriver: CellIdDeriver | None = None,
    pricing_port: PricingPort | None = None,
    payment_provider: PaymentProvider | None = None,
    otp_provider: OtpProvider | None = None,
    phone_identity_protector: PhoneIdentityProtector | None = None,
    access_token_codec: AccessTokenCodec | None = None,
    media_storage: MediaStoragePort | None = None,
    media_policy: MediaPolicy | None = None,
) -> FastAPI:
    application_settings = settings or get_settings()
    configure_logging(
        application_settings.log_level,
        application_settings.log_file_path,
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        engine = create_database_engine(application_settings)
        application.state.database_engine = engine
        application.state.database_session_factory = create_session_factory(engine)
        logger.info(
            "application_started",
            extra={"environment": application_settings.environment},
        )
        try:
            yield
        finally:
            await engine.dispose()

            media_storage = getattr(application.state, "media_storage", None)
            if isinstance(media_storage, AzureBlobMediaStorage):
                await media_storage.close()

            logger.info("application_stopped")

    application = FastAPI(
        title=application_settings.app_name,
        version="0.1.0",
        debug=application_settings.debug,
        lifespan=lifespan,
    )
    application.state.settings = application_settings
    application.state.address_protector = address_protector or UnconfiguredAddressProtector()
    application.state.location_resolver = location_resolver or UnconfiguredLocationResolver()
    application.state.cell_id_deriver = cell_id_deriver or UnconfiguredCellIdDeriver()
    application.state.pricing_port = pricing_port or UnconfiguredPricingPort()
    application.state.payment_provider = payment_provider or UnconfiguredPaymentProvider()
    application.state.otp_provider = otp_provider or UnconfiguredOtpProvider()
    application.state.phone_identity_protector = (
        phone_identity_protector or UnconfiguredPhoneIdentityProtector()
    )
    application.state.access_token_codec = access_token_codec or _token_codec(application_settings)
    application.state.media_storage = media_storage or _media_storage(application_settings)
    application.state.media_policy = media_policy or _media_policy(application_settings)
    application.include_router(api_router)
    return application


def _token_codec(settings: Settings) -> AccessTokenCodec:
    if (
        settings.auth_jwt_private_key_pem is None
        or settings.auth_jwt_public_key_pem is None
        or settings.auth_token_issuer is None
        or settings.auth_token_audience is None
    ):
        return UnconfiguredAccessTokenCodec()
    return Rs256AccessTokenCodec(
        private_key_pem=settings.auth_jwt_private_key_pem.get_secret_value(),
        public_key_pem=settings.auth_jwt_public_key_pem.get_secret_value(),
        issuer=settings.auth_token_issuer,
        audience=settings.auth_token_audience,
    )


def _media_storage(settings: Settings) -> MediaStoragePort:
    if (
        not settings.media_blob_account_url
        or not settings.media_blob_container_name
        or not settings.media_upload_authorization_ttl_seconds
    ):
        return UnconfiguredMediaStoragePort()

    from azure.identity.aio import DefaultAzureCredential

    return AzureBlobMediaStorage(
        account_url=settings.media_blob_account_url,
        container_name=settings.media_blob_container_name,
        credential=DefaultAzureCredential(),
        authorization_ttl_seconds=settings.media_upload_authorization_ttl_seconds,
    )


def _media_policy(settings: Settings) -> MediaPolicy:
    if (
        not settings.media_photo_allowed_content_types
        or not settings.media_photo_max_size_bytes
        or not settings.media_video_allowed_content_types
        or not settings.media_video_max_size_bytes
    ):
        return UnconfiguredMediaPolicy()

    return ConfiguredMediaPolicy(
        allowed_content_types={
            "PHOTO": settings.media_photo_allowed_content_types,
            "VIDEO": settings.media_video_allowed_content_types,
        },
        maximum_size_bytes={
            "PHOTO": settings.media_photo_max_size_bytes,
            "VIDEO": settings.media_video_max_size_bytes,
        },
    )


app = create_app()
