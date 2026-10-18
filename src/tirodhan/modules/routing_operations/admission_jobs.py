"""Durable preparation outside the API; binding a certificate remains synchronous."""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import or_, select, true
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.collection_requests.scheduling import SlotConflictError
from tirodhan.modules.planning.routing.contracts import OptimizerEngine, RouteOptimizer
from tirodhan.modules.planning.routing.execution import RoutingAdapterError
from tirodhan.modules.planning.routing.validation import RoutingValidationError

from .admission import ReserveCapacity, reserve_capacity
from .admission_failures import retryable
from .commands import OperationalInputError
from .orchestration_models import (
    CapacityAdmissionRequest,
    CapacityReservation,
    OperatingRegion,
    RegionCell,
)
from .snapshot import MatrixSource


async def request_admission(
    factory: async_sessionmaker[AsyncSession],
    *,
    customer_id: UUID,
    command: ReserveCapacity,
    expires_at: datetime,
    now: datetime | None = None,
) -> CapacityAdmissionRequest:
    now = now or utc_now()
    fingerprint = hashlib.sha256(command.model_dump_json().encode()).digest()
    async with factory() as session, session.begin():
        from .commands import require_live_role

        await require_live_role(session, customer_id, "CUSTOMER")
        from tirodhan.modules.reliability.primitives import (
            claim_idempotency_record,
            complete_idempotency_record,
        )

        claim = await claim_idempotency_record(
            session,
            scope=f"routing.admission:{customer_id}",
            idempotency_key=str(command.client_command_id),
            request_fingerprint=fingerprint,
            expires_at=expires_at,
            now=now,
        )
        existing = await session.get(CapacityAdmissionRequest, command.client_command_id)
        if existing is not None:
            if existing.customer_id != customer_id or existing.fingerprint != fingerprint:
                raise OperationalInputError("ADMISSION_REPLAY_CONFLICT")
            return existing
        if not claim.created:
            raise OperationalInputError("ADMISSION_IN_PROGRESS")
        from tirodhan.modules.collection_requests.models import CollectionRequest
        from tirodhan.modules.serviceability.models import ServiceabilityContext

        context = await session.get(ServiceabilityContext, command.context_id)
        original = (
            await session.get(CollectionRequest, command.reattempt_request_id)
            if command.reattempt_request_id
            else None
        )
        if (
            context is None
            or context.user_id != customer_id
            or (
                command.reattempt_request_id is not None
                and (
                    original is None
                    or original.customer_id != customer_id
                    or original.serviceability_context_id != command.context_id
                )
            )
        ):
            raise OperationalInputError("SERVICEABILITY_CONTEXT_REQUIRED")
        membership = (
            await session.get(RegionCell, original.cell_id if original else context.cell_id)
            if (original or context.cell_id)
            else None
        )
        if membership is None:
            raise OperationalInputError("OPERATING_REGION_REQUIRED")
        values = command.model_dump(exclude={"client_command_id"})
        row = CapacityAdmissionRequest(
            admission_id=command.client_command_id,
            region_id=membership.region_id,
            customer_id=customer_id,
            **values,
            fingerprint=fingerprint,
            status="PENDING",
            expires_at=expires_at,
            created_at=now,
        )
        session.add(row)
        await session.flush()
        await complete_idempotency_record(
            session,
            claim.record,
            result_resource_id=row.admission_id,
            result_status_code=202,
            completed_at=now,
        )
        return row


