"""Finite ACA Job entry point. Scheduling/rate limits require operational configuration."""

import asyncio

from tirodhan.core.config import Settings, get_settings
from tirodhan.core.logging import configure_logging
from tirodhan.db.session import create_database_engine, create_session_factory
from tirodhan.modules.payments.reconciliation import ReconciliationPolicy, reconcile_batch
from tirodhan.modules.payments.runtime import razorpay_runtime


def configured_policy(settings: Settings) -> ReconciliationPolicy:
    values = [
        settings.financial_reconciliation_batch_size,
        settings.financial_reconciliation_interval_seconds,
        settings.financial_reconciliation_max_backoff_seconds,
        settings.financial_reconciliation_lease_seconds,
        settings.financial_unresolved_threshold_seconds,
        settings.command_idempotency_ttl_seconds,
        settings.planning_lead_time_minutes,
    ]
    if any(value is None for value in values):
        raise ValueError("Financial reconciliation scheduling policy must be explicitly configured")
    return ReconciliationPolicy(*(int(value) for value in values if value is not None))


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_file_path)
    policy = configured_policy(settings)
    engine = create_database_engine(settings, use_null_pool=True)
    try:
        async with razorpay_runtime(settings) as provider:
            if provider is None:
                raise ValueError("Financial reconciliation requires configured Razorpay")
            await reconcile_batch(create_session_factory(engine), provider, policy)
    finally:
        await engine.dispose()


async def run_if_configured(settings: Settings) -> None:
    """Reuse the existing finite expiry Job after its transaction has closed."""
    scheduling = (
        settings.financial_reconciliation_batch_size,
        settings.financial_reconciliation_interval_seconds,
        settings.financial_reconciliation_max_backoff_seconds,
        settings.financial_reconciliation_lease_seconds,
        settings.financial_unresolved_threshold_seconds,
    )
    if all(value is None for value in scheduling):
        return
    policy = configured_policy(settings)
    async with razorpay_runtime(settings) as provider:
        if provider is None:
            raise ValueError("Financial reconciliation requires configured Razorpay")
        engine = create_database_engine(settings, use_null_pool=True)
        try:
            await reconcile_batch(create_session_factory(engine), provider, policy)
        finally:
            await engine.dispose()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
