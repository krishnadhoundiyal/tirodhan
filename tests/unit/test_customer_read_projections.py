from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.collection_requests.scheduling import (
    SERVICE_TIMEZONE,
    SlotConflictError,
    daily_grid,
    validate_slot,
)
from tirodhan.modules.customer_reads.cursor import Cursor, CursorCodec
from tirodhan.modules.customer_reads.errors import CustomerReadError
from tirodhan.modules.customer_reads.projections import (
    cancellation_projection,
    journey_projection,
    payment_projection,
    refund_projection,
)
from tirodhan.modules.customer_reads.schemas import MediaDto
from tirodhan.modules.handovers.models import HandoverEvent
from tirodhan.modules.payments.models import Payment, PaymentAttempt, Refund
from tirodhan.modules.planning.models import PickupExecution

NOW = datetime(2026, 10, 9, 0, 0, tzinfo=timezone.utc)


def request(status: str = "ACCEPTED") -> CollectionRequest:
    return CollectionRequest(
        request_id=uuid4(),
        status=status,
        created_at=NOW,
        slot_start=NOW + timedelta(days=1),
        slot_end=NOW + timedelta(days=1, minutes=30),
        payment_expires_at=NOW + timedelta(hours=1),
        accepted_at=NOW if status == "ACCEPTED" else None,
    )


def payment(req: CollectionRequest, status: str = "PENDING") -> Payment:
    return Payment(
        payment_id=uuid4(),
        request_id=req.request_id,
        status=status,
        amount_minor=100,
        currency="INR",
        succeeded_at=NOW if status == "SUCCEEDED" else None,
    )


def test_daily_grid_timezone_and_midnight() -> None:
    slots = daily_grid(date(2026, 10, 9))
    assert len(slots) == 48
    assert len({s.slot_id for s in slots}) == 48
    assert slots[0].start == datetime(2026, 10, 8, 18, 30, tzinfo=timezone.utc)
    assert slots[-1].end.astimezone(SERVICE_TIMEZONE).date() == date(2026, 10, 10)
    assert all(s.end - s.start == timedelta(minutes=30) for s in slots)
    assert all(s.start.astimezone(SERVICE_TIMEZONE).minute in (0, 30) for s in slots)
    assert all(a.end == b.start for a, b in zip(slots[:-1], slots[1:], strict=True))


@pytest.mark.parametrize("minutes", [0, 15, 29, 31, 60])
def test_invalid_slot_duration(minutes: int) -> None:
    start = NOW + timedelta(days=1)
    with pytest.raises(SlotConflictError):
        validate_slot(start, start + timedelta(minutes=minutes), now=NOW)


@pytest.mark.parametrize(
    "offset", [timedelta(minutes=1), timedelta(seconds=1), timedelta(microseconds=1)]
)
def test_invalid_slot_alignment(offset: timedelta) -> None:
    start = NOW + timedelta(days=1) + offset
    with pytest.raises(SlotConflictError):
        validate_slot(start, start + timedelta(minutes=30), now=NOW)


def test_past_naive_and_offset_equivalence() -> None:
    with pytest.raises(SlotConflictError):
        validate_slot(NOW, NOW + timedelta(minutes=30), now=NOW)
    with pytest.raises(SlotConflictError):
        validate_slot(NOW.replace(tzinfo=None), NOW + timedelta(minutes=30), now=NOW)
    start = NOW + timedelta(days=1)
    assert (
        validate_slot(
            start.astimezone(SERVICE_TIMEZONE),
            (start + timedelta(minutes=30)).astimezone(SERVICE_TIMEZONE),
            now=NOW,
        ).start
        == start
    )


def test_signed_cursor_replay_owner_view_expiry_and_tampering() -> None:
    codec = CursorCodec(b"k" * 32)
    value = Cursor(uuid4(), "active", NOW, NOW + timedelta(minutes=5), NOW, uuid4())
    token = codec.encode(value)
    assert codec.decode(token, owner=value.owner, view="active", now=NOW) == value
    for candidate, owner, view in [
        (token[:-4] + "AAAA", value.owner, "active"),
        (token, uuid4(), "active"),
        (token, value.owner, "history"),
        ("bad!", value.owner, "active"),
    ]:
        with pytest.raises(CustomerReadError) as error:
            codec.decode(candidate, owner=owner, view=view, now=NOW)
        assert error.value.status == 422
    with pytest.raises(CustomerReadError) as error:
        codec.decode(token, owner=value.owner, view="active", now=value.expires_at)
    assert error.value.code == "CURSOR_EXPIRED"


