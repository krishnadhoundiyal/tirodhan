"""Finite inventory/report control job. No provider POST or guessed schedule."""

import asyncio
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from tirodhan.core.config import get_settings
from tirodhan.core.logging import configure_logging
from tirodhan.db.session import create_database_engine, create_session_factory
from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.payments.inventory import InventoryPolicy, discover_inventory
from tirodhan.modules.payments.models import FinancialScanCheckpoint
from tirodhan.modules.payments.runtime import razorpay_runtime
from tirodhan.modules.payments.settlement import reconcile_settlement_day
from tirodhan.workers.financial_reconciliation import configured_policy


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_file_path)
    policy = configured_policy(settings)
    values = (
        settings.financial_inventory_start_epoch,
        settings.financial_inventory_window_seconds,
        settings.financial_inventory_overlap_seconds,
        settings.financial_inventory_visibility_lag_seconds,
        settings.financial_inventory_page_budget,
    )
    if any(v is None for v in values) or not settings.razorpay_account_id:
        raise ValueError("Account inventory policy must be explicitly configured")
    inventory_policy = InventoryPolicy(*(int(v) for v in values if v is not None))
    if settings.financial_settlement_start_date is not None and (
        not settings.financial_settlement_timezone
        or settings.financial_settlement_revisit_days is None
    ):
        raise ValueError("Settlement timezone and revisit policy must be explicitly configured")
    engine = create_database_engine(settings, use_null_pool=True)
    try:
        async with razorpay_runtime(settings) as provider:
            if provider is None:
                raise ValueError("Inventory reconciliation requires configured Razorpay")
            factory = create_session_factory(engine)
            await discover_inventory(factory, provider, inventory_policy, policy, "PAYMENTS")
            await discover_inventory(factory, provider, inventory_policy, policy, "REFUNDS")
            if settings.financial_settlement_start_date is not None:
                first = date.fromisoformat(settings.financial_settlement_start_date)
                zone = ZoneInfo(settings.financial_settlement_timezone or "")
                cutoff = (
                    (utc_now() - timedelta(seconds=inventory_policy.visibility_lag_seconds))
                    .astimezone(zone)
                    .date()
                )
                epoch = int(datetime.combine(first, datetime.min.time(), timezone.utc).timestamp())
                async with factory() as session, session.begin():
                    await session.execute(
                        insert(FinancialScanCheckpoint)
                        .values(
                            financial_scan_checkpoint_id=new_uuid7(),
                            provider=provider.provider_code,
                            provider_account_key=provider.account_key,
                            scan_kind="SETTLEMENT_HISTORY",
                            window_start=epoch,
                            window_end=epoch + 86400,
                        )
                        .on_conflict_do_nothing(
                            index_elements=["provider", "provider_account_key", "scan_kind"]
                        )
                    )
                    history = await session.scalar(
                        select(FinancialScanCheckpoint).where(
                            FinancialScanCheckpoint.provider_account_key == provider.account_key,
                            FinancialScanCheckpoint.scan_kind == "SETTLEMENT_HISTORY",
                        )
                    )
                    assert history is not None
                    history_id, history_epoch = (
                        history.financial_scan_checkpoint_id,
                        history.window_start,
                    )
                day = datetime.fromtimestamp(history_epoch, timezone.utc).date()
                days = {day} if day < cutoff else set()
                for offset in range(1, (settings.financial_settlement_revisit_days or 0) + 1):
                    recent = cutoff - timedelta(days=offset)
                    if recent >= first:
                        days.add(recent)
                for report_day in sorted(days):
                    started_at = utc_now()
                    await reconcile_settlement_day(
                        factory,
                        provider,
                        report_day,
                        page_budget=inventory_policy.page_budget,
                        lease_seconds=policy.lease_seconds,
                        fee_includes_tax=settings.financial_settlement_fee_includes_tax,
                    )
                    if report_day == day:
                        async with factory() as session, session.begin():
                            exhausted = await session.scalar(
                                select(FinancialScanCheckpoint.financial_scan_checkpoint_id).where(
                                    FinancialScanCheckpoint.provider_account_key
                                    == provider.account_key,
                                    FinancialScanCheckpoint.scan_kind
                                    == f"SETTLEMENT:{day.isoformat()}",
                                    FinancialScanCheckpoint.page_offset == 0,
                                    FinancialScanCheckpoint.last_exhausted_at >= started_at,
                                )
                            )
                            current = await session.scalar(
                                select(FinancialScanCheckpoint)
                                .where(
                                    FinancialScanCheckpoint.financial_scan_checkpoint_id
                                    == history_id,
                                )
                                .with_for_update()
                            )
                            if exhausted and current and current.window_start == history_epoch:
                                current.window_start += 86400
                                current.window_end += 86400
    finally:
        await engine.dispose()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
