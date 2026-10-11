"""Manager-only evidence inquiry and explicitly authorized financial recovery."""

from datetime import date, datetime
from typing import Annotated, Any, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.api.dependencies import get_database_session, get_session_factory, require_role
from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.identity.service import AuthenticatedPrincipal
from tirodhan.modules.payments.manager_recovery import RecoveryAction, recover_financial_record
from tirodhan.modules.payments.models import (
    CapturedCharge,
    FinancialAudit,
    FinancialException,
    FinancialScanCheckpoint,
    Payment,
    PaymentAttempt,
    PaymentProviderEvent,
    Refund,
    RefundObligation,
    SettlementEvidence,
)
from tirodhan.modules.payments.ports import (
    FinancialInquiryProvider,
    PaymentProviderNotConfiguredError,
    PaymentProviderUncertainError,
)
from tirodhan.modules.payments.reconciliation import approve_refund, manager_inquiry
from tirodhan.modules.payments.refunds import RefundConflictError
from tirodhan.modules.reliability.models import OutboxEvent
from tirodhan.modules.reliability.primitives import IdempotencyKeyConflictError
from tirodhan.workers.financial_reconciliation import configured_policy

router = APIRouter(prefix="/v1/manager/financial", tags=["manager finances"])
Manager = Annotated[AuthenticatedPrincipal, Depends(require_role("MANAGER"))]
ReadSession = Annotated[AsyncSession, Depends(get_database_session)]
Factory = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]


class InquiryCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command_id: UUID
    reported_payment_id: str | None = Field(default=None, pattern=r"^pay_[A-Za-z0-9]{1,190}$")


class RefundApprovalCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command_id: UUID
    failed_refund_id: UUID | None = None


class AuditResponse(BaseModel):
    audit_id: UUID
    command_id: UUID
    actor_user_id: UUID
    payment_id: UUID | None
    refund_id: UUID | None
    evidence_id: UUID | None
    settlement_evidence_id: UUID | None
    financial_exception_id: UUID | None
    action: str
    result_code: str
    created_at: datetime


def audit_response(audit: FinancialAudit) -> AuditResponse:
    return AuditResponse(
        audit_id=audit.financial_audit_id,
        command_id=audit.command_id,
        actor_user_id=audit.actor_user_id,
        payment_id=audit.payment_id,
        refund_id=audit.refund_id,
        evidence_id=audit.evidence_id,
        settlement_evidence_id=audit.settlement_evidence_id,
        financial_exception_id=audit.financial_exception_id,
        action=audit.action,
        result_code=audit.result_code,
        created_at=audit.created_at,
    )


def provider(request: Request) -> FinancialInquiryProvider:
    instance = request.app.state.payment_provider
    if not callable(getattr(instance, "inquire_payment", None)):
        raise HTTPException(503, "Financial inquiry provider is not configured")
    return cast(FinancialInquiryProvider, instance)


