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
    application.include_router(api_router)
    return application


app = create_app()
