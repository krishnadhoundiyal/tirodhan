from fastapi import APIRouter

from tirodhan.api.routes import addresses, health, serviceability

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(addresses.router)
api_router.include_router(serviceability.router)
