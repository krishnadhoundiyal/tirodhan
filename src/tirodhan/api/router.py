from fastapi import APIRouter

from tirodhan.api.routes import addresses, collection_requests, health, payments, serviceability

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(addresses.router)
api_router.include_router(serviceability.router)
api_router.include_router(collection_requests.router)
api_router.include_router(payments.router)