async def run_admissions(
    factory: async_sessionmaker[AsyncSession],
    *,
    providers: dict[str, MatrixSource],
    optimizers: dict[OptimizerEngine, RouteOptimizer],
    limit: int = 16,
    lease_seconds: int = 300,
    clock: Callable[[], datetime] = utc_now,
    max_wall_seconds: int = 120,
    retry_delay_seconds: int = 5,
    region_id: UUID | None = None,
) -> int:
    if not 1 <= retry_delay_seconds <= 60:
        raise ValueError("retry delay must be between 1 and 60 seconds")
    async with factory() as session, session.begin():
        expired = await session.scalars(
            select(CapacityAdmissionRequest)
            .where(
                CapacityAdmissionRequest.region_id == region_id if region_id else true(),
                CapacityAdmissionRequest.status.in_(("PENDING", "RUNNING", "NEEDS_ACCEPTANCE")),
                CapacityAdmissionRequest.expires_at <= clock(),
            )
            .order_by(CapacityAdmissionRequest.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        for expired_row in expired:
            # A lost response cannot erase a certificate already committed under this
            # intent. Its historical success is distinct from current bind permission.
            committed = await session.get(CapacityReservation, expired_row.admission_id)
            expired_row.status, expired_row.failure_code = (
                ("SUCCEEDED", None) if committed else ("FAILED", "ADMISSION_EXPIRED")
            )
            expired_row.lease_token = expired_row.lease_until = None
            expired_row.next_attempt_at = None
            expired_row.preparation_cursor = {}
    from .admission_leases import claim, release

    regional_token = new_uuid7()
    deadline = time.monotonic() + max_wall_seconds
    count = 0
    try:
        for _ in range(limit):
            if time.monotonic() >= deadline:
                break
            now = clock()
            async with factory() as session, session.begin():
                owner = aliased(CapacityAdmissionRequest)
                occupied = (
                    select(owner.admission_id)
                    .where(
                        owner.region_id == CapacityAdmissionRequest.region_id,
                        owner.status == "RUNNING",
                        owner.admission_id != CapacityAdmissionRequest.admission_id,
                    )
                    .exists()
                )
                row = await session.scalar(
                    select(CapacityAdmissionRequest)
                    .where(
                        CapacityAdmissionRequest.region_id == region_id if region_id else true(),
                        or_(
                            (CapacityAdmissionRequest.status == "PENDING")
                            & or_(
                                CapacityAdmissionRequest.next_attempt_at.is_(None),
                                CapacityAdmissionRequest.next_attempt_at <= now,
                            ),
                            (CapacityAdmissionRequest.status == "RUNNING")
                            & (CapacityAdmissionRequest.lease_until <= now),
                        ),
                        CapacityAdmissionRequest.expires_at > now,
                        ~occupied,
                    )
                    .order_by(
                        CapacityAdmissionRequest.created_at, CapacityAdmissionRequest.admission_id
                    )
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
                if row is None:
                    break
                claim_region = row.region_id
                if claim_region is None:
                    # Legacy intents may predate resolved serviceability/membership.
                    # Resolve before entering the regional owner protocol; never let a
                    # nullable historical FK become an uncoordinated preparation lane.
                    from sqlalchemy import func

                    from tirodhan.modules.collection_requests.models import CollectionRequest
                    from tirodhan.modules.serviceability.models import ServiceabilityContext

                    claim_region = await session.scalar(
                        select(RegionCell.region_id)
                        .select_from(ServiceabilityContext)
                        .outerjoin(
                            CollectionRequest,
                            CollectionRequest.request_id == row.reattempt_request_id,
                        )
                        .join(
                            RegionCell,
                            RegionCell.cell_id
                            == func.coalesce(
                                CollectionRequest.cell_id, ServiceabilityContext.cell_id
                            ),
                        )
                        .where(ServiceabilityContext.serviceability_context_id == row.context_id)
                    )
                    if claim_region is None:
                        row.status, row.failure_code = "FAILED", "OPERATING_REGION_REQUIRED"
                        row.lease_token = row.lease_until = None
                        count += 1
                        continue
                if claim_region is not None:
                    # Only the short claim transaction holds this lock. The durable
                    # RUNNING row/unique index owns regional preparation during roads/
                    # native work; independent regions can continue immediately.
                    region = await session.scalar(
                        select(OperatingRegion)
                        .where(OperatingRegion.region_id == claim_region)
                        .with_for_update(skip_locked=True)
                    )
                    if region is None:
                        continue
                    active = await session.scalar(
                        select(CapacityAdmissionRequest.admission_id)
                        .where(
                            CapacityAdmissionRequest.region_id == claim_region,
                            CapacityAdmissionRequest.status == "RUNNING",
                            CapacityAdmissionRequest.admission_id != row.admission_id,
                        )
                        .limit(1)
                    )
                    if active is not None:
                        if row.region_id is None:
                            row.region_id, row.status = claim_region, "PENDING"
                            row.lease_token = row.lease_until = None
                            row.next_attempt_at = now + timedelta(seconds=retry_delay_seconds)
                        continue
                if not await claim(
                    session,
                    claim_region,
                    regional_token,
                    now,
                    now + timedelta(seconds=lease_seconds),
                ):
                    continue
                row.region_id = claim_region
                row.status, row.lease_token, row.lease_until = (
                    "RUNNING",
                    new_uuid7(),
                    now + timedelta(seconds=lease_seconds),
                )
                row.attempts += 1
                token = row.lease_token
                command = ReserveCapacity(
                    client_command_id=row.admission_id,
                    context_id=row.context_id,
                    reattempt_request_id=row.reattempt_request_id,
                    slot_start=row.slot_start,
                    slot_end=row.slot_end,
                    mass_g=row.mass_g,
                    volume_ml=row.volume_ml,
                    pickup_seconds=row.pickup_seconds,
                    discharge_seconds=row.discharge_seconds,
                )
            failure = None
            retry = False
            try:
                await asyncio.wait_for(
                    reserve_capacity(
                        factory,
                        customer_id=row.customer_id,
                        command=command,
                        providers=providers,
                        optimizers=optimizers,
                        expires_at=row.expires_at,
                        lease_token=token,
                        regional_lease_token=regional_token,
                        projection_time=row.created_at,
                        now=clock(),
                    ),
                    timeout=min(max(1, lease_seconds - 5), max(1, deadline - time.monotonic())),
                )
            except SlotConflictError:
                failure = "SLOT_UNAVAILABLE"
            except RoutingAdapterError as error:
                failure = error.code
                # Invalid provider output is not evidence of exhausted physical capacity.
                # Retry durably with backoff until the customer's preparation intent expires.
                retry = row.expires_at > clock()
            except (OperationalInputError, RoutingValidationError) as error:
                failure = str(error)
                retry = retryable(failure) and row.expires_at > clock()
            except TimeoutError:
                failure, retry = "ADMISSION_WORK_TIMEOUT", row.expires_at > clock()
            except DBAPIError as error:
                if getattr(error.orig, "sqlstate", None) not in ("40001", "40P01"):
                    raise
                failure, retry = (
                    "CAPACITY_TRANSACTION_RETRY",
                    row.expires_at > clock(),
                )
            async with factory() as session, session.begin():
                current = await session.scalar(
                    select(CapacityAdmissionRequest)
                    .where(CapacityAdmissionRequest.admission_id == row.admission_id)
                    .with_for_update()
                )
                assert current is not None
                if (
                    current.lease_token == token
                    and current.lease_until is not None
                    and current.lease_until > clock()
                ):
                    waiting_for_acceptance = False
                    if failure in (
                        "NO_FEASIBLE_REGIONAL_CAPACITY",
                        "NO_COMMITTED_CAPACITY",
                        "UNKNOWN_SEARCH_EXHAUSTED",
                    ):
                        from .flexible import offer_admission

                        waiting_for_acceptance = await offer_admission(session, current, clock())
                    current.status = (
                        "NEEDS_ACCEPTANCE"
                        if waiting_for_acceptance
                        else "PENDING"
                        if retry
                        else "FAILED"
                        if failure
                        else "SUCCEEDED"
                    )
                    current.failure_code = failure
                    if current.status != "PENDING":
                        current.preparation_cursor = {}
                    current.next_attempt_at = (
                        clock()
                        + timedelta(
                            seconds=min(60, retry_delay_seconds * 2 ** min(current.attempts - 1, 4))
                        )
                        if retry
                        else None
                    )
                    current.lease_token = current.lease_until = None
            count += 1
    finally:
        await release(factory, regional_token)
    return count
