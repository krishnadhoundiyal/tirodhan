"""Provider-reported movements are control evidence, not a second payment ledger."""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.payments.accounting import open_exception
from tirodhan.modules.payments.models import (
    CapturedCharge,
    FinancialScanCheckpoint,
    Payment,
    Refund,
    RefundObligation,
    SettlementEvidence,
)
from tirodhan.modules.payments.ports import PaymentProviderUncertainError
from tirodhan.modules.payments.razorpay import RazorpayProvider


@dataclass(frozen=True)
class SettlementMovement:
    provider_entity_id: str
    movement_type: str
    provider_settlement_id: str | None
    provider_payment_id: str | None
    provider_dispute_id: str | None
    amount_minor: int
    currency: str
    debit_minor: int
    credit_minor: int
    fee_minor: int
    tax_minor: int
    settled: bool
    on_hold: bool
    provider_created_at: datetime
    provider_settled_at: datetime | None

    @classmethod
    def parse(cls, entity: dict[str, object]) -> "SettlementMovement":
        try:
            identity, kind, currency = entity["entity_id"], entity["type"], entity["currency"]
            if (
                not isinstance(identity, str)
                or not re.fullmatch(r"[a-z]+_[A-Za-z0-9]{1,190}", identity)
                or kind not in {"payment", "refund", "transfer", "adjustment"}
                or not isinstance(currency, str)
                or not re.fullmatch(r"[A-Z]{3}", currency)
            ):
                raise ValueError
            prefix = {"payment": "pay", "refund": "rfnd", "transfer": "trf"}.get(str(kind))
            if prefix and not identity.startswith(prefix + "_"):
                raise ValueError
            values = [entity[k] for k in ("amount", "debit", "credit", "fee", "tax")]
            if any(type(v) is not int or not 0 <= v <= 2**63 - 1 for v in values):
                raise ValueError
            if type(entity["settled"]) is not bool or type(entity["on_hold"]) is not bool:
                raise ValueError

            def instant(key: str) -> datetime | None:
                value = entity.get(key)
                if value is None:
                    return None
                if type(value) is not int or value < 946684800:
                    raise ValueError
                return datetime.fromtimestamp(value, timezone.utc)

            created = instant("created_at")
            if created is None:
                raise ValueError

            def ref(key: str, prefix: str) -> str | None:
                value = entity.get(key)
                if value is not None and (
                    not isinstance(value, str)
                    or not re.fullmatch(prefix + r"_[A-Za-z0-9]{1,190}", value)
                ):
                    raise ValueError
                return value

            from typing import cast

            money = [cast(int, v) for v in values]
            return cls(
                identity,
                str(kind),
                ref("settlement_id", "setl"),
                ref("payment_id", "pay"),
                ref("dispute_id", "disp"),
                money[0],
                currency,
                money[1],
                money[2],
                money[3],
                money[4],
                bool(entity["settled"]),
                bool(entity["on_hold"]),
                created,
                instant("settled_at"),
            )
        except (KeyError, ValueError, TypeError, OverflowError):
            raise PaymentProviderUncertainError("SETTLEMENT_FACTS_INVALID") from None

    def fields(self) -> dict[str, object]:
        from dataclasses import asdict

        return asdict(self)

    def digest(self) -> str:
        return hashlib.sha256(
            json.dumps(self.fields(), sort_keys=True, default=str).encode()
        ).hexdigest()


