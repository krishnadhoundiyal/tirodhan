from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx

from tirodhan.core.config import Settings
from tirodhan.modules.payments.razorpay import RazorpayProvider, razorpay_configured


@asynccontextmanager
async def razorpay_runtime(
    settings: Settings, *, client: httpx.AsyncClient | None = None, enabled: bool = True
) -> AsyncIterator[RazorpayProvider | None]:
    if not enabled or not razorpay_configured(settings):
        yield None
        return
    owned = client is None
    runtime_client = client or httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(retries=0))
    try:
        yield RazorpayProvider(settings, client=runtime_client)
    finally:
        if owned:
            await runtime_client.aclose()
