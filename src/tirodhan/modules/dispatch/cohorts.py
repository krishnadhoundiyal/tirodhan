"""One caller-owned transaction establishes one complete cell-based cohort."""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import String, exists, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.dispatch.events import FLEET_FIRST, INDEPENDENT, append_dispatch_event
from tirodhan.modules.dispatch.models import (
    AssignmentOffer,
    Fleet,
    FleetMembership,
    FleetServiceCell,
    RiderAssignment,
    RiderServiceCell,
)
from tirodhan.modules.dispatch.service import (
    PickupGroupNotAssignableError,
    RiderNotEligibleError,
    RiderNotFoundError,
    _active_assignment,
    _lock_eligible_rider,
    _lock_group,
    _require_initial_dispatch_population,
)
from tirodhan.modules.planning.models import PickupExecution, PlanningBatch
from tirodhan.modules.reliability.models import OutboxEvent


async def create_offer_cohort(
    session: AsyncSession,
    *,
    collection_group_id: UUID,
    dispatch_stage: str,
    evaluation_time: datetime,
    lifetime_seconds: int,
) -> tuple[AssignmentOffer, ...]:
    if dispatch_stage not in (FLEET_FIRST, INDEPENDENT) or lifetime_seconds <= 0:
        raise ValueError("invalid dispatch stage or offer lifetime")
    if evaluation_time.tzinfo is None:
        raise ValueError("dispatch time must be timezone-aware")
    group = await _lock_group(session, collection_group_id)
    if await _active_assignment(session, collection_group_id, lock=True) is not None:
        return ()
    try:
        await _require_initial_dispatch_population(session, collection_group_id)
    except PickupGroupNotAssignableError:
        # Completed/reassigned work is not a new initial-dispatch opportunity.
        return ()
    established = tuple(
        await session.scalars(
            select(AssignmentOffer)
            .where(AssignmentOffer.collection_group_id == collection_group_id)
            .order_by(AssignmentOffer.rider_id)
        )
    )
    fleet_offers = tuple(offer for offer in established if offer.audience_kind == "FLEET")
    independent = tuple(offer for offer in established if offer.audience_kind == INDEPENDENT)
    if dispatch_stage == FLEET_FIRST and established:
        return fleet_offers or independent
    if dispatch_stage == INDEPENDENT:
        if independent:
            return independent
        if not fleet_offers or any(offer.expires_at > evaluation_time for offer in fleet_offers):
            raise ValueError("independent fallback requires an expired fleet cohort")
    batch = await session.get(PlanningBatch, group.planning_batch_id)
    if batch is None:
        raise RuntimeError("dispatch planning batch is missing")

    candidates: list[tuple[UUID, UUID | None]] = []
    if dispatch_stage == FLEET_FIRST:
        # Coverage/state managers lock fleet before rider, as does this path. Sorting
        # fleets and riders provides a common order across concurrent groups.
        fleets = list(
            await session.scalars(
                select(Fleet)
                .where(Fleet.status == "ACTIVE")
                .order_by(Fleet.fleet_id)
                .with_for_update()
            )
        )
        for fleet in fleets:
            coverage = await session.scalar(
                select(FleetServiceCell).where(
                    FleetServiceCell.fleet_id == fleet.fleet_id,
                    FleetServiceCell.cell_id == batch.cell_id,
                    FleetServiceCell.deactivated_at.is_(None),
                )
            )
            if fleet.status == "ACTIVE" and coverage is not None:
                members = await session.scalars(
                    select(FleetMembership.rider_id).where(
                        FleetMembership.fleet_id == fleet.fleet_id,
                        FleetMembership.left_at.is_(None),
                    )
                )
                candidates.extend((rider_id, fleet.fleet_id) for rider_id in members)
    eligible: list[tuple[UUID, UUID | None]] = []
    for rider_id, fleet_id in sorted(candidates):
        try:
            await _lock_eligible_rider(session, rider_id)
        except (RiderNotEligibleError, RiderNotFoundError):
            continue
        eligible.append((rider_id, fleet_id))
    audience = "FLEET"
    if not eligible:
        audience = INDEPENDENT
        riders = tuple(
            await session.scalars(
                select(RiderServiceCell.rider_id)
                .where(
                    RiderServiceCell.cell_id == batch.cell_id,
                    RiderServiceCell.deactivated_at.is_(None),
                    ~exists(
                        select(FleetMembership.fleet_membership_id).where(
                            FleetMembership.rider_id == RiderServiceCell.rider_id,
                            FleetMembership.left_at.is_(None),
                        )
                    ),
                )
                .order_by(RiderServiceCell.rider_id)
            )
        )
        for rider_id in riders:
            try:
                await _lock_eligible_rider(session, rider_id)
            except (RiderNotEligibleError, RiderNotFoundError):
                continue
            # Rider row serialization also excludes concurrent membership/coverage
            # mutations; re-read those facts only after acquiring the rider locks.
            member = await session.scalar(
                select(FleetMembership).where(
                    FleetMembership.rider_id == rider_id, FleetMembership.left_at.is_(None)
                )
            )
            cell = await session.scalar(
                select(RiderServiceCell).where(
                    RiderServiceCell.rider_id == rider_id,
                    RiderServiceCell.cell_id == batch.cell_id,
                    RiderServiceCell.deactivated_at.is_(None),
                )
            )
            if member is None and cell is not None:
                eligible.append((rider_id, None))
    round_number = max((offer.offer_round for offer in established), default=0) + 1
    offers = tuple(
        AssignmentOffer(
            offer_id=new_uuid7(),
            collection_group_id=collection_group_id,
            rider_id=rider_id,
            offer_round=round_number,
            status="OPEN",
            offered_at=evaluation_time,
            expires_at=evaluation_time + timedelta(seconds=lifetime_seconds),
            audience_kind=audience,
            fleet_id=fleet_id,
        )
        for rider_id, fleet_id in eligible
    )
    session.add_all(offers)
    await session.flush()
    return offers


