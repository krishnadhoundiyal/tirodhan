from fastapi import APIRouter

from tirodhan.api.routes import (
    addresses,
    auth,
    collection_requests,
    customer_reads,
    health,
    manager,
    payments,
    rider,
    serviceability,
)

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(addresses.router)
api_router.include_router(serviceability.router)
api_router.include_router(collection_requests.router)
api_router.include_router(customer_reads.router)
api_router.include_router(payments.router)
api_router.include_router(rider.router)
api_router.include_router(manager.router)
