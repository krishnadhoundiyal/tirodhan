"""Bounded provider-to-local discovery with durable, account-bound page progress."""

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Literal

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.payments.models import FinancialScanCheckpoint
from tirodhan.modules.payments.ports import PaymentProviderUncertainError
from tirodhan.modules.payments.razorpay import RazorpayProvider
from tirodhan.modules.payments.reconciliation import ReconciliationPolicy
from tirodhan.modules.payments.service import process_authenticated_payment_event


@dataclass(frozen=True)
class InventoryPolicy:
    start_epoch: int
    window_seconds: int
    overlap_seconds: int
    visibility_lag_seconds: int
    page_budget: int

    def __post_init__(self) -> None:
        if (
            self.start_epoch < 946684800
            or not 0 < self.overlap_seconds < self.window_seconds
            or self.visibility_lag_seconds <= 0
            or not 1 <= self.page_budget <= 100
        ):
            raise ValueError("Financial inventory policy is invalid")


async def discover_inventory(
    factory: async_sessionmaker[AsyncSession],
    provider: RazorpayProvider,
    policy: InventoryPolicy,
    financial_policy: ReconciliationPolicy,
    kind: Literal["PAYMENTS", "REFUNDS"],
) -> int:
    cutoff = int(utc_now().timestamp()) - policy.visibility_lag_seconds
    if cutoff <= policy.start_epoch:
        return 0
    async with factory() as session, session.begin():
        await session.execute(
            insert(FinancialScanCheckpoint)
            .values(
                financial_scan_checkpoint_id=new_uuid7(),
                provider=provider.provider_code,
                provider_account_key=provider.account_key,
                scan_kind=kind,
                window_start=policy.start_epoch,
                window_end=min(cutoff, policy.start_epoch + policy.window_seconds),
            )
            .on_conflict_do_nothing(
                index_elements=["provider", "provider_account_key", "scan_kind"]
            )
        )
    completed_pages = 0
    for _ in range(policy.page_budget):
        now = utc_now()
        async with factory() as session, session.begin():
            checkpoint = await session.scalar(
                select(FinancialScanCheckpoint)
                .where(
                    FinancialScanCheckpoint.provider == provider.provider_code,
                    FinancialScanCheckpoint.provider_account_key == provider.account_key,
                    FinancialScanCheckpoint.scan_kind == kind,
                    or_(
                        FinancialScanCheckpoint.claim_until.is_(None),
                        FinancialScanCheckpoint.claim_until <= now,
                    ),
                )
                .with_for_update(skip_locked=True)
            )
            if checkpoint is None:
                break
            token = new_uuid7()
            checkpoint.claim_token = token
            checkpoint.claim_until = now + timedelta(seconds=financial_policy.lease_seconds)
            identity = checkpoint.financial_scan_checkpoint_id
            start, end, offset = (
                checkpoint.window_start,
                checkpoint.window_end,
                checkpoint.page_offset,
            )
        try:
            items = await provider.inventory_page(kind, start, end, offset)
            minimal = [provider.minimal_entity(item) for item in items]
            # Repeated provider IDs are observations of one operation. Changed
            # facts get a different evidence identity and the shared state machine.
            for item, facts in zip(items, minimal, strict=True):
                digest = hashlib.sha256(
                    json.dumps([provider.account_key, facts], sort_keys=True).encode()
                ).hexdigest()
                observation = provider._observation(item)
                if kind == "REFUNDS" and observation.refund_id is None:
                    note = facts["tirodhan_refund_id"]
                    receipt = facts["receipt"]
                    from uuid import UUID

                    try:
                        internal = UUID(note) if isinstance(note, str) else None
                    except ValueError:
                        internal = None
                    if internal and receipt == f"rf_{internal.hex}":
                        observation = replace(observation, refund_id=internal)
                observation = replace(
                    observation,
                    external_event_id=f"inventory:{digest}",
                    event_type="refund.inventory" if kind == "REFUNDS" else "payment.inventory",
                    evidence_source="API_INVENTORY",
                    definitive_non_payable=False,
                )
                await process_authenticated_payment_event(
                    factory,
                    observation,
                    payload_hash=bytes.fromhex(digest),
                    planning_lead_time_minutes=financial_policy.planning_lead_time_minutes,
                    idempotency_expires_at=utc_now()
                    + timedelta(seconds=financial_policy.command_ttl_seconds),
                )
            async with factory() as session, session.begin():
                checkpoint = await session.scalar(
                    select(FinancialScanCheckpoint)
                    .where(
                        FinancialScanCheckpoint.financial_scan_checkpoint_id == identity,
                        FinancialScanCheckpoint.claim_token == token,
                    )
                    .with_for_update()
                )
                if checkpoint is None:
                    continue  # Another claimant owns scheduling; observations remain valid.
                checkpoint.pass_digest = hashlib.sha256(
                    checkpoint.pass_digest.encode() + json.dumps(minimal, sort_keys=True).encode()
                ).hexdigest()
                checkpoint.claim_token = None
                checkpoint.claim_until = None
                if len(items) == 100:
                    checkpoint.page_offset += 100
                else:
                    # Exhaustion is API coverage, not a provider-guaranteed snapshot.
                    # Repeat the entire closed window before advancing; overlap guards
                    # boundary visibility. A changed pass remains explicitly incomplete.
                    stable = checkpoint.previous_digest == checkpoint.pass_digest
                    checkpoint.previous_digest = checkpoint.pass_digest
                    checkpoint.pass_digest = ""
                    checkpoint.page_offset = 0
                    if stable:
                        checkpoint.last_exhausted_at = utc_now()
                        checkpoint.window_start = max(
                            policy.start_epoch, end - policy.overlap_seconds
                        )
                        checkpoint.window_end = min(
                            cutoff, checkpoint.window_start + policy.window_seconds
                        )
                        checkpoint.previous_digest = None
            completed_pages += 1
        except PaymentProviderUncertainError:
            async with factory() as session, session.begin():
                checkpoint = await session.scalar(
                    select(FinancialScanCheckpoint)
                    .where(
                        FinancialScanCheckpoint.financial_scan_checkpoint_id == identity,
                        FinancialScanCheckpoint.claim_token == token,
                    )
                    .with_for_update()
                )
                if checkpoint:
                    checkpoint.claim_token = None
                    checkpoint.claim_until = utc_now() + timedelta(
                        seconds=financial_policy.interval_seconds
                    )
            break
    return completed_pages
