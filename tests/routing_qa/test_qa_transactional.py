import asyncio
import os
from datetime import datetime, timezone
from uuid import UUID

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.orm import declarative_base

from tirodhan.core.config import Settings
from tirodhan.db.base import Base
from tirodhan.db.session import create_database_engine
from tirodhan.modules.dispatch.models import AssignmentOffer, RiderAssignment, RiderProfile
from tirodhan.modules.planning.models import CollectionGroup, PlanningBatch


def uid(value: int) -> UUID:
    return UUID(int=value)


@pytest_asyncio.fixture
async def disposable_engine():
    database_url = os.getenv("TIRODHAN_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TIRODHAN_TEST_DATABASE_URL not set in environment. Skipping.")
        return

    settings = Settings(_env_file=None, TIRODHAN_DATABASE_URL=database_url)
    try:
        engine = create_database_engine(settings)
        # Verify db is reachable
        async with engine.connect() as conn:
            await conn.execute(sa.text("SELECT 1"))
    except Exception:
        pytest.skip("Database is unreachable. Skipping.")
        return

    # Create schema for required dependencies
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_racing_transactions_do_not_double_book_riders(disposable_engine):
    # This simulates a race condition using the real DB schemas
    if disposable_engine is None:
        pytest.skip("No engine available")

    now = datetime.now(timezone.utc)

    # Needs some seeded data to comply with FK requirements on actual real models
    rider_1_id = uid(1)
    rider_2_id = uid(2)
    group_id = uid(3)
    offer_id = uid(4)
    batch_id = uid(5)

    async with disposable_engine.begin() as conn:
        # Since RiderProfile expects an AppUser due to FK, we use raw insert
        await conn.execute(
            sa.text(
                """
            INSERT INTO app_user (user_id, phone, status, created_at, updated_at)
            VALUES (:r1, '9999999991', 'ACTIVE', :now, :now),
                   (:r2, '9999999992', 'ACTIVE', :now, :now)
        """
            ),
            {"r1": rider_1_id, "r2": rider_2_id, "now": now},
        )

        await conn.execute(
            sa.insert(RiderProfile).values(
                [
                    {
                        "rider_id": rider_1_id,
                        "status": "ACTIVE",
                        "created_at": now,
                        "updated_at": now,
                    },
                    {
                        "rider_id": rider_2_id,
                        "status": "ACTIVE",
                        "created_at": now,
                        "updated_at": now,
                    },
                ]
            )
        )

        await conn.execute(
            sa.insert(PlanningBatch).values(
                planning_batch_id=batch_id,
                cell_id="x",
                slot_start=now,
                slot_end=now,
                status="COMPLETED",
                max_attempts_snapshot=1,
                created_at=now,
            )
        )

        await conn.execute(
            sa.insert(CollectionGroup).values(
                collection_group_id=group_id,
                planning_batch_id=batch_id,
                planning_mode="SINGLE",
                created_at=now,
            )
        )

        # Open offer waiting for riders to accept
        await conn.execute(
            sa.insert(AssignmentOffer).values(
                offer_id=offer_id,
                collection_group_id=group_id,
                rider_id=rider_1_id,
                offer_round=1,
                status="OPEN",
                offered_at=now,
                expires_at=now,
            )
        )

    async def book_rider(rider_id: UUID, new_assignment_id: UUID):
        try:
            async with disposable_engine.connect() as conn:
                async with conn.begin():
                    # Request real serializable isolation level
                    await conn.execute(sa.text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))

                    # Verify no active assignment exists for this group
                    result = await conn.execute(
                        sa.select(RiderAssignment).where(
                            (RiderAssignment.collection_group_id == group_id)
                            & (RiderAssignment.status == "ACTIVE")
                        )
                    )
                    existing = result.fetchall()

                    if len(existing) == 0:
                        await asyncio.sleep(0.1)  # Induce race condition pause

                        await conn.execute(
                            sa.insert(RiderAssignment).values(
                                assignment_id=new_assignment_id,
                                collection_group_id=group_id,
                                rider_id=rider_id,
                                source="RIDER_OFFER_ACCEPTED",
                                status="ACTIVE",
                                created_at=now,
                                assigned_at=now,
                            )
                        )
                        return True
                    return False
        except Exception:
            # Expected serialization exception on commit
            return False

    results = await asyncio.gather(book_rider(rider_1_id, uid(10)), book_rider(rider_2_id, uid(11)))

    # Using actual RiderAssignment schema with UniqueConstraint rules on active groups
    assert sum(results) == 1
