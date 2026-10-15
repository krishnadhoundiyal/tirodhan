"""Opt-in measured command/reconciliation batching and incremental admission costs."""

import os
import time
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from test_operational_orchestration import admission, regional, reserve
from test_routing_fleet import LineMatrix, engines, tick

from tirodhan.db.values import new_uuid7
from tirodhan.modules.collection_requests.cancellation import cancel_collection_request_by_customer
from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.routing_operations.dispatch_models import VehicleRoute
from tirodhan.modules.routing_operations.orchestration_models import CapacityReservation
from tirodhan.modules.routing_operations.reconciliation import reconcile_cancellations

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("TIRODHAN_ORCHESTRATION_BENCHMARK") != "1",
        reason="explicit performance campaign required",
    ),
]


@pytest.mark.parametrize("batched", [False, True])
@pytest.mark.parametrize("separate_routes", [False, True])
async def test_batching_measurements(database_session_factory, batched, separate_routes):
    import json

    factory = database_session_factory
    data = await regional(factory, second=True)
    _, vehicles, _, ids, _, resource, clock, _, customer, context = data
    if separate_routes:
        from geoalchemy2.elements import WKTElement

        from tirodhan.modules.routing_operations.commands import PositionReport

        async with factory() as session, session.begin():
            second = await session.get(CollectionRequest, ids[1])
            delta = ((second.slot_end - second.slot_start).total_seconds() + 600) / 100000
            longitude = 77.208 + delta
            second.pickup_location = WKTElement(f"POINT({longitude} 28.631)", srid=4326)
        await resource(
            PositionReport(
                client_command_id=new_uuid7(),
                vehicle_id=vehicles[1],
                longitude=longitude,
                latitude=28.631,
                observed_at=clock[0] + timedelta(seconds=1),
            ),
            at=clock[0] + timedelta(seconds=1),
        )
        clock[0] += timedelta(seconds=1)
    assert (await tick(factory, clock)).status == "ACTIVATED"
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(VehicleRoute)) == (
            2 if separate_routes else 1
        )
    canceled, reconciliation = [], []
    for request_id in ids:
        async with factory() as session:
            booking = await session.get(CollectionRequest, request_id)
        started = time.perf_counter()
        async with factory() as session, session.begin():
            await cancel_collection_request_by_customer(
                session,
                request_id=request_id,
                customer_id=booking.customer_id,
                idempotency_key=str(new_uuid7()),
                planning_lead_time_minutes=15,
                idempotency_expires_at=clock[0] + timedelta(days=1),
            )
        canceled.append(time.perf_counter() - started)
        if not batched:
            started = time.perf_counter()
            assert await reconcile_cancellations(factory, clock[0]) == 1
            reconciliation.append(time.perf_counter() - started)
    if batched:
        started = time.perf_counter()
        assert await reconcile_cancellations(factory, clock[0]) == len(ids)
        reconciliation.append(time.perf_counter() - started)
    matrix, solvers = LineMatrix(), engines()
    admissions = []
    for _ in range(4):
        command = admission(context, clock[0], mass_g=100, volume_ml=100)
        started = time.perf_counter()
        certificate = await reserve(
            factory, customer, command, clock[0], matrix=matrix, solvers=solvers
        )
        async with factory() as session:
            row = await session.get(CapacityReservation, certificate)
        admissions.append(
            {
                "seconds": time.perf_counter() - started,
                "incremental": row.certificate["incremental"],
                "cumulative_solver_calls": sum(e.calls for e in solvers.values()),
            }
        )
    print(
        "INC7_BATCH_BENCHMARK",
        json.dumps(
            {
                "synthetic_matrix_and_seed_engines": True,
                "batched": batched,
                "separate_routes": separate_routes,
                "cancellation_transactions_seconds": canceled,
                "batch_reconciliation_seconds": reconciliation,
                "admissions": admissions,
                "matrix_calls": matrix.calls,
            }
        ),
    )
