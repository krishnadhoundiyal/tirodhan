"""Atomic forecast-inventory holds. Checkout never invokes routing feasibility."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from uuid import UUID

from pydantic import Field
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.db.values import utc_now
from tirodhan.modules.business_admin.weight_contracts import (
    WeightItemInput,
    WeightQuoteConfirmation,
)
from tirodhan.modules.collection_requests.scheduling import validate_slot
from tirodhan.modules.reliability.primitives import (
    append_outbox_event,
    claim_idempotency_record,
    complete_idempotency_record,
)
from tirodhan.modules.serviceability.models import ServiceabilityContext

from .admission import ReserveCapacity
from .admission_models import AdmissionSourceHead
from .allowance_estimator import Workload, footprint, remaining_allowance
from .capacity_models import RegionalCapacityBudget, RegionalSlotProjection, SlotReservationDebit
from .commands import OperationalInputError, require_live_role
from .orchestration import wake
from .orchestration_models import CapacityReservation, OperatingRegion, RegionCell


class HoldSlot(ReserveCapacity):
    expected_generation: int = Field(strict=True, ge=1)
    items: tuple[WeightItemInput, ...] | None = None
    confirmed_quote: WeightQuoteConfirmation | None = None


async def hold_slot(
    factory: async_sessionmaker[AsyncSession],
    *,
    customer_id: UUID,
    command: HoldSlot,
    expires_at: datetime,
    now: datetime | None = None,
    customer_checkout: bool = False,
) -> UUID:
    try:
        return await _hold(
            factory, customer_id, command, expires_at, now or utc_now(), customer_checkout
        )
    except DBAPIError as error:
        if getattr(error.orig, "sqlstate", None) in {"40001", "40P01", "23514"}:
            raise OperationalInputError("SLOT_ALLOWANCE_CHANGED_RETRY") from None
        raise


async def _hold(
    factory: async_sessionmaker[AsyncSession],
    customer_id: UUID,
    command: HoldSlot,
    expires_at: datetime,
    now: datetime,
    customer_checkout: bool,
) -> UUID:
    # Preserve the exact legacy wire fingerprint when new optional fields are absent.
    exclude: dict[str, bool | dict[int, set[str]]] = {
        name: True for name in ("items", "confirmed_quote") if getattr(command, name) is None
    }
    # Omitted volume must not alter pre-volume hold fingerprints, including nested items.
    if command.items is not None:
        exclude["items"] = {
            index: {"declared_volume_ml"}
            for index, item in enumerate(command.items)
            if item.declared_volume_ml is None
        }
    fingerprint = hashlib.sha256(command.model_dump_json(exclude=exclude).encode()).digest()
    async with factory() as session, session.begin():
        await require_live_role(session, customer_id, "CUSTOMER")
        claim = await claim_idempotency_record(
            session,
            scope=f"routing.slot-hold:{customer_id}",
            idempotency_key=str(command.client_command_id),
            request_fingerprint=fingerprint,
            expires_at=expires_at,
            now=now,
        )
        if claim.record.status == "COMPLETED":
            assert claim.record.result_resource_id is not None
            return claim.record.result_resource_id
        if not claim.created:
            raise OperationalInputError("SLOT_HOLD_IN_PROGRESS")
        from tirodhan.modules.business_admin.weight_pricing import (
            confirm_quote,
            declaration_fingerprint,
            declared_volume,
            lock_pricing,
            quote_in_session,
            weight_pricing_selected,
        )
        from tirodhan.modules.collection_requests.ports import DeclaredRequestItem

        await lock_pricing(session)
        declared_fingerprint = None
        weight_quote_fingerprint = None
        if await weight_pricing_selected(session):
            if not command.items:
                raise OperationalInputError("HOLD_WEIGHT_DECLARATIONS_REQUIRED")
            quote = await quote_in_session(
                session, tuple(DeclaredRequestItem(**i.model_dump()) for i in command.items)
            )
            confirm_quote(quote, command.confirmed_quote)
            assert quote.weight_facts is not None and quote.declared_items is not None
            if command.mass_g != quote.weight_facts.total_weight_grams:
                raise OperationalInputError("HOLD_DECLARED_MASS_MISMATCH")
            volume = declared_volume(quote.declared_items)
            if volume is None and customer_checkout:
                raise OperationalInputError("ITEM_VOLUME_DECLARATIONS_REQUIRED")
            if volume is not None and command.volume_ml != volume:
                raise OperationalInputError("HOLD_DECLARED_VOLUME_MISMATCH")
            declared_fingerprint = declaration_fingerprint(quote.declared_items)
            weight_quote_fingerprint = quote.weight_facts.quote_fingerprint
        elif command.items is not None or command.confirmed_quote is not None:
            raise OperationalInputError("WEIGHT_PRICING_POLICY_REQUIRED")
        # Reattempts keep the same Payment-first authority used by customer consent.
        original = None
        if command.reattempt_request_id:
            from tirodhan.modules.planning.models import PickupExecution

            from .authority import lock_booking_population

            original = (await lock_booking_population(session, [command.reattempt_request_id])).get(
                command.reattempt_request_id
            )
            execution = await session.scalar(
                select(PickupExecution).where(
                    PickupExecution.request_id == command.reattempt_request_id
                )
            )
            if (
                original is None
                or original.customer_id != customer_id
                or original.serviceability_context_id != command.context_id
                or original.status not in ("ACCEPTED", "PLANNED")
                or (execution and execution.status != "PENDING_ASSIGNMENT")
            ):
                raise OperationalInputError("REATTEMPT_BOOKING_REQUIRED")
        context = await session.scalar(
            select(ServiceabilityContext)
            .where(ServiceabilityContext.serviceability_context_id == command.context_id)
            .with_for_update(key_share=True)
        )
        if (
            context is None
            or context.user_id != customer_id
            or (not original and (context.status != "SERVICEABLE" or context.expires_at <= now))
        ):
            raise OperationalInputError("SERVICEABILITY_CONTEXT_REQUIRED")
        mapping = await session.get(RegionCell, original.cell_id if original else context.cell_id)
        if mapping is None:
            raise OperationalInputError("OPERATING_REGION_REQUIRED")
        region = await session.scalar(
            select(OperatingRegion)
            .where(OperatingRegion.region_id == mapping.region_id)
            .with_for_update()
        )
        head = await session.scalar(
            select(AdmissionSourceHead)
            .where(AdmissionSourceHead.region_id == mapping.region_id)
            .with_for_update()
        )
        now = max(now, utc_now())
        if not original and context.expires_at <= now:
            raise OperationalInputError("SERVICEABILITY_CONTEXT_REQUIRED")
        validate_slot(command.slot_start, command.slot_end, now=now)
        if expires_at <= now:
            raise OperationalInputError("SLOT_HOLD_EXPIRED")
        projection = await session.get(
            RegionalSlotProjection, (mapping.region_id, command.slot_start), populate_existing=True
        )
        if (
            region is None
            or head is None
            or projection is None
            or projection.source_version != head.version
            or projection.valid_until <= now
            or projection.generation != command.expected_generation
            or projection.status not in ("AVAILABLE", "LIMITED")
        ):
            raise OperationalInputError("SLOT_PROJECTION_UNAVAILABLE")
        from .allowance_policy import current_policy

        policy = await current_policy(session, region.region_id)
        if policy is None:
            raise OperationalInputError("SLOT_ESTIMATOR_UNCONFIGURED")
        if policy.version != projection.policy_version:
            raise OperationalInputError("SLOT_POLICY_CHANGED")
        if (
            command.pickup_seconds > region.policy["minimum_pickup_seconds"]
            or command.discharge_seconds > region.policy["minimum_discharge_seconds"]
        ):
            raise OperationalInputError("SLOT_SERVICE_CLASS_UNSUPPORTED")
        pickup = max(command.pickup_seconds, int(region.policy["minimum_pickup_seconds"]))
        discharge = max(command.discharge_seconds, int(region.policy["minimum_discharge_seconds"]))
        work = Workload(command.slot_start, command.slot_end, pickup, discharge)
        budgets = list(
            await session.scalars(
                select(RegionalCapacityBudget).where(
                    RegionalCapacityBudget.region_id == region.region_id,
                    RegionalCapacityBudget.generation == projection.generation,
                    # Old buckets remain for debit audit; never scan that retained history
                    # on a customer hold. Current inventory spans at most seven dates.
                    RegionalCapacityBudget.bucket_start >= now - timedelta(minutes=30),
                    RegionalCapacityBudget.bucket_start < now + timedelta(days=8),
                )
            )
        )
        remaining = {
            (b.bucket_start, b.kind): max(0, b.capacity_seconds - b.used_seconds) for b in budgets
        }
        if not remaining_allowance(work, policy, remaining):
            raise OperationalInputError("SLOT_FULL")
        region.version += 1
        reservation = CapacityReservation(
            reservation_id=command.client_command_id,
            region_id=region.region_id,
            region_version=region.version,
            customer_id=customer_id,
            context_id=context.serviceability_context_id,
            pickup_location=context.location,
            slot_start=command.slot_start,
            slot_end=command.slot_end,
            mass_g=(command.mass_g * int(region.policy["safety_percent"]) + 99) // 100,
            volume_ml=(command.volume_ml * int(region.policy["safety_percent"]) + 99) // 100,
            declared_mass_g=command.mass_g if declared_fingerprint else None,
            declaration_fingerprint=declared_fingerprint,
            weight_quote_fingerprint=weight_quote_fingerprint,
            pickup_seconds=pickup,
            discharge_seconds=discharge,
            receiving_point_id=region.default_warehouse_id,
            discharge_deadline=max(
                now.replace(microsecond=0), command.slot_start - timedelta(hours=12)
            )
            + timedelta(days=1),
            expires_at=expires_at,
            fingerprint=fingerprint,
            admission_basis="PROJECTED_ALLOWANCE",
            estimated_duty_seconds=sum(footprint(work, policy).values()),
            estimator_policy_version=policy.version,
            certificate=None,
            created_at=now,
        )
        session.add(reservation)
        await session.flush()
        debit = {(bucket, "DUTY"): seconds for bucket, seconds in footprint(work, policy).items()}
        debit[(work.slot_start, "PICKUP")] = pickup
        session.add_all(
            SlotReservationDebit(
                reservation_id=reservation.reservation_id,
                region_id=region.region_id,
                bucket_start=bucket,
                kind=kind,
                generation=projection.generation,
                seconds=seconds,
            )
            for (bucket, kind), seconds in sorted(debit.items())
        )
        await (
            session.flush()
        )  # PostgreSQL conditionally consumes every shared budget, or rolls back all.
        for key, seconds in debit.items():
            remaining[key] -= seconds
        projection.holds += 1
        projection.pickup_seconds += pickup
        await wake(session, "SLOT_HELD", now, region_id=region.region_id)
        await session.flush()
        await session.refresh(head)
        rows = list(
            await session.scalars(
                select(RegionalSlotProjection).where(
                    RegionalSlotProjection.region_id == region.region_id
                )
            )
        )
        for row in rows:
            if row.generation != projection.generation:
                continue
            row.remaining_bookings = remaining_allowance(
                Workload(
                    row.slot_start,
                    row.slot_end,
                    int(region.policy["minimum_pickup_seconds"]),
                    int(region.policy["minimum_discharge_seconds"]),
                ),
                policy,
                remaining,
            )
            row.status = (
                "FULL"
                if row.remaining_bookings == 0
                else "LIMITED"
                if row.remaining_bookings <= policy.limited_bookings
                else "AVAILABLE"
            )
            # Only this command's known hold/version deltas occurred under the head lock.
            row.source_version = head.version
            row.valid_until = min(row.valid_until, expires_at)
        await append_outbox_event(
            session,
            event_key=f"slot-held:{reservation.reservation_id}",
            event_type="SlotCapacityHeld",
            aggregate_type="regional_capacity_reservation",
            aggregate_id=reservation.reservation_id,
            payload={"reservation_id": str(reservation.reservation_id)},
            available_at=now,
        )
        await complete_idempotency_record(
            session,
            claim.record,
            result_resource_id=reservation.reservation_id,
            result_status_code=201,
            completed_at=now,
        )
        return reservation.reservation_id
