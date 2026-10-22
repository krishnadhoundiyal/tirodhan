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
from tirodhan.modules.collection_requests.models import CollectionRequest
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
            # Write only columns that existed at this migration boundary. Current ORM
            # item metadata includes nullable post-0037 declaration provenance.
            await session.execute(
                text(
                    "INSERT INTO collection_request_item(request_item_id,request_id,"
                    "item_category_code,declared_quantity,quoted_line_amount_minor,currency,"
                    "pricing_rule_version,created_at) VALUES(:item,:request,'TEST_LEGACY',1,"
                    ":amount,:currency,'legacy-fixture-1',:now)"
                ),
                {
                    "item": new_uuid7(),
                    "request": request.request_id,
                    "amount": request.quoted_amount_minor,
                    "currency": request.currency,
                    "now": utc_now(),
                },
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


def pre_weight_item_snapshot(row):
    """Old financial round trips omit only explicitly unknown additive provenance."""
    assert row.get("weight_band") is None and row.get("weight_kg") is None
    return {key: value for key, value in row.items() if key not in {"weight_band", "weight_kg"}}
