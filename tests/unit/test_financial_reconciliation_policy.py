from unittest.mock import AsyncMock

import pytest

from tirodhan.core.config import Settings
from tirodhan.modules.payments.reconciliation import ReconciliationPolicy
from tirodhan.workers import financial_reconciliation as worker


@pytest.mark.asyncio
async def test_unconfigured_existing_job_has_no_provider_or_database_side_effect(monkeypatch):
    runtime = AsyncMock()
    monkeypatch.setattr(worker, "razorpay_runtime", runtime)
    await worker.run_if_configured(Settings(_env_file=None))
    runtime.assert_not_called()


@pytest.mark.asyncio
async def test_partially_configured_job_fails_before_provider_io(monkeypatch):
    runtime = AsyncMock()
    monkeypatch.setattr(worker, "razorpay_runtime", runtime)
    with pytest.raises(ValueError, match="explicitly configured"):
        await worker.run_if_configured(
            Settings(_env_file=None, financial_reconciliation_batch_size=10)
        )
    runtime.assert_not_called()


@pytest.mark.parametrize("batch,interval,maximum", [(0, 60, 600), (1001, 60, 600), (10, 60, 30)])
def test_operational_policy_rejects_unbounded_or_invalid_work(batch, interval, maximum):
    with pytest.raises(ValueError):
        ReconciliationPolicy(batch, interval, maximum, 120, 600, 86400, 30)


def test_refund_failure_finality_is_disabled_without_account_confirmation():
    assert Settings(_env_file=None).razorpay_normal_refund_failure_finality_confirmed is False
