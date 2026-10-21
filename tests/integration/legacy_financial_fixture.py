"""Seed pre-administration history at its actual schema boundary, before adopting revisions.

Current operational services intentionally require current schema. Migration preservation
tests must not create new immutable administration history merely to seed old finances.
"""

import asyncio
from contextlib import asynccontextmanager

from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from test_rider_dispatch import create_fixture

from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.collection_requests.models import CollectionRequest, CollectionRequestItem
from tirodhan.modules.payments.models import Payment, PaymentAttempt


@asynccontextmanager
async def pre_administration_writer(factory):
    """Seed an old-writer fixture, before adopting administration history.

    Some prior migration tests need current regional fixture services, including
    mapped fact columns. Suppress only the *new* capture/defaults while seeding
    their pre-0033 facts. Never delete or rewrite an existing revision, disable
    immutable-history guards, or use this for an administrative behaviour test.
    The actual downgrade/upgrade then runs normally and backfills its baselines.
    """
    from tirodhan.modules.business_admin.models import REVISIONS

    async with factory() as session, session.begin():
        for source, table in REVISIONS.items():
            assert not await session.scalar(select(table.c.revision).limit(1))
            await session.execute(text(f'ALTER TABLE "{source}" DISABLE TRIGGER business_revision'))
        facts = list(
            await session.scalars(
                text(
                    "SELECT table_name FROM information_schema.columns "
                    "WHERE table_schema='public' AND column_name='recorded_command_id'"
                )
            )
        )
        for source in facts:
            await session.execute(
                text(
                    f'ALTER TABLE "{source}" ALTER COLUMN recorded_at DROP DEFAULT, '
                    "ALTER COLUMN recorded_command_id DROP DEFAULT"
                )
            )
    try:
        yield
    finally:
        async with factory() as session, session.begin():
            for source in REVISIONS:
                await session.execute(
                    text(f'ALTER TABLE "{source}" ENABLE TRIGGER business_revision')
                )
            for source in facts:
                await session.execute(
                    text(
                        f'ALTER TABLE "{source}" ALTER COLUMN recorded_at '
                        "SET DEFAULT CURRENT_TIMESTAMP, ALTER COLUMN "
                        "recorded_command_id SET DEFAULT "
                        "nullif(current_setting('tirodhan.command',true),'')::uuid"
                    )
                )


async def legacy_financial_fixture(factory):
    await asyncio.to_thread(
        command.downgrade, Config("alembic.ini"), "0032_projected_slot_allowances"
    )
    await create_fixture(factory, group_count=2, pickups_per_group=1)
    async with factory() as session, session.begin():
        for request in await session.scalars(select(CollectionRequest)):
            session.add(
                CollectionRequestItem(
                    request_id=request.request_id,
                    item_category_code="TEST_LEGACY",
                    declared_quantity=1,
                    quoted_line_amount_minor=request.quoted_amount_minor,
                    currency=request.currency,
                    pricing_rule_version="legacy-fixture-1",
                    created_at=utc_now(),
                )
            )
            payment = Payment(
                request_id=request.request_id,
                amount_minor=request.quoted_amount_minor,
                currency=request.currency,
                status="SUCCEEDED",
                succeeded_at=utc_now(),
            )
            session.add(payment)
            await session.flush()
            attempt = PaymentAttempt(
                payment_id=payment.payment_id,
                provider="fake",
                provider_payment_id=str(new_uuid7()),
                provider_idempotency_key=str(new_uuid7()),
                status="SUCCEEDED",
                completed_at=utc_now(),
            )
            session.add(attempt)
            await session.flush()
            payment.successful_attempt_id = attempt.payment_attempt_id
