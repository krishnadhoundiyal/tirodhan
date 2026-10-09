from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from tirodhan.api.router import api_router
from tirodhan.core.config import Settings, get_settings
from tirodhan.core.logging import configure_logging
from tirodhan.db.session import create_database_engine, create_session_factory
from tirodhan.modules.collection_requests.ports import PricingPort, UnconfiguredPricingPort
from tirodhan.modules.collection_requests.scheduling import (
    SchedulingUnavailableError,
    SlotAvailabilityPort,
    UnconfiguredSlotAvailability,
)
from tirodhan.modules.customer_reads.catalogue import ProductMediaPort, UnconfiguredProductMedia
from tirodhan.modules.customer_reads.errors import CustomerReadError
from tirodhan.modules.customers.ports import AddressProtectionNotConfiguredError, AddressProtector
from tirodhan.modules.evidence.azure_media import AzureBlobMediaStorage
from tirodhan.modules.evidence.media_policy import ConfiguredMediaPolicy, UnconfiguredMediaPolicy
from tirodhan.modules.evidence.media_ports import (
    MediaPolicy,
    MediaStoragePort,
    MediaStorageUnavailableError,
    UnconfiguredMediaStoragePort,
)
from tirodhan.modules.identity.kaleyra import KaleyraVerifyOtpProvider
from tirodhan.modules.identity.phone_protection import AesGcmPhoneIdentityProtector
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
from tirodhan.modules.payments.razorpay import razorpay_configured
from tirodhan.modules.payments.runtime import razorpay_runtime
from tirodhan.modules.planning.policy import PlanningConfigurationError
from tirodhan.modules.serviceability.h3_cells import H3CellIdDeriver
from tirodhan.modules.serviceability.ports import (
    CellIdDeriver,
    LocationResolver,
    UnconfiguredLocationResolver,
)
from tirodhan.modules.serviceability.runtime import (
    address_protector_from_settings,
    serviceability_runtime,
)

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    address_protector: AddressProtector | None = None,
    location_resolver: LocationResolver | None = None,
    cell_id_deriver: CellIdDeriver | None = None,
    google_http_client: httpx.AsyncClient | None = None,
    pricing_port: PricingPort | None = None,
    payment_provider: PaymentProvider | None = None,
    payment_http_client: httpx.AsyncClient | None = None,
    otp_provider: OtpProvider | None = None,
    otp_http_client: httpx.AsyncClient | None = None,
    phone_identity_protector: PhoneIdentityProtector | None = None,
    access_token_codec: AccessTokenCodec | None = None,
    media_storage: MediaStoragePort | None = None,
    media_policy: MediaPolicy | None = None,
    slot_availability: SlotAvailabilityPort | None = None,
    product_media: ProductMediaPort | None = None,
) -> FastAPI:
    application_settings = settings or get_settings()
    if payment_provider is None:
        razorpay_configured(application_settings)
    configure_logging(
        application_settings.log_level,
        application_settings.log_file_path,
    )

    # Eager crypto parsing fails before any authentication transaction can commit.
    configured_phone_protector = phone_identity_protector or _phone_protector(application_settings)
    configured_address_protector = address_protector or address_protector_from_settings(
        application_settings
    )
    owns_media_storage = media_storage is None
    configured_media_storage = media_storage or _media_storage(application_settings)
    owns_otp_http_client = (
        otp_provider is None
        and otp_http_client is None
        and _kaleyra_configured(application_settings)
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
            if otp_provider is None and _kaleyra_configured(application_settings):
                runtime_client = otp_http_client or httpx.AsyncClient(
                    transport=httpx.AsyncHTTPTransport(retries=0)
                )
                application.state.otp_http_client = runtime_client
                application.state.otp_provider = _otp_provider(application_settings, runtime_client)
            async with (
                serviceability_runtime(
                    application_settings,
                    client=google_http_client,
                    resolver=location_resolver,
                    protector=configured_address_protector,
                ) as runtime,
                razorpay_runtime(
                    application_settings,
                    client=payment_http_client,
                    enabled=payment_provider is None,
                ) as financial_runtime,
            ):
                application.state.location_resolver = runtime.resolver
                if financial_runtime is not None:
                    application.state.payment_provider = financial_runtime
                yield
        finally:
            await engine.dispose()

            if owns_otp_http_client:
                await application.state.otp_http_client.aclose()

            if owns_media_storage:
                media_runtime = getattr(application.state, "media_storage", None)
                if isinstance(media_runtime, AzureBlobMediaStorage):
                    await media_runtime.close()

            logger.info("application_stopped")

    application = FastAPI(
        title=application_settings.app_name,
        version="0.1.0",
        debug=application_settings.debug,
        lifespan=lifespan,
    )
    application.state.settings = application_settings
    application.state.address_protector = configured_address_protector
    application.state.location_resolver = location_resolver or UnconfiguredLocationResolver()
    application.state.cell_id_deriver = cell_id_deriver or H3CellIdDeriver()
    application.state.pricing_port = pricing_port or UnconfiguredPricingPort()
    application.state.payment_provider = payment_provider or UnconfiguredPaymentProvider()
    application.state.otp_provider = otp_provider or UnconfiguredOtpProvider()
    application.state.phone_identity_protector = configured_phone_protector
    application.state.access_token_codec = access_token_codec or _token_codec(application_settings)
    application.state.media_storage = configured_media_storage
    application.state.media_policy = media_policy or _media_policy(application_settings)
    application.state.slot_availability = slot_availability or UnconfiguredSlotAvailability()
    application.state.product_media = product_media or (
        configured_media_storage
        if isinstance(configured_media_storage, AzureBlobMediaStorage)
        else UnconfiguredProductMedia()
    )

    @application.exception_handler(CustomerReadError)
    async def customer_read_error(request: Request, error: CustomerReadError) -> JSONResponse:
        return JSONResponse(
            status_code=error.status,
            content={"error": {"code": error.code}},
            headers={"Cache-Control": "private, no-store"},
        )

    async def unavailable_runtime(request: Request, error: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={"error": {"code": "NOT_ELIGIBLE"}},
            headers={"Cache-Control": "private, no-store"},
        )

    for error_type in (
        SchedulingUnavailableError,
        PlanningConfigurationError,
        AddressProtectionNotConfiguredError,
        MediaStorageUnavailableError,
    ):
        application.add_exception_handler(error_type, unavailable_runtime)
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


def _kaleyra_configured(settings: Settings) -> bool:
    return all(
        value is not None
        for value in (
            settings.kaleyra_api_domain,
            settings.kaleyra_sid,
            settings.kaleyra_api_key,
            settings.kaleyra_verify_flow_id,
            settings.kaleyra_http_timeout_seconds,
        )
    )


def _otp_provider(settings: Settings, client: httpx.AsyncClient) -> OtpProvider:
    assert settings.kaleyra_api_domain is not None
    assert settings.kaleyra_sid is not None
    assert settings.kaleyra_api_key is not None
    assert settings.kaleyra_verify_flow_id is not None
    assert settings.kaleyra_http_timeout_seconds is not None
    return KaleyraVerifyOtpProvider(
        api_domain=settings.kaleyra_api_domain,
        sid=settings.kaleyra_sid,
        api_key=settings.kaleyra_api_key.get_secret_value(),
        flow_id=settings.kaleyra_verify_flow_id,
        timeout_seconds=settings.kaleyra_http_timeout_seconds,
        client=client,
    )


def _phone_protector(settings: Settings) -> PhoneIdentityProtector:
    if (
        settings.phone_encryption_active_key_id is None
        or settings.phone_encryption_keys is None
        or settings.phone_lookup_hmac_key is None
    ):
        return UnconfiguredPhoneIdentityProtector()
    return AesGcmPhoneIdentityProtector.from_configuration(
        active_key_id=settings.phone_encryption_active_key_id,
        encryption_keys_json=settings.phone_encryption_keys.get_secret_value(),
        lookup_hmac_key_base64=settings.phone_lookup_hmac_key.get_secret_value(),
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
