"""Generated real-service rolling histories on public roads, with actual native engines."""

import asyncio
import json
import os
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from fleet_verification.database import DatabaseHarness
from geographical_evidence import Ledger, snapshot
from geographical_fixture import DATA, arrivals, setup_region
from geographical_observers import CYCLE, install, observed_workers
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.database import DirectoryBasedExampleDatabase
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    rule,
    run_state_machine_as_test,
)
from test_geographical_rolling import ENABLED, geographical_requirements  # noqa: F401

from tirodhan.db.values import new_uuid7
from tirodhan.modules.collection_requests.cancellation import (
    CancellationConflictError,
    cancel_collection_request_by_customer,
)
from tirodhan.modules.collection_requests.models import CollectionRequest
from tirodhan.modules.planning.routing.osrm import OSRMMatrixProvider
from tirodhan.modules.routing_operations.regional_matrix import RegionalMatrixCache
from tirodhan.modules.routing_operations.regional_planner import run_region

pytestmark = [
    pytest.mark.integration,
    pytest.mark.routing_engine,
    pytest.mark.skipif(not ENABLED, reason="explicit geographical native acceptance opt-in"),
]


class GeographicalHistory(RuleBasedStateMachine):
    def __init__(self, url, root):
        super().__init__()
        self.db = DatabaseHarness(url)
        self.root = root
        self.history = []
        self.history_id = str(new_uuid7())

    @initialize(
        name=st.sampled_from(("North", "East", "West", "South")),
        confirmed=st.booleans(),
        mass=st.sampled_from((2000, 10000, 100000)),
        horizon=st.sampled_from((7200, 10800, 14400)),
    )
    def declare_resources(self, name, confirmed, mass, horizon):
        self.region = self.db.run(
            setup_region(
                self.db.factory, name, 2, horizon, confirmed=confirmed, mass_capacity_g=mass
            )
        )
        self.confirmed = confirmed
        self.history.append(
            dict(
                action="declare_resources",
                name=name,
                confirmed=confirmed,
                mass_capacity_g=mass,
                horizon_seconds=horizon,
            )
        )
        self.ledger = Ledger()
        self.previous = self.db.run(snapshot(self.db.factory, self.region))
        self.sequence = 0
        self.client = httpx.AsyncClient()
        self.matrix = RegionalMatrixCache(
            OSRMMatrixProvider(
                client=self.client,
                endpoint=os.environ["TIRODHAN_OSRM_TEST_URL"],
                graph_version=DATA["source_sha256"],
                profile_version="qa-car",
                max_nodes=512,
                max_snap_distance_m=100,
            ),
            graph=DATA["source_sha256"],
            profile="qa-car",
            block_nodes=128,
            max_pairs=262144,
        )
        initial = self.db.run(arrivals(self.db.factory, self.region, 1, 1))
        self.history.append(dict(action="initial_bookings", exact_inputs=initial))
        self.optimize(60)

    def optimize(self, seconds):
        self.sequence += 1
        self.region["clock"] += timedelta(seconds=seconds)
        token = CYCLE.set(f"stateful:{self.history_id}:{self.sequence}")
        try:
            result = self.db.run(
                run_region(
                    self.db.factory,
                    region_id=self.region["region_id"],
                    providers={"qa-car": self.matrix},
                    clock=lambda: self.region["clock"],
                    workers=observed_workers,
                )
            )
        finally:
            CYCLE.reset(token)
        self.history.append(
            dict(
                action="plan",
                advance_seconds=seconds,
                status=result.status if result else "NO_DUE_CYCLE",
            )
        )
        current = self.db.run(snapshot(self.db.factory, self.region))
        self.ledger.check(self.previous, current)
        self.previous = current
        if self.sequence == 1 and self.confirmed:
            assert result is not None and result.status == "ACTIVATED", (
                "Native priming cycle failed"
            )
        if not self.confirmed:
            assert not current["assignments"], "Unconfirmed capacity received authority"

    @rule(count=st.integers(1, 2), offset=st.integers(0, 200))
    def arrive(self, count, offset):
        # Change future synthetic arrival geography; immutable accepted snapshots are untouched.
        nodes = self.region["nodes"]
        self.region["nodes"] = nodes[offset:] + nodes[:offset]
        created = self.db.run(arrivals(self.db.factory, self.region, count, self.sequence + 1))
        self.history.append(dict(action="arrive", count=count, offset=offset, exact_inputs=created))

    @rule(seconds=st.integers(1, 120))
    def plan(self, seconds):
        self.optimize(seconds)

    @rule(index=st.integers(0, 20))
    def cancel(self, index):
        from uuid import UUID

        from tirodhan.db.values import utc_now

        identity = UUID(self.region["bookings"][index % len(self.region["bookings"])]["booking_id"])

        async def command():
            async with self.db.factory() as session, session.begin():
                booking = await session.get(CollectionRequest, identity)
                return await cancel_collection_request_by_customer(
                    session,
                    request_id=identity,
                    customer_id=booking.customer_id,
                    idempotency_key=str(new_uuid7()),
                    planning_lead_time_minutes=15,
                    idempotency_expires_at=utc_now() + timedelta(days=2),
                )

        try:
            self.db.run(command())
            outcome = "CANCELLED_OR_REPLAY"
        except CancellationConflictError as error:
            outcome = str(error)
        self.history.append(
            dict(action="cancel", index=index, booking_id=str(identity), outcome=outcome)
        )

    @invariant()
    def preserve_ownership_and_historical_bookings(self):
        current = self.db.run(snapshot(self.db.factory, self.region))
        self.ledger.check(self.previous, current)
        assert len(current["bookings"]) == len(self.region["bookings"])
        self.previous = current

    def teardown(self):
        try:
            path = self.root / f"history-{new_uuid7()}.json"
            path.write_text(
                json.dumps(
                    dict(
                        history_id=self.history_id,
                        seed=1729,
                        actions=self.history,
                        final_snapshot=self.db.run(snapshot(self.db.factory, self.region))
                        if hasattr(self, "region")
                        else None,
                    ),
                    default=str,
                    indent=2,
                )
                + "\n"
            )
            if hasattr(self, "client"):
                self.db.run(self.client.aclose())
        finally:
            self.db.close()


@pytest.mark.asyncio
async def test_real_geographical_generated_histories(migrated_database_url, monkeypatch):
    from tirodhan.core.logging import configure_logging

    configure_logging("WARNING")
    root = Path(os.environ["TIRODHAN_GEO_ARTIFACTS"]) / "stateful"
    (root / "trace").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("TIRODHAN_GEO_TRACE", str(root / "trace"))
    install(monkeypatch)
    await asyncio.to_thread(
        run_state_machine_as_test,
        lambda: GeographicalHistory(migrated_database_url, root),
        settings=settings(
            max_examples=3,
            stateful_step_count=5,
            deadline=None,
            print_blob=True,
            # Native process/DB execution between draws is not strategy-generation work.
            suppress_health_check=[HealthCheck.too_slow],
            database=DirectoryBasedExampleDatabase(str(root / "hypothesis")),
        ),
    )
