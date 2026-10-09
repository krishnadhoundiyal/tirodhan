from datetime import datetime

from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.customer_reads.errors import CustomerReadError
from tirodhan.modules.customer_reads.schemas import (
    CancellationDto,
    HandoverDto,
    JourneyDto,
    MilestoneDto,
    PaymentAttemptDto,
    PaymentDto,
    RefundDto,
)
from tirodhan.modules.handovers.models import HandoverEvent
from tirodhan.modules.payments.models import Payment, PaymentAttempt, Refund
from tirodhan.modules.planning.models import PickupExecution
from tirodhan.modules.planning.policy import PlanningConfigurationError, planning_cutoff_time


def payment_projection(
    request: CollectionRequest,
    payment: Payment,
    attempts: list[PaymentAttempt],
    *,
    now: datetime,
    reconciliation: bool = False,
) -> PaymentDto:
    latest = attempts[-1] if attempts else None
    statuses = {
        "CREATED": "PROCESSING",
        "PENDING": "PENDING",
        "SUCCEEDED": "SUCCEEDED",
        "FAILED": "FAILED",
        "INITIATION_UNCERTAIN": "CONFIRMING",
    }
    current = (
        PaymentAttemptDto.model_validate(
            {
                "payment_attempt_id": latest.payment_attempt_id,
                "status": statuses[latest.status],
            }
        )
        if latest is not None
        else None
    )
    status = payment.status
    if status == "PENDING":
        if reconciliation:
            status = "CONFIRMING"
        elif request.status == "EXPIRED" or request.payment_expires_at <= now:
            status = "EXPIRED"
        elif request.status == "CANCELLED":
            status = "CANCELLED"
        elif current is not None:
            status = current.status
    if payment.status == "SUCCEEDED" and payment.succeeded_at is None:
        raise CustomerReadError(503, "NOT_ELIGIBLE")
    # A second charge in reconciliation cannot downgrade canonical success. An
    # unresolved attempt makes launching another charge unsafe for this read model.
    retry = (
        payment.status == "PENDING"
        and request.status == "PENDING_PAYMENT"
        and request.payment_expires_at > now
        and not reconciliation
        and all(a.status == "FAILED" for a in attempts)
    )
    return PaymentDto.model_validate(
        {
            "payment_id": payment.payment_id,
            "request_id": request.request_id,
            "amount_minor": payment.amount_minor,
            "currency": payment.currency,
            "status": status,
            "retry_allowed": retry,
            "expires_at": request.payment_expires_at if payment.status == "PENDING" else None,
            "succeeded_at": payment.succeeded_at,
            "current_attempt": current,
        }
    )


def refund_projection(refund: Refund) -> RefundDto:
    statuses = {
        "PENDING": "INITIATED",
        "PROCESSING": "PROCESSING",
        "SUBMITTED": "PROCESSING",
        "SUCCEEDED": "COMPLETED",
        "FAILED": "FAILED",
        "INITIATION_UNCERTAIN": "CONFIRMING",
    }
    reasons = {"CUSTOMER_CANCELLATION", "SERVICE_UNAVAILABLE", "PAYMENT_CORRECTION"}
    if (
        refund.status not in statuses
        or refund.reason_code not in reasons
        or (refund.status == "SUCCEEDED" and refund.completed_at is None)
    ):
        # Operations free-text reasons have no approved customer mapping.
        raise CustomerReadError(503, "NOT_ELIGIBLE")
    return RefundDto.model_validate(
        {
            "refund_id": refund.refund_id,
            "status": statuses[refund.status],
            "amount_minor": refund.amount_minor,
            "currency": refund.currency,
            "initiated_at": refund.created_at,
            "completed_at": refund.completed_at if refund.status == "SUCCEEDED" else None,
            "reason": refund.reason_code,
        }
    )


def cancellation_projection(
    request: CollectionRequest,
    payment: Payment,
    *,
    now: datetime,
    lead_time_minutes: int | None,
    frozen: bool = False,
) -> CancellationDto:
    try:
        cutoff = planning_cutoff_time(request.slot_start, lead_time_minutes)
    except PlanningConfigurationError:
        cutoff = None
    reason: str | None = None
    if request.status == "CANCELLED":
        reason = "ALREADY_CANCELLED"
    elif (
        frozen
        or request.planning_batch_id is not None
        or request.status in ("PRE_PLANNING", "PLANNED")
    ):
        reason = "PLANNING_STARTED"
    elif request.status != "ACCEPTED":
        reason = "NOT_ACCEPTED"
    elif cutoff is None:
        reason = "NOT_ELIGIBLE"
    elif now >= cutoff:
        reason = "PLANNING_CUTOFF_REACHED"
    return CancellationDto.model_validate(
        {
            "allowed": reason is None,
            "cutoff_at": cutoff,
            "reason": reason,
            "refund_expectation": "REVIEW_REQUIRED" if payment.status == "SUCCEEDED" else "NONE",
        }
    )


def journey_projection(
    request: CollectionRequest,
    pickup: PickupExecution | None,
    events: list[HandoverEvent],
) -> JourneyDto:
    validated = next((event for event in events if event.status == "VALIDATED"), None)
    recorded = validated or (events[-1] if events else None)
    collected = pickup.collected_at if pickup is not None and pickup.status == "COLLECTED" else None
    # A rejected geofence attempt is recorded history, not proof of receipt.
    times = [
        request.accepted_at,
        collected,
        validated.occurred_at if validated else None,
        validated.evaluated_at if validated else None,
    ]
    codes = ["BOOKED", "COLLECTED", "RECEIVED", "HANDOVER_VALIDATED"]
    labels = ["Booked", "Collected by Tirodhan", "Receiving point", "Handover validated"]
    current = next((i for i, value in enumerate(times) if value is None), None)
    milestones = [
        MilestoneDto.model_validate(
            {
                "code": code,
                "state": "COMPLETE" if value else "CURRENT" if i == current else "UPCOMING",
                "occurred_at": value,
                "label": labels[i],
                "detail": None,
                "image": None,
            }
        )
        for i, (code, value) in enumerate(zip(codes, times, strict=True))
    ]
    return JourneyDto(
        request_id=request.request_id,
        milestones=milestones,
        # Existing handovers snapshot geofence, but no historical display name,
        # address or approval label. Mutable master data cannot fill these facts.
        receiving_point=None,
        handover=HandoverDto(
            state="VALIDATED" if validated else "RECORDED" if recorded else "NOT_RECORDED",
            recorded_at=recorded.created_at if recorded else None,
            validated_at=validated.evaluated_at if validated else None,
        ),
    )