async def record_settlement(
    factory: async_sessionmaker[AsyncSession],
    account_key: str,
    movement: SettlementMovement,
    *,
    fee_includes_tax: bool | None = None,
) -> SettlementEvidence:
    async with factory() as session, session.begin():
        charge = await session.scalar(
            select(CapturedCharge).where(
                CapturedCharge.provider == "RAZORPAY",
                CapturedCharge.provider_payment_id
                == (
                    movement.provider_entity_id
                    if movement.movement_type == "payment"
                    else movement.provider_payment_id
                ),
            )
        )
        operation = (
            await session.scalar(
                select(Refund).where(
                    Refund.provider == "RAZORPAY",
                    Refund.provider_refund_id == movement.provider_entity_id,
                )
            )
            if movement.movement_type == "refund"
            else None
        )
        payment_id = charge.payment_id if charge else operation.payment_id if operation else None
        payment = (
            await session.scalar(
                select(Payment).where(Payment.payment_id == payment_id).with_for_update()
            )
            if payment_id
            else None
        )
        if operation is not None:
            # Provider outcomes acquire the same Payment lock. Re-read after it
            # so an earlier unlocked lookup cannot create a stale mismatch.
            await session.refresh(operation)
        classification = "MATCHED"
        if movement.movement_type in {"transfer", "adjustment"}:
            classification = "SETTLEMENT_ADJUSTMENT_REVIEW"
        elif charge is None or (movement.movement_type == "refund" and operation is None):
            classification = "SETTLEMENT_UNKNOWN_" + movement.movement_type.upper()
        elif (
            charge.provider_account_key != account_key
            or charge.currency != movement.currency
            or movement.amount_minor
            != (operation.amount_minor if operation else charge.amount_minor)
            or (
                operation is not None
                and (
                    operation.payment_id != charge.payment_id
                    or operation.captured_charge_id not in {None, charge.captured_charge_id}
                )
            )
            or (operation is not None and operation.status != "SUCCEEDED")
        ):
            classification = "SETTLEMENT_MISMATCH"
        elif movement.provider_dispute_id:
            classification = "SETTLEMENT_DISPUTE_REVIEW"
        elif not movement.settled or movement.on_hold:
            classification = "SETTLEMENT_NOT_DUE"
        # Preserve reported gross/net/fee/tax independently. The public API does
        # not state a universal fee-tax inclusion equation for all movement types.
        # An actual negative payment net or wrong direction is nevertheless a mismatch.
        elif (
            movement.movement_type == "payment"
            and (movement.debit_minor != 0 or movement.credit_minor > movement.amount_minor)
        ) or (
            movement.movement_type == "refund"
            and (movement.credit_minor != 0 or movement.debit_minor < movement.amount_minor)
        ):
            classification = "SETTLEMENT_MISMATCH"
        elif fee_includes_tax is None:
            classification = "MATCHED_GROSS"
        else:
            fee = movement.fee_minor + (0 if fee_includes_tax else movement.tax_minor)
            expected_net = (
                movement.amount_minor - fee
                if movement.movement_type == "payment"
                else movement.amount_minor + fee
            )
            reported_net = (
                movement.credit_minor
                if movement.movement_type == "payment"
                else movement.debit_minor
            )
            if expected_net != reported_net:
                classification = "SETTLEMENT_MISMATCH"
        # A newly matched local operation or newly confirmed fee rule must be
        # assessed again. Keep the earlier observation rather than rewriting it.
        observation_key = hashlib.sha256(
            json.dumps(
                [
                    movement.digest(),
                    fee_includes_tax,
                    classification,
                    str(charge.captured_charge_id) if charge else None,
                    str(operation.refund_id) if operation else None,
                ]
            ).encode()
        ).hexdigest()
        identity = await session.scalar(
            insert(SettlementEvidence)
            .values(
                settlement_evidence_id=new_uuid7(),
                provider_account_key=account_key,
                observation_key=observation_key,
                fee_includes_tax=fee_includes_tax,
                classification=classification,
                captured_charge_id=charge.captured_charge_id if charge else None,
                refund_id=operation.refund_id if operation else None,
                observed_at=utc_now(),
                **movement.fields(),
            )
            .on_conflict_do_nothing(index_elements=["provider_account_key", "observation_key"])
            .returning(SettlementEvidence.settlement_evidence_id)
        )
        evidence = (
            await session.get(SettlementEvidence, identity)
            if identity
            else await session.scalar(
                select(SettlementEvidence).where(
                    SettlementEvidence.provider_account_key == account_key,
                    SettlementEvidence.observation_key == observation_key,
                )
            )
        )
        assert evidence is not None
        if payment and classification not in {"MATCHED", "MATCHED_GROSS", "SETTLEMENT_NOT_DUE"}:
            case = await open_exception(
                session, payment, classification, charge=charge, refund=operation
            )
            case.settlement_evidence_id = evidence.settlement_evidence_id
            obligation = (
                await session.scalar(
                    select(RefundObligation).where(
                        RefundObligation.captured_charge_id == charge.captured_charge_id
                    )
                )
                if charge
                else None
            )
            if obligation:
                obligation.payout_blocked = True
        return evidence


@dataclass(frozen=True)
class ExpectedSettlement:
    """Explicit provider-verified membership supplied by account-specific control.

    No capture timestamp or guessed T+N establishes this membership.
    """

    charge_id: UUID
    refund_id: UUID | None
    settlement_id: str
    due_at: datetime


