"""Finite wake-up scheduling, independent operational clocks and delivery assurance."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import case, exists, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.modules.reliability.models import InboxMessage, OutboxEvent
from tirodhan.modules.reliability.primitives import (
    append_outbox_event,
    claim_inbox_message,
    complete_inbox_message,
)

from .dispatch_models import RouteRevision, RouteRevisionStop, RouteTask, VehicleRoute
from .fleet_models import FleetControl, FleetException
from .orchestration_models import FleetWakeup, RouteAcknowledgment, RouteDelivery

SCOPE = "COMMITTED_FLEET"


async def wake(session: AsyncSession, reason: str, now: datetime, *, urgent: bool = False) -> None:
    control = await session.get(FleetControl, SCOPE)
    if control is None or not control.configuration.get("regional", False):
        return
    delay = 0 if urgent else min(control.configuration.get("minimum_interval_seconds", 60), 60)
    due = now + timedelta(seconds=delay)
    await session.execute(
        insert(FleetWakeup)
        .values(scope=SCOPE, generation=1, acknowledged=0, due_at=due, reason=reason)
        .on_conflict_do_update(
            index_elements=["scope"],
            set_={
                "generation": FleetWakeup.generation + 1,
                "due_at": case(
                    (FleetWakeup.generation <= FleetWakeup.acknowledged, due),
                    else_=func.least(FleetWakeup.due_at, due),
                ),
                "reason": reason,
            },
        )
    )


async def prepare_due(factory: async_sessionmaker[AsyncSession], now: datetime) -> int | None:
    """No roads/native work. Deadline alarms are durable, and polling remains sufficient."""
    async with factory() as session, session.begin():
        control = await session.scalar(
            select(FleetControl).where(FleetControl.scope == SCOPE).with_for_update()
        )
        if control is None or not control.configuration.get("regional", False):
            return None
        # Consume existing financial facts without changing their producer or publication state.
        consumed = exists().where(
            InboxMessage.consumer_name == "routing-financial-wakeup",
            InboxMessage.message_id
            == func.cast(OutboxEvent.outbox_event_id, OutboxEvent.event_key.type),
            InboxMessage.status == "PROCESSED",
        )
        events = list(
            await session.scalars(
                select(OutboxEvent)
                .where(
                    OutboxEvent.event_type == "CustomerFinancialStatusChanged",
                    OutboxEvent.available_at <= now,
                    ~consumed,
                )
                .order_by(OutboxEvent.available_at, OutboxEvent.outbox_event_id)
                .limit(128)
            )
        )
        for event in events:
            claim = await claim_inbox_message(
                session,
                consumer_name="routing-financial-wakeup",
                message_id=str(event.outbox_event_id),
                message_type=event.event_type,
                business_key=str(event.aggregate_id),
                received_at=now,
            )
            if claim.message.status != "PROCESSED":
                if event.payload.get("change") in ("PAYMENT_CONFIRMED", "COLLECTION_CANCELLED"):
                    await wake(
                        session,
                        "BOOKING_FINANCIAL_FACT",
                        now,
                        urgent=event.payload.get("change") == "COLLECTION_CANCELLED",
                    )
                await complete_inbox_message(session, claim.message, processed_at=now)
        pending = await session.get(FleetWakeup, SCOPE)
        if pending and pending.generation > pending.acknowledged and pending.due_at <= now:
            # Preserve an existing owner; the token fence, rather than wakeup transport, owns work.
            control.next_due_at = min(control.next_due_at, now)
        from .models import OperatingShift
        from .orchestration_models import OperatingBreak

        deliveries = list(
            await session.scalars(
                select(RouteDelivery)
                .where(RouteDelivery.due_at <= now, RouteDelivery.escalated_at.is_(None))
                .order_by(RouteDelivery.revision_id)
                .limit(128)
                .with_for_update(skip_locked=True)
            )
        )
        for delivery in deliveries:
            revision = await session.get(RouteRevision, delivery.revision_id)
            assert revision is not None
            root = await session.get(VehicleRoute, revision.route_id)
            assert root is not None
            if (
                await session.get(RouteAcknowledgment, delivery.revision_id) is None
                and root.version == revision.version
                and root.status != "CLOSED"
            ):
                if now < delivery.acknowledgment_due_at and delivery.attempts < 3:
                    delivery.attempts += 1
                    delivery.due_at = min(
                        delivery.acknowledgment_due_at, now + timedelta(seconds=60)
                    )
                    await append_outbox_event(
                        session,
                        event_key=f"route-redelivery:{delivery.revision_id}:{delivery.attempts}",
                        aggregate_type="VehicleRoute",
                        aggregate_id=root.route_id,
                        event_type="VehicleRouteDeliveryRequested",
                        payload={
                            "revision_id": str(delivery.revision_id),
                            "rider_id": str(delivery.rider_id),
                        },
                    )
                    continue
                if now < delivery.acknowledgment_due_at:
                    delivery.due_at = delivery.acknowledgment_due_at
                    continue
                delivery.escalated_at = now
                attention = FleetException(
                    route_id=root.route_id,
                    reported_by=control.manager_id,
                    reason="ROUTE_RECEIPT_OVERDUE",
                    created_at=now,
                )
                session.add(attention)
                await session.flush([attention])
                await append_outbox_event(
                    session,
                    event_key=f"route-receipt:{delivery.revision_id}",
                    aggregate_type="VehicleRoute",
                    aggregate_id=root.route_id,
                    event_type="FleetPlanningAttentionRequired",
                    payload={"exception_id": str(attention.exception_id)},
                )
        # Time progression is a trigger even if no business row or GPS point changed.
        next_start = await session.scalar(
            select(func.min(RouteRevisionStop.planned_start))
            .join(RouteRevision)
            .join(VehicleRoute, VehicleRoute.route_id == RouteRevision.route_id)
            .join(RouteTask, RouteTask.task_id == RouteRevisionStop.task_id)
            .where(RouteRevision.version == VehicleRoute.version, RouteTask.state == "PENDING")
        )
        lead = control.configuration.get("planning_lead_seconds", 0)
        if (
            next_start is not None
            and (
                control.last_evaluated_at is None
                or control.last_evaluated_at < next_start - timedelta(seconds=lead)
            )
            and next_start - timedelta(seconds=lead) <= now
        ):
            control.next_due_at = min(control.next_due_at, now)
        # Force a fresh problem across break/shift boundaries; no implicit shift extension.
        boundary = await session.scalar(
            select(func.count())
            .select_from(OperatingBreak)
            .join(OperatingShift)
            .where(
                OperatingShift.closed_at.is_(None),
                OperatingBreak.starts_at <= now,
                OperatingBreak.ends_at >= now,
            )
        )
        if boundary:
            control.next_due_at = min(control.next_due_at, now)
        return pending.generation if pending else None


async def acknowledge_generation(
    factory: async_sessionmaker[AsyncSession], generation: int | None
) -> None:
    if generation is None:
        return
    async with factory() as session, session.begin():
        row = await session.scalar(
            select(FleetWakeup).where(FleetWakeup.scope == SCOPE).with_for_update()
        )
        if row is not None:
            row.acknowledged = max(row.acknowledged, generation)


def planning_lead_seconds(
    *,
    compute_seconds: float,
    delivery_seconds: int,
    readiness_seconds: int,
    approach_seconds: int,
    recovery_seconds: int,
    safety_fraction: float = 0.25,
) -> int:
    """Operational estimate; never changes booking/payment/cancellation eligibility."""
    import math

    values = (
        compute_seconds,
        delivery_seconds,
        readiness_seconds,
        approach_seconds,
        recovery_seconds,
    )
    if any(v < 0 for v in values) or not 0 <= safety_fraction <= 1:
        raise ValueError("invalid planning lead components")
    return math.ceil(sum(values) * (1 + safety_fraction))