@pytest.mark.parametrize(
    "state,expected",
    [
        ("CREATED", "PROCESSING"),
        ("PENDING", "PENDING"),
        ("INITIATION_UNCERTAIN", "CONFIRMING"),
        ("FAILED", "FAILED"),
    ],
)
def test_payment_attempt_projection(state: str, expected: str) -> None:
    req = request("PENDING_PAYMENT")
    p = payment(req)
    attempt = PaymentAttempt(payment_attempt_id=uuid4(), status=state)
    result = payment_projection(req, p, [attempt], now=NOW)
    assert result.status == expected
    assert result.retry_allowed == (state == "FAILED")
    assert result.current_attempt is not None


def test_successful_payment_wins_late_attempt_and_reconciliation() -> None:
    req = request()
    p = payment(req, "SUCCEEDED")
    late = PaymentAttempt(payment_attempt_id=uuid4(), status="FAILED")
    result = payment_projection(req, p, [late], now=NOW, reconciliation=True)
    assert result.status == "SUCCEEDED"
    assert not result.retry_allowed
    assert result.succeeded_at == NOW


@pytest.mark.parametrize(
    "state,expected",
    [
        ("PENDING", "INITIATED"),
        ("PROCESSING", "PROCESSING"),
        ("SUBMITTED", "PROCESSING"),
        ("SUCCEEDED", "COMPLETED"),
        ("INITIATION_UNCERTAIN", "CONFIRMING"),
        ("FAILED", "FAILED"),
    ],
)
def test_refund_projection(state: str, expected: str) -> None:
    refund = Refund(
        refund_id=uuid4(),
        status=state,
        created_at=NOW,
        completed_at=NOW if state == "SUCCEEDED" else None,
        amount_minor=50,
        currency="INR",
        reason_code="CUSTOMER_CANCELLATION",
    )
    result = refund_projection(refund)
    assert result.status == expected
    assert result.completed_at == (NOW if state == "SUCCEEDED" else None)


def test_missing_financial_truth_and_unmapped_refund_fail_safely() -> None:
    req = request()
    p = payment(req, "SUCCEEDED")
    p.succeeded_at = None
    with pytest.raises(CustomerReadError):
        payment_projection(req, p, [], now=NOW)
    refund = Refund(status="SUCCEEDED", completed_at=None, reason_code="CUSTOMER_CANCELLATION")
    with pytest.raises(CustomerReadError):
        refund_projection(refund)
    refund.reason_code = "OPERATIONS_ADJUSTMENT"
    with pytest.raises(CustomerReadError):
        refund_projection(refund)


def test_cancellation_boundaries_freeze_and_open_compensation_policy() -> None:
    req = request()
    p = payment(req, "SUCCEEDED")
    cutoff = req.slot_start - timedelta(minutes=4)
    assert cancellation_projection(
        req, p, now=cutoff - timedelta(microseconds=1), lead_time_minutes=4
    ).allowed
    result = cancellation_projection(req, p, now=cutoff, lead_time_minutes=4)
    assert not result.allowed and result.reason == "PLANNING_CUTOFF_REACHED"
    assert result.refund_expectation == "REVIEW_REQUIRED"
    assert (
        cancellation_projection(req, p, now=NOW, lead_time_minutes=4, frozen=True).reason
        == "PLANNING_STARTED"
    )
    assert not cancellation_projection(req, p, now=NOW, lead_time_minutes=None).allowed
    req.status = "CANCELLED"
    assert (
        cancellation_projection(req, p, now=NOW, lead_time_minutes=4).reason == "ALREADY_CANCELLED"
    )


def test_journey_uses_recorded_facts_and_rejected_handover_never_completes_receipt() -> None:
    req = request()
    initial = journey_projection(req, None, [])
    assert [m.state for m in initial.milestones] == ["COMPLETE", "CURRENT", "UPCOMING", "UPCOMING"]
    pickup = PickupExecution(status="COLLECTED", collected_at=NOW + timedelta(minutes=30))
    rejected = HandoverEvent(
        status="REJECTED",
        created_at=NOW + timedelta(hours=1),
        occurred_at=NOW + timedelta(hours=1),
        evaluated_at=NOW + timedelta(hours=1),
    )
    result = journey_projection(req, pickup, [rejected])
    assert result.handover.state == "RECORDED"
    assert result.milestones[2].state == "CURRENT" and result.milestones[2].occurred_at is None
    assert result.receiving_point is None
    rejected.status = "VALIDATED"
    result = journey_projection(req, pickup, [rejected])
    assert all(m.state == "COMPLETE" for m in result.milestones)
    assert result.handover.validated_at == rejected.evaluated_at


@pytest.mark.parametrize(
    "url", ["http://blob.test/a", "https://user:secret@blob.test/a", "https://blob.test/a?sp=rw"]
)
def test_unsafe_media_rejected(url: str) -> None:
    with pytest.raises(ValidationError):
        MediaDto(
            url=url,
            thumbnail_url=None,
            width=1,
            height=1,
            alt_text="Art",
            blurhash=None,
            expires_at=None,
        )