@router.get("/provider-events")
async def provider_evidence_inventory(
    manager: Manager,
    session: ReadSession,
    after: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> dict[str, Any]:
    query = select(PaymentProviderEvent).where(
        PaymentProviderEvent.processing_status.in_(
            ["UNMATCHED", "RECONCILIATION", "RECONCILIATION_REQUIRED"]
        )
    )
    if after:
        query = query.where(PaymentProviderEvent.payment_provider_event_id > after)
    rows = list(
        await session.scalars(
            query.order_by(PaymentProviderEvent.payment_provider_event_id).limit(limit + 1)
        )
    )
    return dict(
        evidence=[
            dict(
                evidence_id=r.payment_provider_event_id,
                provider=r.provider,
                provider_account_key=r.provider_account_key,
                external_event_id=r.external_event_id,
                event_type=r.event_type,
                evidence_source=r.evidence_source,
                provider_order_id=r.provider_order_id,
                provider_payment_id=r.provider_payment_id,
                provider_refund_id=r.provider_refund_id,
                provider_dispute_id=r.provider_dispute_id,
                amount_minor=r.amount_minor,
                currency=r.currency,
                decision=r.processing_status,
                reason=r.failure_code,
                contradicted_event_id=r.contradicted_event_id,
                received_at=r.received_at,
            )
            for r in rows[:limit]
        ],
        next_cursor=rows[limit - 1].payment_provider_event_id if len(rows) > limit else None,
    )


@router.get("/settlement-evidence")
async def settlement_inventory(
    manager: Manager,
    session: ReadSession,
    after: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> dict[str, Any]:
    query = select(SettlementEvidence)
    if after:
        query = query.where(SettlementEvidence.settlement_evidence_id > after)
    rows = list(
        await session.scalars(
            query.order_by(SettlementEvidence.settlement_evidence_id).limit(limit + 1)
        )
    )
    return dict(
        movements=[
            dict(
                evidence_id=r.settlement_evidence_id,
                provider_account_key=r.provider_account_key,
                provider_entity_id=r.provider_entity_id,
                movement_type=r.movement_type,
                provider_settlement_id=r.provider_settlement_id,
                amount_minor=r.amount_minor,
                currency=r.currency,
                credit_minor=r.credit_minor,
                debit_minor=r.debit_minor,
                fee_minor=r.fee_minor,
                tax_minor=r.tax_minor,
                classification=r.classification,
                observed_at=r.observed_at,
                provider_payment_id=r.provider_payment_id,
                provider_dispute_id=r.provider_dispute_id,
                captured_charge_id=r.captured_charge_id,
                refund_id=r.refund_id,
                settled=r.settled,
                on_hold=r.on_hold,
                provider_created_at=r.provider_created_at,
                provider_settled_at=r.provider_settled_at,
            )
            for r in rows[:limit]
        ],
        next_cursor=rows[limit - 1].settlement_evidence_id if len(rows) > limit else None,
    )


@router.get("/payments")
async def inventory(
    manager: Manager,
    session: ReadSession,
    after: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> dict[str, Any]:
    query = (
        select(Payment, CollectionRequest.status)
        .join(CollectionRequest, CollectionRequest.request_id == Payment.request_id)
        .where(
            or_(
                Payment.status == "PENDING",
                exists().where(
                    FinancialException.payment_id == Payment.payment_id,
                    FinancialException.status == "OPEN",
                ),
                exists().where(
                    Refund.payment_id == Payment.payment_id,
                    Refund.status.in_(
                        ["PENDING", "PROCESSING", "SUBMITTED", "INITIATION_UNCERTAIN"]
                    ),
                ),
                exists().where(
                    Refund.payment_id == Payment.payment_id,
                    Refund.refund_obligation_id.is_(None),
                    Refund.status == "FAILED",
                ),
                exists(
                    select(RefundObligation.refund_obligation_id)
                    .join(CapturedCharge)
                    .where(
                        CapturedCharge.payment_id == Payment.payment_id,
                        or_(
                            RefundObligation.payout_blocked.is_(True),
                            RefundObligation.amount_minor
                            > select(func.coalesce(func.sum(Refund.amount_minor), 0))
                            .where(
                                Refund.refund_obligation_id
                                == RefundObligation.refund_obligation_id,
                                Refund.status == "SUCCEEDED",
                            )
                            .correlate(RefundObligation)
                            .scalar_subquery(),
                        ),
                    )
                ),
                (CollectionRequest.status == "CANCELLED")
                & ~exists().where(Refund.payment_id == Payment.payment_id),
            )
        )
    )
    if after:
        query = query.where(Payment.payment_id > after)
    rows = (await session.execute(query.order_by(Payment.payment_id).limit(limit + 1))).all()
    return {
        "payments": [
            dict(
                payment_id=p.payment_id,
                request_id=p.request_id,
                status=p.status,
                booking_status=status,
                amount_minor=p.amount_minor,
                currency=p.currency,
            )
            for p, status in rows[:limit]
        ],
        "next_after": str(rows[limit - 1][0].payment_id) if len(rows) > limit else None,
    }


@router.get("/exceptions")
async def exception_queue(
    manager: Manager,
    session: ReadSession,
    after: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> dict[str, Any]:
    query = select(FinancialException).where(FinancialException.status == "OPEN")
    if after:
        query = query.where(FinancialException.financial_exception_id > after)
    rows = list(
        await session.scalars(
            query.order_by(FinancialException.financial_exception_id).limit(limit + 1)
        )
    )
    return dict(
        cases=[
            dict(
                case_id=r.financial_exception_id,
                payment_id=r.payment_id,
                refund_id=r.refund_id,
                captured_charge_id=r.captured_charge_id,
                evidence_id=r.evidence_id,
                settlement_evidence_id=r.settlement_evidence_id,
                reason=r.reason_code,
                first_detected_at=r.detected_at,
                last_escalated_at=r.last_escalated_at,
                occurrence_count=r.occurrence_count,
                investigation=f"/v1/manager/financial/exceptions/{r.financial_exception_id}/resolve",
                required_role="MANAGER",
            )
            for r in rows[:limit]
        ],
        next_cursor=rows[limit - 1].financial_exception_id if len(rows) > limit else None,
    )


@router.get("/scan-checkpoints")
async def checkpoint_queue(
    manager: Manager,
    session: ReadSession,
    after: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> dict[str, Any]:
    query = select(FinancialScanCheckpoint)
    if after:
        query = query.where(FinancialScanCheckpoint.financial_scan_checkpoint_id > after)
    rows = list(
        await session.scalars(
            query.order_by(FinancialScanCheckpoint.financial_scan_checkpoint_id).limit(limit + 1)
        )
    )
    return dict(
        checkpoints=[
            dict(
                checkpoint_id=r.financial_scan_checkpoint_id,
                account_key=r.provider_account_key,
                scan_kind=r.scan_kind,
                window_start=r.window_start,
                window_end=r.window_end,
                page_offset=r.page_offset,
                last_checked_at=r.last_checked_at,
                last_exhausted_at=r.last_exhausted_at,
                last_error_code=r.last_error_code,
                claim_until=r.claim_until,
                required_action=(
                    "Repair provider access or report; rerun configured bounded inventory job"
                ),
                required_role="OPERATIONS",
            )
            for r in rows[:limit]
        ],
        next_cursor=rows[limit - 1].financial_scan_checkpoint_id if len(rows) > limit else None,
    )


@router.get("/audit")
async def audit_inventory(
    manager: Manager,
    session: ReadSession,
    after: UUID | None = None,
    settlement_evidence_id: UUID | None = None,
    evidence_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> dict[str, Any]:
    query = select(FinancialAudit)
    if after:
        query = query.where(FinancialAudit.financial_audit_id > after)
    if settlement_evidence_id:
        query = query.where(FinancialAudit.settlement_evidence_id == settlement_evidence_id)
    if evidence_id:
        query = query.where(FinancialAudit.evidence_id == evidence_id)
    rows = list(
        await session.scalars(query.order_by(FinancialAudit.financial_audit_id).limit(limit + 1))
    )
    return dict(
        audits=[audit_response(r).model_dump() for r in rows[:limit]],
        next_cursor=rows[limit - 1].financial_audit_id if len(rows) > limit else None,
    )


@router.get("/refund-deliveries")
async def refund_delivery_queue(
    manager: Manager,
    session: ReadSession,
    after: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> dict[str, Any]:
    query = select(OutboxEvent).where(OutboxEvent.event_type == "RefundRequested")
    if after:
        query = query.where(OutboxEvent.outbox_event_id > after)
    rows = list(await session.scalars(query.order_by(OutboxEvent.outbox_event_id).limit(limit + 1)))
    return dict(
        deliveries=[
            dict(
                event_id=r.outbox_event_id,
                refund_id=r.aggregate_id,
                status=r.status,
                created_at=r.created_at,
                available_at=r.available_at,
                published_at=r.published_at,
                publish_attempt_count=r.publish_attempt_count,
                recovery=f"/v1/manager/financial/refunds/{r.aggregate_id}/recover-original",
            )
            for r in rows[:limit]
        ],
        next_cursor=rows[limit - 1].outbox_event_id if len(rows) > limit else None,
    )


@router.get("/payments/{payment_id}")
async def detail(
    payment_id: UUID,
    manager: Manager,
    session: ReadSession,
    section: Literal[
        "attempts",
        "charges",
        "refunds",
        "exceptions",
        "audit",
        "obligations",
        "provider_events",
        "settlement_evidence",
    ]
    | None = None,
    after: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> dict[str, Any]:
    if after and not section:
        raise HTTPException(422, "A continuation cursor requires a section")
    payment = await session.get(Payment, payment_id)
    if payment is None:
        raise HTTPException(404, "Payment not found")
    result: dict[str, Any] = {
        "payment_id": payment_id,
        "request_id": payment.request_id,
        "status": payment.status,
    }
    projections = [
        (
            "attempts",
            PaymentAttempt,
            PaymentAttempt.payment_id == payment_id,
            [
                "payment_attempt_id",
                "status",
                "provider",
                "provider_order_id",
                "provider_payment_id",
                "created_at",
                "next_check_at",
                "check_count",
                "claim_until",
            ],
        ),
        (
            "charges",
            CapturedCharge,
            CapturedCharge.payment_id == payment_id,
            [
                "captured_charge_id",
                "payment_attempt_id",
                "provider_payment_id",
                "amount_minor",
                "currency",
                "evidence_id",
            ],
        ),
        (
            "refunds",
            Refund,
            Refund.payment_id == payment_id,
            [
                "refund_id",
                "captured_charge_id",
                "refund_obligation_id",
                "amount_minor",
                "status",
                "non_payable_verified_at",
                "failure_evidence_id",
                "provider_refund_id",
                "provider_idempotency_key",
                "created_at",
                "processing_started_at",
                "next_check_at",
                "check_count",
                "claim_until",
            ],
        ),
        (
            "exceptions",
            FinancialException,
            FinancialException.payment_id == payment_id,
            [
                "financial_exception_id",
                "captured_charge_id",
                "reason_code",
                "status",
                "evidence_id",
                "settlement_evidence_id",
                "expected_settlement_id",
                "expected_settlement_due_at",
                "refund_id",
                "created_at",
                "detected_at",
                "resolved_at",
                "occurrence_count",
                "last_escalated_at",
            ],
        ),
        (
            "audit",
            FinancialAudit,
            FinancialAudit.payment_id == payment_id,
            [
                "financial_audit_id",
                "command_id",
                "actor_user_id",
                "action",
                "result_code",
                "evidence_id",
                "submitted_reference",
                "created_at",
                "refund_id",
                "financial_exception_id",
                "settlement_evidence_id",
            ],
        ),
    ]
    charge_query = select(CapturedCharge.captured_charge_id).where(
        CapturedCharge.payment_id == payment_id
    )
    refund_query = select(Refund.refund_id).where(Refund.payment_id == payment_id)
    projections.extend(
        [
            (
                "obligations",
                RefundObligation,
                RefundObligation.captured_charge_id.in_(charge_query),
                [
                    "refund_obligation_id",
                    "captured_charge_id",
                    "amount_minor",
                    "reason_code",
                    "payout_blocked",
                    "created_at",
                ],
            ),
            (
                "provider_events",
                PaymentProviderEvent,
                or_(
                    PaymentProviderEvent.payment_attempt_id.in_(
                        select(PaymentAttempt.payment_attempt_id).where(
                            PaymentAttempt.payment_id == payment_id
                        )
                    ),
                    PaymentProviderEvent.refund_id.in_(refund_query),
                    PaymentProviderEvent.provider_payment_id.in_(
                        select(CapturedCharge.provider_payment_id).where(
                            CapturedCharge.payment_id == payment_id
                        )
                    ),
                ),
                [
                    "payment_provider_event_id",
                    "external_event_id",
                    "event_type",
                    "evidence_source",
                    "provider_account_key",
                    "provider_order_id",
                    "provider_payment_id",
                    "provider_refund_id",
                    "provider_dispute_id",
                    "observed_outcome",
                    "amount_minor",
                    "currency",
                    "processing_status",
                    "failure_code",
                    "received_at",
                    "processed_at",
                    "contradicted_event_id",
                ],
            ),
            (
                "settlement_evidence",
                SettlementEvidence,
                or_(
                    SettlementEvidence.captured_charge_id.in_(charge_query),
                    SettlementEvidence.refund_id.in_(refund_query),
                    SettlementEvidence.provider_payment_id.in_(
                        select(CapturedCharge.provider_payment_id).where(
                            CapturedCharge.payment_id == payment_id
                        )
                    ),
                    (SettlementEvidence.movement_type == "payment")
                    & SettlementEvidence.provider_entity_id.in_(
                        select(CapturedCharge.provider_payment_id).where(
                            CapturedCharge.payment_id == payment_id
                        )
                    ),
                ),
                [
                    "settlement_evidence_id",
                    "provider_entity_id",
                    "provider_payment_id",
                    "provider_dispute_id",
                    "provider_settlement_id",
                    "amount_minor",
                    "currency",
                    "debit_minor",
                    "credit_minor",
                    "fee_minor",
                    "tax_minor",
                    "settled",
                    "on_hold",
                    "observed_at",
                    "classification",
                ],
            ),
        ]
    )
    result["truncated"] = []
    result["cursors"] = {}
    for name, model, condition, fields in projections:
        if section and name != section:
            continue
        identity = getattr(model, fields[0])
        query = select(model).where(condition).order_by(identity).limit(limit + 1)
        if after:
            query = query.where(identity > after)
        rows = list(await session.scalars(query))
        result[name] = [{field: getattr(row, field) for field in fields} for row in rows[:limit]]
        result["cursors"][name] = getattr(rows[limit - 1], fields[0]) if len(rows) > limit else None
        if len(rows) > limit:
            result["truncated"].append(name)
    obligations = result.get("obligations", [])
    paid_rows = (
        await session.execute(
            select(Refund.refund_obligation_id, func.sum(Refund.amount_minor))
            .where(
                Refund.refund_obligation_id.in_([o["refund_obligation_id"] for o in obligations]),
                Refund.status == "SUCCEEDED",
            )
            .group_by(Refund.refund_obligation_id)
        )
    ).all()
    paid_by_obligation = {key: int(value) for key, value in paid_rows}
    for obligation in obligations:
        paid = int(paid_by_obligation.get(obligation["refund_obligation_id"], 0))
        obligation["outstanding_minor"] = max(0, obligation["amount_minor"] - paid)
        obligation["overpaid_minor"] = max(0, paid - obligation["amount_minor"])
    event_ids = {c["evidence_id"] for c in result.get("charges", [])} | {
        a["evidence_id"] for a in result.get("audit", []) if a["evidence_id"]
    }
    events = list(
        await session.scalars(
            select(PaymentProviderEvent)
            .where(PaymentProviderEvent.payment_provider_event_id.in_(event_ids))
            .limit(100)
        )
    )
    result["evidence"] = [
        dict(
            evidence_id=e.payment_provider_event_id,
            source=e.evidence_source,
            outcome=e.observed_outcome,
            decision=e.processing_status,
            reason=e.failure_code,
            received_at=e.received_at,
        )
        for e in events
    ]
    return result


async def inquiry(
    request: Request,
    factory: async_sessionmaker[AsyncSession],
    manager: AuthenticatedPrincipal,
    command: InquiryCommand,
    target: UUID,
    refund: bool,
) -> AuditResponse:
    try:
        audit = await manager_inquiry(
            factory,
            provider(request),
            command_id=command.command_id,
            actor=manager.user_id,
            target_id=target,
            refund=refund,
            policy=configured_policy(request.app.state.settings),
            reported_reference=command.reported_payment_id,
        )
        return audit_response(audit)
    except (RefundConflictError, IdempotencyKeyConflictError):
        raise HTTPException(
            409, "Financial inquiry conflicts with established accounting"
        ) from None
    except (PaymentProviderNotConfiguredError, ValueError):
        raise HTTPException(503, "Financial reconciliation policy is not configured") from None


@router.post("/payment-attempts/{attempt_id}/reconcile", response_model=AuditResponse)
async def payment_inquiry(
    attempt_id: UUID, command: InquiryCommand, request: Request, manager: Manager, factory: Factory
) -> AuditResponse:
    return await inquiry(request, factory, manager, command, attempt_id, False)


@router.post("/refunds/{refund_id}/reconcile", response_model=AuditResponse)
async def refund_inquiry(
    refund_id: UUID, command: InquiryCommand, request: Request, manager: Manager, factory: Factory
) -> AuditResponse:
    if command.reported_payment_id:
        raise HTTPException(422, "Refund inquiry uses the established charge reference")
    return await inquiry(request, factory, manager, command, refund_id, True)


@router.post("/charges/{charge_id}/refund-approvals", response_model=AuditResponse)
async def refund_approval(
    charge_id: UUID,
    command: RefundApprovalCommand,
    request: Request,
    manager: Manager,
    factory: Factory,
) -> AuditResponse:
    try:
        return audit_response(
            await approve_refund(
                factory,
                provider(request),
                command_id=command.command_id,
                actor=manager.user_id,
                charge_id=charge_id,
                failed_refund_id=command.failed_refund_id,
                policy=configured_policy(request.app.state.settings),
            )
        )
    except (RefundConflictError, IdempotencyKeyConflictError):
        raise HTTPException(
            409, "Refund requires verified non-payable failure and complete accounting"
        ) from None
    except (PaymentProviderNotConfiguredError, PaymentProviderUncertainError, ValueError):
        raise HTTPException(
            503, "Verified provider evidence or reconciliation policy is unavailable"
        ) from None


class RecoveryCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command_id: UUID
    report_day: date | None = None


async def recovery(
    request: Request,
    factory: async_sessionmaker[AsyncSession],
    manager: AuthenticatedPrincipal,
    command: RecoveryCommand,
    target: UUID,
    action: RecoveryAction,
) -> AuditResponse:
    try:
        settings = request.app.state.settings
        return audit_response(
            await recover_financial_record(
                factory,
                provider(request),
                actor=manager.user_id,
                command_id=command.command_id,
                target_id=target,
                action=action,
                policy=configured_policy(settings),
                report_day=command.report_day,
                fee_includes_tax=settings.financial_settlement_fee_includes_tax,
                page_budget=settings.financial_inventory_page_budget or 1,
            )
        )
    except (RefundConflictError, IdempotencyKeyConflictError):
        raise HTTPException(409, "Recovery requires consistent account-bound evidence") from None
    except (PaymentProviderNotConfiguredError, ValueError):
        raise HTTPException(503, "Financial recovery is not configured") from None


@router.post("/exceptions/{case_id}/resolve", response_model=AuditResponse)
async def resolve_case(
    case_id: UUID, command: RecoveryCommand, request: Request, manager: Manager, factory: Factory
) -> AuditResponse:
    return await recovery(request, factory, manager, command, case_id, "RESOLVE_EXCEPTION")


@router.post("/provider-events/{evidence_id}/reconcile", response_model=AuditResponse)
async def investigate_provider(
    evidence_id: UUID,
    command: RecoveryCommand,
    request: Request,
    manager: Manager,
    factory: Factory,
) -> AuditResponse:
    return await recovery(request, factory, manager, command, evidence_id, "INVESTIGATE_PROVIDER")


@router.post("/settlement-evidence/{evidence_id}/reconcile", response_model=AuditResponse)
async def investigate_settlement(
    evidence_id: UUID,
    command: RecoveryCommand,
    request: Request,
    manager: Manager,
    factory: Factory,
) -> AuditResponse:
    return await recovery(request, factory, manager, command, evidence_id, "INVESTIGATE_SETTLEMENT")


@router.post("/refunds/{refund_id}/recover-original", response_model=AuditResponse)
async def recover_original(
    refund_id: UUID, command: RecoveryCommand, request: Request, manager: Manager, factory: Factory
) -> AuditResponse:
    return await recovery(request, factory, manager, command, refund_id, "RECOVER_ORIGINAL")