async def check_expected_settlement(
    factory: async_sessionmaker[AsyncSession],
    account_key: str,
    expected: list[ExpectedSettlement],
    *,
    coverage_verified: bool,
) -> int:
    if len(expected) > 1000:
        raise ValueError("Expected settlement batch exceeds 1000")
    if any(item.due_at.tzinfo is None for item in expected):
        raise ValueError("Expected settlement due dates must be timezone-aware")
    if not coverage_verified:
        return 0  # Public API page exhaustion alone is not report completeness proof.
    missing = 0
    for item in expected:
        if item.due_at > utc_now():
            continue
        async with factory() as session, session.begin():
            charge = await session.get(CapturedCharge, item.charge_id)
            if charge is None or charge.provider_account_key != account_key:
                raise ValueError("Expected settlement account/charge is invalid")
            payment = await session.scalar(
                select(Payment).where(Payment.payment_id == charge.payment_id).with_for_update()
            )
            assert payment is not None
            operation = await session.get(Refund, item.refund_id) if item.refund_id else None
            if item.refund_id and (
                operation is None
                or operation.captured_charge_id != charge.captured_charge_id
                or operation.status != "SUCCEEDED"
            ):
                raise ValueError("Expected refund membership is invalid")
            found = await session.scalar(
                select(SettlementEvidence.settlement_evidence_id)
                .where(
                    SettlementEvidence.provider_account_key == account_key,
                    SettlementEvidence.provider_settlement_id == item.settlement_id,
                    SettlementEvidence.provider_entity_id
                    == (operation.provider_refund_id if operation else charge.provider_payment_id),
                    SettlementEvidence.settled.is_(True),
                    SettlementEvidence.on_hold.is_(False),
                    SettlementEvidence.classification.in_(["MATCHED", "MATCHED_GROSS"]),
                )
                .limit(1)
            )
            if found is None:
                case = await open_exception(
                    session, payment, "SETTLEMENT_EXPECTED_MISSING", charge=charge, refund=operation
                )
                case.expected_settlement_id = item.settlement_id
                case.expected_settlement_due_at = item.due_at
                obligation = await session.scalar(
                    select(RefundObligation).where(
                        RefundObligation.captured_charge_id == charge.captured_charge_id
                    )
                )
                if obligation:
                    obligation.payout_blocked = True
                missing += 1
    return missing


async def reconcile_settlement_day(
    factory: async_sessionmaker[AsyncSession],
    provider: RazorpayProvider,
    day: date,
    *,
    page_budget: int,
    lease_seconds: int,
    fee_includes_tax: bool | None = None,
) -> int:
    if not 1 <= page_budget <= 100 or lease_seconds <= 0:
        raise ValueError("Settlement scan limits are invalid")
    epoch = int(datetime.combine(day, datetime.min.time(), timezone.utc).timestamp())
    kind = f"SETTLEMENT:{day.isoformat()}"
    async with factory() as session, session.begin():
        await session.execute(
            insert(FinancialScanCheckpoint)
            .values(
                financial_scan_checkpoint_id=new_uuid7(),
                provider=provider.provider_code,
                provider_account_key=provider.account_key,
                scan_kind=kind,
                window_start=epoch,
                window_end=epoch + 86400,
            )
            .on_conflict_do_nothing(
                index_elements=["provider", "provider_account_key", "scan_kind"]
            )
        )
    pages = 0
    for _ in range(page_budget):
        async with factory() as session, session.begin():
            checkpoint = await session.scalar(
                select(FinancialScanCheckpoint)
                .where(
                    FinancialScanCheckpoint.provider == provider.provider_code,
                    FinancialScanCheckpoint.provider_account_key == provider.account_key,
                    FinancialScanCheckpoint.scan_kind == kind,
                    or_(
                        FinancialScanCheckpoint.claim_until.is_(None),
                        FinancialScanCheckpoint.claim_until <= utc_now(),
                    ),
                )
                .with_for_update(skip_locked=True)
            )
            if checkpoint is None:
                break
            token = new_uuid7()
            checkpoint.claim_token = token
            checkpoint.claim_until = utc_now() + timedelta(seconds=lease_seconds)
            identity, offset = checkpoint.financial_scan_checkpoint_id, checkpoint.page_offset
        items = await provider.settlement_page(day, offset)
        for item in items:
            await record_settlement(
                factory,
                provider.account_key,
                SettlementMovement.parse(item),
                fee_includes_tax=fee_includes_tax,
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
                continue
            checkpoint.claim_token = None
            checkpoint.claim_until = None
            if len(items) == 1000:
                checkpoint.page_offset += 1000
            else:
                checkpoint.page_offset = 0
                checkpoint.last_exhausted_at = utc_now()
                pages += 1
                break
        pages += 1
    return pages