async def scan_expired_fleet_cohorts(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    evaluation_time: datetime | None = None,
    limit: int = 100,
) -> int:
    if limit <= 0:
        raise ValueError("scan limit must be positive")
    now = evaluation_time or utc_now()
    independent_offer = aliased(AssignmentOffer)
    # Exclude already requested fallbacks before LIMIT so old expired groups cannot
    # starve later groups. Group locking still arbitrates acceptance/duplicate scans.
    async with session_factory() as session:
        group_ids = tuple(
            await session.scalars(
                select(AssignmentOffer.collection_group_id)
                .where(
                    AssignmentOffer.audience_kind == "FLEET",
                    AssignmentOffer.expires_at <= now,
                    ~exists(
                        select(RiderAssignment.assignment_id).where(
                            RiderAssignment.collection_group_id
                            == AssignmentOffer.collection_group_id,
                            RiderAssignment.status == "ACTIVE",
                        )
                    ),
                    ~exists(
                        select(independent_offer.offer_id).where(
                            independent_offer.collection_group_id
                            == AssignmentOffer.collection_group_id,
                            independent_offer.audience_kind == INDEPENDENT,
                        )
                    ),
                    ~exists(
                        select(PickupExecution.pickup_execution_id).where(
                            PickupExecution.collection_group_id
                            == AssignmentOffer.collection_group_id,
                            PickupExecution.status != "PENDING_ASSIGNMENT",
                        )
                    ),
                    ~exists(
                        select(OutboxEvent.outbox_event_id).where(
                            OutboxEvent.event_key
                            == "collection-group-dispatch:"
                            + AssignmentOffer.collection_group_id.cast(String)
                            + ":INDEPENDENT"
                        )
                    ),
                )
                .distinct()
                .order_by(AssignmentOffer.collection_group_id)
                .limit(limit)
            )
        )
    count = 0
    for group_id in group_ids:
        async with session_factory() as session, session.begin():
            group = await _lock_group(session, group_id)
            if await _active_assignment(session, group_id, lock=True) is not None:
                continue
            offers = tuple(
                await session.scalars(
                    select(AssignmentOffer).where(AssignmentOffer.collection_group_id == group_id)
                )
            )
            fleet = tuple(offer for offer in offers if offer.audience_kind == "FLEET")
            if (
                not fleet
                or any(offer.expires_at > now for offer in fleet)
                or any(offer.audience_kind == INDEPENDENT for offer in offers)
            ):
                continue
            try:
                await _require_initial_dispatch_population(session, group_id)
            except PickupGroupNotAssignableError:
                continue
            key = f"collection-group-dispatch:{group_id}:INDEPENDENT"
            if await session.scalar(select(OutboxEvent).where(OutboxEvent.event_key == key)):
                continue
            batch = await session.get(PlanningBatch, group.planning_batch_id)
            if batch is None:
                raise RuntimeError("dispatch planning batch is missing")
            await append_dispatch_event(session, group_id=group_id, batch=batch, stage=INDEPENDENT)
            count += 1
    return count
