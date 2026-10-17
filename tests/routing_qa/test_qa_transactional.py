import asyncio
from uuid import UUID

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.orm import declarative_base

from tirodhan.core.config import Settings
from tirodhan.db.session import create_database_engine


def uid(value: int) -> UUID:
    return UUID(int=value)


Base = declarative_base()


class MockRiderAssignment(Base):
    __tablename__ = "mock_rider_assignment"
    id = sa.Column(sa.String, primary_key=True)
    rider_id = sa.Column(sa.String, nullable=True)
    version = sa.Column(sa.Integer)


@pytest_asyncio.fixture
async def disposable_engine():
    # Only test if integration DB available
    settings = Settings(
        _env_file=None,
        TIRODHAN_DATABASE_URL="postgresql+asyncpg://tirodhan:tirodhan@localhost:5432/tirodhan",
    )
    try:
        engine = create_database_engine(settings)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield engine
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()
    except Exception:
        yield None


@pytest.mark.asyncio
async def test_racing_transactions_do_not_double_book_riders(disposable_engine):
    # This simulates a race condition to ensure we don't double book a rider using Serializable transactions.

    if disposable_engine is None:
        pytest.skip("Integration database not available")
        return

    async with disposable_engine.begin() as conn:
        await conn.execute(
            sa.insert(MockRiderAssignment).values(id="assignment_1", rider_id=None, version=1)
        )

    async def book_rider(rider_id: str):
        try:
            async with disposable_engine.connect() as conn:
                async with conn.begin():
                    await conn.execute(sa.text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))

                    result = await conn.execute(
                        sa.select(MockRiderAssignment).where(
                            MockRiderAssignment.id == "assignment_1"
                        )
                    )
                    row = result.fetchone()

                    if row.rider_id is None:
                        await asyncio.sleep(0.1)  # Induce race condition
                        await conn.execute(
                            sa.update(MockRiderAssignment)
                            .where(MockRiderAssignment.id == "assignment_1")
                            .values(rider_id=rider_id, version=row.version + 1)
                        )
                        return True
                    return False
        except Exception:
            # We expect a serialization exception on one of the transactions
            return False

    results = await asyncio.gather(book_rider("rider_A"), book_rider("rider_B"))

    # In serializable mode, one of them must fail due to concurrent modification
    assert sum(results) == 1
