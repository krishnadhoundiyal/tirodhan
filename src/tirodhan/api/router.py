from fastapi import APIRouter

from tirodhan.api.routes import health

api_router = APIRouter()
api_router.include_router(health.router)
