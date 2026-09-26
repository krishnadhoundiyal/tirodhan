import pytest

from tirodhan.modules.collection_requests.ports import (
    PricingNotConfiguredError,
    UnconfiguredPricingPort,
)
from tirodhan.modules.customers.ports import (
    AddressProtectionNotConfiguredError,
    UnconfiguredAddressProtector,
)
from tirodhan.modules.customers.service import GeoPoint
from tirodhan.modules.payments.ports import (
    PaymentProviderNotConfiguredError,
    UnconfiguredPaymentProvider,
)
from tirodhan.modules.serviceability.ports import (
    ServiceabilityResolverNotConfiguredError,
    UnconfiguredCellIdDeriver,
    UnconfiguredLocationResolver,
)


@pytest.mark.asyncio
async def test_unconfigured_address_protection_fails_explicitly() -> None:
    with pytest.raises(AddressProtectionNotConfiguredError):
        await UnconfiguredAddressProtector().protect("sensitive address")


@pytest.mark.asyncio
async def test_unconfigured_serviceability_adapters_do_not_fabricate_results() -> None:
    point = GeoPoint(latitude=28.6, longitude=77.2)
    with pytest.raises(ServiceabilityResolverNotConfiguredError):
        await UnconfiguredLocationResolver().resolve(
            address="sensitive address", supplied_location=point
        )
    with pytest.raises(ServiceabilityResolverNotConfiguredError):
        await UnconfiguredCellIdDeriver().derive(point)


@pytest.mark.asyncio
async def test_unconfigured_pricing_fails_without_zero_quote() -> None:
    with pytest.raises(PricingNotConfiguredError):
        await UnconfiguredPricingPort().quote([])


def test_unconfigured_payment_provider_fails_explicitly() -> None:
    with pytest.raises(PaymentProviderNotConfiguredError):
        _ = UnconfiguredPaymentProvider().provider_code
