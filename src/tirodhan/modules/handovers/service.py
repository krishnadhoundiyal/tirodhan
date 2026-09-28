from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from geoalchemy2 import Geography
from sqlalchemy import func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.customers.service import GeoPoint, command_fingerprint, geography_point
from tirodhan.modules.dispatch.models import RiderAssignment, RiderAssignmentItem
from tirodhan.modules.handovers.models import HandoverEvent, HandoverEventItem
from tirodhan.modules.planning.models import PickupExecution
from tirodhan.modules.receiving_points.models import ReceivingPoint
from tirodhan.modules.reliability.primitives import (
    claim_idempotency_record,
    complete_idempotency_record,
    get_completed_idempotency_result,
)

RECEIVING_POINT_ACTIVE = "ACTIVE"
HANDOVER_VALIDATED = "VALIDATED"
HANDOVER_REJECTED = "REJECTED"
WITHIN_ALLOWED_RADIUS = "WITHIN_ALLOWED_RADIUS"
OUTSIDE_ALLOWED_RADIUS = "OUTSIDE_ALLOWED_RADIUS"
PICKUP_COLLECTED = "COLLECTED"
HANDOVER_IDEMPOTENCY_SCOPE = "handover.record"

_POINT_GEOGRAPHY = Geography(geometry_type="POINT", srid=4326, spatial_index=False)


class HandoverError(RuntimeError):
    pass


class InvalidHandoverCommandError(ValueError):
    pass


class HandoverCommandInProgressError(HandoverError):
    pass


class HandoverStateInconsistentError(HandoverError):
    pass


class ReceivingPointNotFoundError(HandoverError):
    pass


class ReceivingPointInactiveError(HandoverError):
    pass


class HandoverPickupNotFoundError(HandoverError):
    pass


class HandoverPickupNotCollectedError(HandoverError):
    pass


class HandoverRiderAttributionError(HandoverError):
    pass


class PickupAlreadyHandedOverError(HandoverError):
    pass


