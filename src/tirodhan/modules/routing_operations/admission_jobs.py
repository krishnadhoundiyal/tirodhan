"""Durable preparation outside the API; binding a certificate remains synchronous."""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.planning.routing.contracts import OptimizerEngine, RouteOptimizer
from tirodhan.modules.planning.routing.execution import RoutingAdapterError
from tirodhan.modules.planning.routing.validation import RoutingValidationError

from .admission import ReserveCapacity, reserve_capacity
from .commands import OperationalInputError
from .orchestration_models import CapacityAdmissionRequest
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
        values = command.model_dump(exclude={"client_command_id"})
        row = CapacityAdmissionRequest(
            admission_id=command.client_command_id,
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
) -> int:
    async with factory() as session, session.begin():
        expired = await session.scalars(
            select(CapacityAdmissionRequest)
            .where(
                CapacityAdmissionRequest.status.in_(("PENDING", "NEEDS_ACCEPTANCE")),
                CapacityAdmissionRequest.expires_at <= clock(),
            )
            .order_by(CapacityAdmissionRequest.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        for expired_row in expired:
            expired_row.status, expired_row.failure_code = "FAILED", "ADMISSION_EXPIRED"
    deadline = time.monotonic() + max_wall_seconds
    count = 0
    for _ in range(limit):
        if time.monotonic() >= deadline:
            break
        now = clock()
        async with factory() as session, session.begin():
            row = await session.scalar(
                select(CapacityAdmissionRequest)
                .where(
                    or_(
                        CapacityAdmissionRequest.status == "PENDING",
                        (CapacityAdmissionRequest.status == "RUNNING")
                        & (CapacityAdmissionRequest.lease_until <= now),
                    )
                )
                .order_by(
                    CapacityAdmissionRequest.created_at, CapacityAdmissionRequest.admission_id
                )
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if row is None:
                break
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
                    now=clock(),
                ),
                timeout=min(max(1, lease_seconds - 5), max(1, deadline - time.monotonic())),
            )
        except (OperationalInputError, RoutingAdapterError, RoutingValidationError) as error:
            failure = str(error)
            retry = (
                failure == "CAPACITY_PROJECTION_STALE_RETRY"
                and row.attempts < 3
                and row.expires_at > clock()
            )
        except TimeoutError:
            failure, retry = "ADMISSION_WORK_TIMEOUT", row.attempts < 3 and row.expires_at > clock()
        except DBAPIError as error:
            if getattr(error.orig, "sqlstate", None) not in ("40001", "40P01"):
                raise
            failure, retry = (
                "CAPACITY_TRANSACTION_RETRY",
                row.attempts < 3 and row.expires_at > clock(),
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
                if failure in ("NO_FEASIBLE_REGIONAL_CAPACITY", "NO_COMMITTED_CAPACITY"):
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
                current.lease_token = current.lease_until = None
        count += 1
    return count
