from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from tirodhan.api.router import api_router
from tirodhan.core.config import Settings, get_settings
from tirodhan.core.logging import configure_logging
from tirodhan.db.session import create_database_engine, create_session_factory

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    application_settings = settings or get_settings()
    configure_logging(application_settings.log_level)

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
    application.include_router(api_router)
    return application


app = create_app()
