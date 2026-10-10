"""Manager-only evidence inquiry and explicitly authorized financial recovery."""

from datetime import datetime
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.api.dependencies import get_database_session, get_session_factory, require_role
from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.identity.service import AuthenticatedPrincipal
from tirodhan.modules.payments.models import (
    CapturedCharge,
    FinancialAudit,
    FinancialException,
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
    payment_id: UUID
    refund_id: UUID | None
    evidence_id: UUID | None
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


@router.get("/payments/{payment_id}")
async def detail(payment_id: UUID, manager: Manager, session: ReadSession) -> dict[str, Any]:
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
            ["payment_attempt_id", "status", "provider", "provider_order_id", "next_check_at"],
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
            ],
        ),
    ]
    result["truncated"] = []
    for name, model, condition, fields in projections:
        rows = list(await session.scalars(select(model).where(condition).limit(101)))
        result[name] = [{field: getattr(row, field) for field in fields} for row in rows[:100]]
        if len(rows) > 100:
            result["truncated"].append(name)
    charge_ids = [c["captured_charge_id"] for c in result["charges"]]
    obligations = list(
        await session.scalars(
            select(RefundObligation).where(RefundObligation.captured_charge_id.in_(charge_ids))
        )
    )
    paid_rows = (
        await session.execute(
            select(Refund.refund_obligation_id, func.sum(Refund.amount_minor))
            .where(
                Refund.refund_obligation_id.in_([o.refund_obligation_id for o in obligations]),
                Refund.status == "SUCCEEDED",
            )
            .group_by(Refund.refund_obligation_id)
        )
    ).all()
    paid_by_obligation = {key: int(value) for key, value in paid_rows}
    result["obligations"] = [
        dict(
            refund_obligation_id=o.refund_obligation_id,
            captured_charge_id=o.captured_charge_id,
            amount_minor=o.amount_minor,
            payout_blocked=o.payout_blocked,
            outstanding_minor=max(
                0,
                o.amount_minor - int(paid_by_obligation.get(o.refund_obligation_id, 0)),
            ),
        )
        for o in obligations
    ]
    event_ids = {c["evidence_id"] for c in result["charges"]} | {
        a["evidence_id"] for a in result["audit"] if a["evidence_id"]
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