async def record_handover(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    client_handover_id: UUID,
    rider_id: UUID,
    receiving_point_id: UUID,
    pickup_execution_ids: Sequence[UUID],
    observed_location: GeoPoint | None,
    idempotency_expires_at: datetime,
    now: datetime | None = None,
) -> HandoverEvent:
    canonical_pickup_ids = tuple(sorted(pickup_execution_ids, key=lambda value: value.int))
    fingerprint = command_fingerprint(
        {
            "rider_id": rider_id,
            "receiving_point_id": receiving_point_id,
            "pickup_execution_ids": [str(value) for value in canonical_pickup_ids],
            "observed_location": {
                "latitude": observed_location.latitude if observed_location is not None else None,
                "longitude": observed_location.longitude if observed_location is not None else None,
            },
        }
    )

    async with session_factory() as session, session.begin():
        claim = await claim_idempotency_record(
            session,
            scope=HANDOVER_IDEMPOTENCY_SCOPE,
            idempotency_key=str(client_handover_id),
            request_fingerprint=fingerprint,
            expires_at=idempotency_expires_at,
        )
        if not claim.created:
            result = get_completed_idempotency_result(claim.record)
            if result is None or result.resource_id is None:
                raise HandoverCommandInProgressError("handover command is in progress")
            event = await session.get(HandoverEvent, result.resource_id)
            if event is None or event.client_handover_id != client_handover_id:
                raise HandoverStateInconsistentError(
                    "completed handover command references an invalid event"
                )
            return event

        _validate_command_inputs(
            pickup_execution_ids=pickup_execution_ids,
            observed_location=observed_location,
        )
        assert observed_location is not None

        receiving_point = await session.scalar(
            select(ReceivingPoint)
            .where(ReceivingPoint.receiving_point_id == receiving_point_id)
            .with_for_update()
        )
        if receiving_point is None:
            raise ReceivingPointNotFoundError("receiving point not found")
        if receiving_point.status != RECEIVING_POINT_ACTIVE:
            raise ReceivingPointInactiveError("receiving point is not ACTIVE")

        pickups = list(
            await session.scalars(
                select(PickupExecution)
                .where(PickupExecution.pickup_execution_id.in_(canonical_pickup_ids))
                .order_by(PickupExecution.pickup_execution_id)
                .with_for_update()
            )
        )
        if len(pickups) != len(canonical_pickup_ids):
            raise HandoverPickupNotFoundError("one or more pickup executions were not found")
        if any(pickup.status != PICKUP_COLLECTED for pickup in pickups):
            raise HandoverPickupNotCollectedError("every pickup must be COLLECTED")

        attributed_pickup_ids = set(
            await session.scalars(
                select(RiderAssignmentItem.pickup_execution_id)
                .join(
                    RiderAssignment,
                    RiderAssignment.assignment_id == RiderAssignmentItem.assignment_id,
                )
                .where(
                    RiderAssignmentItem.pickup_execution_id.in_(canonical_pickup_ids),
                    RiderAssignmentItem.released_at.is_(None),
                    RiderAssignment.rider_id == rider_id,
                )
            )
        )
        if attributed_pickup_ids != set(canonical_pickup_ids):
            raise HandoverRiderAttributionError(
                "every pickup must retain unreleased assignment attribution to the rider"
            )

        already_validated = await session.scalar(
            select(HandoverEventItem.pickup_execution_id).where(
                HandoverEventItem.pickup_execution_id.in_(canonical_pickup_ids),
                HandoverEventItem.status == HANDOVER_VALIDATED,
            )
        )
        if already_validated is not None:
            raise PickupAlreadyHandedOverError("a pickup already has a validated handover")

        within_radius, distance_m = await _evaluate_geofence(
            session,
            observed_location=observed_location,
            receiving_point_location=receiving_point.location,
            allowed_radius_m=receiving_point.allowed_radius_m,
        )
        event_status = HANDOVER_VALIDATED if within_radius else HANDOVER_REJECTED
        validation_code = WITHIN_ALLOWED_RADIUS if within_radius else OUTSIDE_ALLOWED_RADIUS
        evaluated_at = now or utc_now()
        event = HandoverEvent(
            handover_event_id=new_uuid7(),
            client_handover_id=client_handover_id,
            rider_id=rider_id,
            receiving_point_id=receiving_point.receiving_point_id,
            occurred_at=evaluated_at,
            observed_location=geography_point(observed_location),
            receiving_point_location_snapshot=receiving_point.location,
            allowed_radius_m_snapshot=receiving_point.allowed_radius_m,
            distance_m=distance_m,
            status=event_status,
            validation_code=validation_code,
            created_at=evaluated_at,
            evaluated_at=evaluated_at,
        )
        items = [
            HandoverEventItem(
                handover_event_id=event.handover_event_id,
                pickup_execution_id=pickup_id,
                status=event_status,
                created_at=evaluated_at,
                evaluated_at=evaluated_at,
            )
            for pickup_id in canonical_pickup_ids
        ]
        session.add(event)
        session.add_all(items)
        await session.flush([event, *items])
        await complete_idempotency_record(
            session,
            claim.record,
            result_resource_id=event.handover_event_id,
            result_status_code=None,
            completed_at=evaluated_at,
        )
        return event


def _validate_command_inputs(
    *,
    pickup_execution_ids: Sequence[UUID],
    observed_location: GeoPoint | None,
) -> None:
    if not pickup_execution_ids:
        raise InvalidHandoverCommandError("at least one pickup execution is required")
    if len(set(pickup_execution_ids)) != len(pickup_execution_ids):
        raise InvalidHandoverCommandError("duplicate pickup execution IDs are not allowed")
    if observed_location is None:
        raise InvalidHandoverCommandError("observed location is required")


async def _evaluate_geofence(
    session: AsyncSession,
    *,
    observed_location: GeoPoint,
    receiving_point_location: Any,
    allowed_radius_m: int,
) -> tuple[bool, float]:
    observed = literal(geography_point(observed_location), type_=_POINT_GEOGRAPHY)
    receiving_point = literal(receiving_point_location, type_=_POINT_GEOGRAPHY)
    distance = func.ST_Distance(observed, receiving_point)
    row = (
        await session.execute(
            select(
                or_(
                    func.ST_DWithin(observed, receiving_point, allowed_radius_m),
                    distance <= allowed_radius_m,
                ),
                distance,
            )
        )
    ).one()
    return bool(row[0]), float(row[1])
