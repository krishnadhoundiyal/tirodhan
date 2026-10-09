from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any, Literal
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from scheduling_helpers import TestSlotAvailability
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_collection_payment import ITEMS, FixedPricing, create_context, create_request, create_user
from test_collection_request_completion import ready_request
from test_operational_api import _token_codec, _token_for

from tirodhan.api.dependencies import get_current_customer_id
from tirodhan.api.routes import customer_reads
from tirodhan.core.config import Settings
from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.main import create_app
from tirodhan.modules.collection_requests.models import CollectionRequest, CollectionRequestItem
from tirodhan.modules.collection_requests.scheduling import (
    SERVICE_TIMEZONE,
    SlotConflictError,
    SlotWindow,
    UnconfiguredSlotAvailability,
    daily_grid,
)
from tirodhan.modules.collection_requests.service import (
    CreateCollectionRequestCommand,
    complete_collection_request,
    create_collection_request,
)
from tirodhan.modules.customer_reads.models import (
    CatalogueArtwork,
    CatalogueCategory,
    CatalogueGroup,
    CatalogueMedia,
)
from tirodhan.modules.customer_reads.schemas import CollectionDetailDto
from tirodhan.modules.evidence.media_ports import UploadAuthorization
from tirodhan.modules.identity.models import AppUser, RefreshSession, UserRole
from tirodhan.modules.payments.models import Payment, PaymentAttempt, Refund
from tirodhan.modules.planning.locking import acquire_work_unit_advisory_lock
from tirodhan.modules.planning.models import PlanningBatch
from tirodhan.modules.receiving_points.models import ReceivingPoint

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


class ProductStorage:
    async def authorize_product_image(self, *, object_key: str) -> UploadAuthorization:
        return UploadAuthorization(
            opaque_value=f"https://blob.test/{object_key}?sp=r",
            expires_at=utc_now() + timedelta(minutes=5),
        )


def app_for(
    factory: async_sessionmaker[AsyncSession],
    customer: UUID | None,
    protector: Any = None,
    policy: Any = None,
    ttl: int = 600,
) -> Any:
    app = create_app(
        Settings(
            _env_file=None,
            environment="test",
            customer_cursor_signing_key="test-key-32-bytes-long-xxxxxxxxxx",
            customer_cursor_ttl_seconds=ttl,
            planning_lead_time_minutes=4,
            pending_payment_lifetime_seconds=1800,
            command_idempotency_ttl_seconds=3600,
        ),
        access_token_codec=_token_codec(),
        address_protector=protector,
        pricing_port=FixedPricing(),
        slot_availability=policy,
        product_media=ProductStorage(),
    )
    app.state.database_session_factory = factory
    if customer is not None:
        app.dependency_overrides[get_current_customer_id] = lambda: customer
    return app


async def seed_catalogue(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as session, session.begin():
        group = CatalogueGroup(
            group_id=new_uuid7(),
            group_code="TEST",
            display_name="Test group",
            display_order=1,
            active=True,
        )
        asset = CatalogueMedia(
            media_id=new_uuid7(),
            object_key="product-art/test",
            width=100,
            height=100,
            alt_text="Test artwork",
        )
        session.add_all([group, asset])
        await session.flush()
        session.add_all(
            [
                CatalogueCategory(
                    category_id=new_uuid7(),
                    category_code=code,
                    group_id=group.group_id,
                    display_name=f"Test {code}",
                    description="Synthetic test only",
                    display_order=order,
                    active=True,
                    image_id=asset.media_id,
                    thumbnail_id=asset.media_id,
                    quantity_input="OPTIONAL",
                    weight_input="OPTIONAL",
                    quick_label=code,
                    quick_order=order,
                )
                for code, order in [("TEST_B", 2), ("TEST_A", 1)]
            ]
        )
        session.add(CatalogueArtwork(artwork_id=new_uuid7(), kind="hero", media_id=asset.media_id))


async def protected_request(
    factory: async_sessionmaker[AsyncSession], owner: UUID, protector: Any
) -> Any:
    context = await create_context(factory, owner)
    result = await create_request(factory, owner, context.serviceability_context_id)
    async with factory() as session, session.begin():
        await session.execute(
            update(CollectionRequest)
            .where(CollectionRequest.request_id == result.request.request_id)
            .values(
                pickup_address_snapshot_encrypted=await protector.protect("Historical household")
            )
        )
    return result


async def confirm_fixture_payment(session: AsyncSession, result: Any, now: datetime) -> None:
    """Completed/planned fixtures retain the canonical charge required by acceptance."""
    attempt_id = new_uuid7()
    session.add(
        PaymentAttempt(
            payment_attempt_id=attempt_id,
            payment_id=result.payment.payment_id,
            provider="MOCK",
            provider_payment_id=f"charge-{attempt_id}",
            provider_idempotency_key=f"attempt-{attempt_id}",
            status="SUCCEEDED",
            created_at=result.request.created_at,
            completed_at=now,
        )
    )
    await session.flush()
    await session.execute(
        update(Payment)
        .where(Payment.payment_id == result.payment.payment_id)
        .values(status="SUCCEEDED", successful_attempt_id=attempt_id, succeeded_at=now)
    )
    await session.execute(
        update(CollectionRequest)
        .where(CollectionRequest.request_id == result.request.request_id)
        .values(accepted_at=now)
    )


async def test_principal_live_roles_revocation_disablement_and_no_customer_requirement(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user = await create_user(database_session_factory)
    token, session_id = await _token_for(database_session_factory, user.user_id, roles=("RIDER",))
    app = app_for(database_session_factory, None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/v1/auth/me")).status_code == 401
        headers = {"Authorization": f"Bearer {token}"}
        response = await client.get("/v1/auth/me", headers=headers)
        assert response.json() == {"user_id": str(user.user_id), "roles": ["RIDER"]}
        assert "no-store" in response.headers["cache-control"]
        assert (
            await client.get("/v1/customer/collection-catalogue", headers=headers)
        ).status_code == 403
        async with database_session_factory() as session, session.begin():
            await session.execute(
                update(UserRole)
                .where(UserRole.user_id == user.user_id)
                .values(revoked_at=utc_now())
            )
        assert (await client.get("/v1/auth/me", headers=headers)).json()["roles"] == []
        async with database_session_factory() as session, session.begin():
            await session.execute(
                update(AppUser).where(AppUser.user_id == user.user_id).values(status="DISABLED")
            )
        assert (await client.get("/v1/auth/me", headers=headers)).status_code == 401
        async with database_session_factory() as session, session.begin():
            await session.execute(
                update(AppUser).where(AppUser.user_id == user.user_id).values(status="ACTIVE")
            )
            await session.execute(
                update(RefreshSession)
                .where(RefreshSession.refresh_session_id == session_id)
                .values(revoked_at=utc_now())
            )
        assert (await client.get("/v1/auth/me", headers=headers)).status_code == 401


async def test_catalogue_empty_ordering_version_missing_media_and_inactive_groups(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    owner = await create_user(database_session_factory)
    app = app_for(database_session_factory, owner.user_id)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        empty = (await client.get("/v1/customer/collection-catalogue")).json()
        assert empty["categories"] == [] and empty["artwork"]["hero"] is None
        await seed_catalogue(database_session_factory)
        response = await client.get("/v1/customer/collection-catalogue")
        data = response.json()
        assert [c["category_code"] for c in data["categories"]] == ["TEST_A", "TEST_B"]
        assert [c["category_code"] for c in data["quick_categories"]] == ["TEST_A", "TEST_B"]
        assert data["version"] != empty["version"]
        assert (await client.get("/v1/customer/collection-catalogue")).json()["version"] == data[
            "version"
        ]
        async with database_session_factory() as session, session.begin():
            await session.execute(
                update(CatalogueCategory)
                .where(CatalogueCategory.category_code == "TEST_A")
                .values(image_id=None)
            )
        result = (await client.get("/v1/customer/collection-catalogue")).json()
        assert [c["category_code"] for c in result["categories"]] == ["TEST_B"]
        assert result["version"] != data["version"]
        async with database_session_factory() as session, session.begin():
            await session.execute(update(CatalogueGroup).values(active=False))
        assert (await client.get("/v1/customer/collection-catalogue")).json()["categories"] == []


class OneDayPolicy(TestSlotAvailability):
    async def service_dates(
        self, session: AsyncSession, *, cell_id: str, now: datetime
    ) -> tuple[Any, ...]:
        return ((now.astimezone(SERVICE_TIMEZONE) + timedelta(days=1)).date(),)

    async def availability(
        self, session: AsyncSession, *, cell_id: str, slot: SlotWindow, now: datetime
    ) -> Literal["AVAILABLE", "FULL"] | None:
        return "FULL" if slot.start.astimezone(SERVICE_TIMEZONE).hour == 12 else "AVAILABLE"


async def test_slots_owned_context_policy_failure_and_available_full_grid(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    owner = await create_user(database_session_factory)
    context = await create_context(database_session_factory, owner.user_id)
    other = await create_user(database_session_factory)
    foreign = await create_context(database_session_factory, other.user_id)
    app = app_for(database_session_factory, owner.user_id, policy=UnconfiguredSlotAvailability())
    path = "/v1/customer/pickup-slots"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            path, params={"serviceability_context_id": str(context.serviceability_context_id)}
        )
        assert response.status_code == 503
        assert (
            await client.get(
                path, params={"serviceability_context_id": str(foreign.serviceability_context_id)}
            )
        ).status_code == 404
        app.state.slot_availability = OneDayPolicy()
        response = await client.get(
            path, params={"serviceability_context_id": str(context.serviceability_context_id)}
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert len(data["slots"]) == 32
        assert sum(slot["availability"] == "FULL" for slot in data["slots"]) == 2
        assert data["expires_at"] == context.expires_at.isoformat().replace("+00:00", "Z")
        async with database_session_factory() as session, session.begin():
            await session.execute(
                update(type(context))
                .where(type(context).serviceability_context_id == context.serviceability_context_id)
                .values(status="UNSERVICEABLE")
            )
        assert (
            await client.get(
                path, params={"serviceability_context_id": str(context.serviceability_context_id)}
            )
        ).json()["error"]["code"] == "SERVICEABILITY_UNSERVICEABLE"
        async with database_session_factory() as session, session.begin():
            await session.execute(
                update(type(context))
                .where(type(context).serviceability_context_id == context.serviceability_context_id)
                .values(expires_at=utc_now() - timedelta(seconds=1))
            )
        assert (
            await client.get(
                path, params={"serviceability_context_id": str(context.serviceability_context_id)}
            )
        ).json()["error"]["code"] == "SERVICEABILITY_EXPIRED"


async def test_customer_pages_snapshot_cursor_isolation_detail_and_missing_payment(
    database_session_factory: async_sessionmaker[AsyncSession],
    address_protector: Any,
) -> None:
    owner = await create_user(database_session_factory)
    other = await create_user(database_session_factory)
    old = await protected_request(database_session_factory, owner.user_id, address_protector)
    newer = await protected_request(database_session_factory, owner.user_id, address_protector)
    foreign = await protected_request(database_session_factory, other.user_id, address_protector)
    app = app_for(database_session_factory, owner.user_id, address_protector)
    path = "/v1/customer/collection-requests"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        page = (await client.get(path, params={"view": "active", "limit": 1})).json()
        assert [r["request_id"] for r in page["items"]] == [str(newer.request.request_id)]
        assert "Historical household" not in str(page)
        await protected_request(database_session_factory, owner.user_id, address_protector)
        second = (
            await client.get(
                path, params={"view": "active", "limit": 1, "cursor": page["next_cursor"]}
            )
        ).json()
        assert [r["request_id"] for r in second["items"]] == [str(old.request.request_id)]
        assert second["next_cursor"] is None
        assert (await client.get(path, params={"view": "history"})).json()["items"] == []
        assert (await client.get(path, params={"view": "active", "limit": 51})).status_code == 422
        assert (await client.get(path + f"/{foreign.request.request_id}")).status_code == 404
        detail = await client.get(path + f"/{old.request.request_id}")
        assert detail.status_code == 200, detail.text
        parsed = CollectionDetailDto.model_validate(detail.json())
        assert parsed.address.text == "Historical household"
        assert parsed.payment.status == "PENDING" and parsed.payment.retry_allowed
        assert all(m.state != "COMPLETE" for m in parsed.journey.milestones)
        assert parsed.quote.amount_minor == 500
        assert parsed.refunds == []
        assert "pickup_location" not in detail.json() and "cell_id" not in detail.json()


async def test_history_and_recommendations_count_distinct_customer_collections(
    database_session_factory: async_sessionmaker[AsyncSession],
    address_protector: Any,
) -> None:
    owner = await create_user(database_session_factory)
    other = await create_user(database_session_factory)
    app = app_for(database_session_factory, owner.user_id, address_protector)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        path = "/v1/customer/recommendations/collection-categories"
        assert (await client.get(path)).json() == {"recommendations": []}
        await seed_catalogue(database_session_factory)
        first = await protected_request(database_session_factory, owner.user_id, address_protector)
        second = await protected_request(database_session_factory, owner.user_id, address_protector)
        foreign = await protected_request(
            database_session_factory, other.user_id, address_protector
        )
        async with database_session_factory() as session, session.begin():
            for result in (first, second, foreign):
                await confirm_fixture_payment(session, result, utc_now())
            await session.execute(
                update(CollectionRequest)
                .where(
                    CollectionRequest.request_id.in_(
                        [
                            first.request.request_id,
                            second.request.request_id,
                            foreign.request.request_id,
                        ]
                    )
                )
                .values(status="COMPLETED", completed_at=utc_now())
            )
            await session.execute(
                update(CollectionRequestItem)
                .where(
                    CollectionRequestItem.request_id == second.request.request_id,
                    CollectionRequestItem.item_category_code == "TEST_B",
                )
                .values(item_category_code="TEST_A")
            )
        data = (await client.get(path)).json()["recommendations"]
        assert data == [
            {"category_code": "TEST_A", "rank": 1, "reason": "PREVIOUS_COLLECTION"},
            {"category_code": "TEST_B", "rank": 2, "reason": "PREVIOUS_COLLECTION"},
        ]
        history = (
            await client.get("/v1/customer/collection-requests", params={"view": "history"})
        ).json()
        assert len(history["items"]) == 2
        async with database_session_factory() as session, session.begin():
            await session.execute(
                update(CatalogueCategory)
                .where(CatalogueCategory.category_code == "TEST_A")
                .values(active=False)
            )
        assert (await client.get(path)).json()["recommendations"] == [
            {"category_code": "TEST_B", "rank": 1, "reason": "PREVIOUS_COLLECTION"}
        ]


async def test_booking_revalidation_full_and_freeze_concurrent_race_and_replay(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    owner = await create_user(database_session_factory)
    context = await create_context(database_session_factory, owner.user_id)
    slot = daily_grid((utc_now().astimezone(SERVICE_TIMEZONE) + timedelta(days=1)).date())[24]
    command = CreateCollectionRequestCommand(
        owner.user_id,
        new_uuid7(),
        context.serviceability_context_id,
        slot.start,
        slot.end,
        ITEMS,
        utc_now() + timedelta(minutes=30),
    )
    kwargs = {
        "idempotency_expires_at": utc_now() + timedelta(hours=1),
        "planning_lead_time_minutes": 4,
    }
    with pytest.raises(SlotConflictError):
        await create_collection_request(
            database_session_factory,
            command,
            FixedPricing(),
            slot_availability=OneDayPolicy(),
            **kwargs,
        )
    # Hold the exact freeze lock while a fresh booking reaches the boundary.
    reached = asyncio.Event()

    class NotifyingPolicy(TestSlotAvailability):
        async def service_dates(
            self, session: AsyncSession, *, cell_id: str, now: datetime
        ) -> tuple[Any, ...]:
            reached.set()
            return await super().service_dates(session, cell_id=cell_id, now=now)

    async with database_session_factory() as session, session.begin():
        await acquire_work_unit_advisory_lock(
            session, cell_id=context.cell_id, slot_start=slot.start, slot_end=slot.end
        )
        task = asyncio.create_task(
            create_collection_request(
                database_session_factory,
                command,
                FixedPricing(),
                slot_availability=NotifyingPolicy(),
                **kwargs,
            )
        )
        await asyncio.wait_for(reached.wait(), timeout=10)
        session.add(
            PlanningBatch(
                planning_batch_id=new_uuid7(),
                cell_id=context.cell_id,
                slot_start=slot.start,
                slot_end=slot.end,
                status="READY",
                max_attempts_snapshot=1,
                created_at=utc_now(),
            )
        )
    with pytest.raises(SlotConflictError) as error:
        await task
    assert error.value.code == "PLANNING_STARTED"
    async with database_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(CollectionRequest)) == 0
    good_context = await create_context(database_session_factory, owner.user_id)
    result = await create_request(
        database_session_factory, owner.user_id, good_context.serviceability_context_id
    )
    replay_command = CreateCollectionRequestCommand(
        owner.user_id,
        result.request.client_request_id,
        good_context.serviceability_context_id,
        result.request.slot_start,
        result.request.slot_end,
        ITEMS,
        utc_now() + timedelta(minutes=30),
    )
    replay = await create_collection_request(
        database_session_factory, replay_command, FixedPricing(), **kwargs
    )
    assert replay.request.request_id == result.request.request_id


async def test_cursor_expiration_owner_view_validation_and_missing_financial_record(
    database_session_factory: async_sessionmaker[AsyncSession],
    address_protector: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = await create_user(database_session_factory)
    other = await create_user(database_session_factory)
    first = await protected_request(database_session_factory, owner.user_id, address_protector)
    await protected_request(database_session_factory, owner.user_id, address_protector)
    app = app_for(database_session_factory, owner.user_id, address_protector, ttl=60)
    path = "/v1/customer/collection-requests"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        page = (await client.get(path, params={"view": "active", "limit": 1})).json()
        token = page["next_cursor"]
        assert token
        assert (
            await client.get(path, params={"view": "history", "cursor": token})
        ).status_code == 422
        app.dependency_overrides[get_current_customer_id] = lambda: other.user_id
        assert (
            await client.get(path, params={"view": "active", "cursor": token})
        ).status_code == 422
        app.dependency_overrides[get_current_customer_id] = lambda: owner.user_id
        future = utc_now() + timedelta(minutes=2)
        monkeypatch.setattr(customer_reads, "utc_now", lambda: future)
        expired = await client.get(path, params={"view": "active", "cursor": token})
        assert expired.status_code == 409 and expired.json() == {
            "error": {"code": "CURSOR_EXPIRED"}
        }
        async with database_session_factory() as session, session.begin():
            payment = await session.get(Payment, first.payment.payment_id)
            await session.delete(payment)
        response = await client.get(path + f"/{first.request.request_id}")
        assert response.status_code == 503 and "Historical household" not in response.text


async def test_detail_projects_real_recorded_handover_and_survives_master_edits(
    database_session_factory: async_sessionmaker[AsyncSession],
    address_protector: Any,
) -> None:
    ready = await ready_request(database_session_factory)
    completed = await complete_collection_request(database_session_factory, ready.request_id)
    async with database_session_factory() as session, session.begin():
        row = await session.get(CollectionRequest, ready.request_id)
        owner = row.customer_id
        row.pickup_address_snapshot_encrypted = await address_protector.protect("Original booking")
        session.add(
            CollectionRequestItem(
                request_item_id=new_uuid7(),
                request_id=row.request_id,
                item_category_code="HISTORICAL",
                declared_quantity=1,
                quoted_line_amount_minor=row.quoted_amount_minor,
                currency="INR",
                created_at=row.created_at,
            )
        )
        payment_id, attempt_id = new_uuid7(), new_uuid7()
        session.add(
            Payment(
                payment_id=payment_id,
                request_id=row.request_id,
                amount_minor=row.quoted_amount_minor,
                currency="INR",
                status="SUCCEEDED",
                created_at=row.created_at,
                succeeded_at=row.accepted_at,
                successful_attempt_id=attempt_id,
            )
        )
        session.add(
            PaymentAttempt(
                payment_attempt_id=attempt_id,
                payment_id=payment_id,
                provider="test",
                provider_payment_id=f"pay_{attempt_id.hex}",
                provider_idempotency_key=str(attempt_id),
                status="SUCCEEDED",
                created_at=row.created_at,
                completed_at=row.accepted_at,
            )
        )
    app = app_for(database_session_factory, owner, address_protector)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/v1/customer/collection-requests/{ready.request_id}")
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["completed_at"] == completed.completed_at.isoformat().replace("+00:00", "Z")
        assert result["journey"]["handover"]["state"] == "VALIDATED"
        assert all(
            m["state"] == "COMPLETE" and m["occurred_at"] is not None
            for m in result["journey"]["milestones"]
        )
        assert result["journey"]["receiving_point"] is None
        assert result["journey_status"] == "HANDOVER_VALIDATED"
        async with database_session_factory() as session, session.begin():
            await session.execute(
                update(ReceivingPoint).values(official_name="Master edited later")
            )
        refreshed = await client.get(f"/v1/customer/collection-requests/{ready.request_id}")
        assert refreshed.json() == result


async def test_booking_before_commit_failure_rolls_back_and_retry_converges(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    owner = await create_user(database_session_factory)
    context = await create_context(database_session_factory, owner.user_id)
    slot = daily_grid((utc_now().astimezone(SERVICE_TIMEZONE) + timedelta(days=1)).date())[0]
    cmd = CreateCollectionRequestCommand(
        owner.user_id,
        new_uuid7(),
        context.serviceability_context_id,
        slot.start,
        slot.end,
        ITEMS,
        utc_now() + timedelta(minutes=30),
    )

    class FailedPolicy(TestSlotAvailability):
        async def availability(
            self, session: AsyncSession, *, cell_id: str, slot: SlotWindow, now: datetime
        ) -> Literal["AVAILABLE", "FULL"] | None:
            raise RuntimeError("simulated pre-commit crash")

    args = {
        "idempotency_expires_at": utc_now() + timedelta(hours=1),
        "planning_lead_time_minutes": 4,
    }
    with pytest.raises(RuntimeError, match="pre-commit"):
        await create_collection_request(
            database_session_factory, cmd, FixedPricing(), slot_availability=FailedPolicy(), **args
        )
    results = await asyncio.gather(
        *[
            create_collection_request(
                database_session_factory,
                cmd,
                FixedPricing(),
                slot_availability=TestSlotAvailability(),
                **args,
            )
            for _ in range(2)
        ]
    )
    assert results[0].request.request_id == results[1].request.request_id
    async with database_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(CollectionRequest)) == 1


@pytest.mark.parametrize(
    "state,expected",
    [
        ("PENDING", "INITIATED"),
        ("SUCCEEDED", "COMPLETED"),
        ("INITIATION_UNCERTAIN", "CONFIRMING"),
        ("SUBMITTED", "PROCESSING"),
        ("FAILED", "FAILED"),
    ],
)
async def test_detail_financial_truth(
    database_session_factory: async_sessionmaker[AsyncSession],
    address_protector: Any,
    state: str,
    expected: str,
) -> None:
    owner = await create_user(database_session_factory)
    result = await protected_request(database_session_factory, owner.user_id, address_protector)
    now = utc_now()
    attempt_id = new_uuid7()
    async with database_session_factory() as session, session.begin():
        session.add(
            PaymentAttempt(
                payment_attempt_id=attempt_id,
                payment_id=result.payment.payment_id,
                provider="test",
                provider_idempotency_key=str(attempt_id),
                provider_payment_id=f"pay_{attempt_id.hex}",
                status="SUCCEEDED",
                created_at=now,
                completed_at=now,
            )
        )
        await session.flush()
        await session.execute(
            update(Payment)
            .where(Payment.payment_id == result.payment.payment_id)
            .values(status="SUCCEEDED", successful_attempt_id=attempt_id, succeeded_at=now)
        )
        await session.execute(
            update(CollectionRequest)
            .where(CollectionRequest.request_id == result.request.request_id)
            .values(status="CANCELLED", accepted_at=now, cancelled_at=now)
        )
        session.add(
            Refund(
                refund_id=new_uuid7(),
                payment_id=result.payment.payment_id,
                payment_attempt_id=attempt_id,
                provider="test",
                provider_idempotency_key=str(new_uuid7()),
                amount_minor=100,
                currency="INR",
                reason_code="CUSTOMER_CANCELLATION",
                status=state,
                created_at=now,
                completed_at=now if state == "SUCCEEDED" else None,
            )
        )
    app = app_for(database_session_factory, owner.user_id, address_protector)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/v1/customer/collection-requests/{result.request.request_id}")
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["payment"]["status"] == "SUCCEEDED"
        assert data["refunds"][0]["status"] == expected
        assert data["refund_status"] == expected
        # Failed status without authoritative non-payable proof still reserves money.
        assert data["cancellation"]["refund_expectation"] == "REVIEW_REQUIRED"


async def test_fresh_booking_policy_failure_inactive_category_and_immutable_display_snapshot(
    database_session_factory: async_sessionmaker[AsyncSession],
    address_protector: Any,
) -> None:
    owner = await create_user(database_session_factory)
    context = await create_context(database_session_factory, owner.user_id)
    slot = daily_grid((utc_now().astimezone(SERVICE_TIMEZONE) + timedelta(days=1)).date())[0]
    app = app_for(
        database_session_factory,
        owner.user_id,
        address_protector,
        policy=UnconfiguredSlotAvailability(),
    )
    body = {
        "client_request_id": str(new_uuid7()),
        "serviceability_context_id": str(context.serviceability_context_id),
        "slot_start": slot.start.isoformat(),
        "slot_end": slot.end.isoformat(),
        "items": [{"item_category_code": "TEST_A"}, {"item_category_code": "TEST_B"}],
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/v1/collection-requests", json=body)
        assert response.status_code == 503
        await seed_catalogue(database_session_factory)
        app.state.slot_availability = TestSlotAvailability()
        async with database_session_factory() as session, session.begin():
            await session.execute(
                update(CatalogueCategory)
                .where(CatalogueCategory.category_code == "TEST_A")
                .values(active=False)
            )
        assert (await client.post("/v1/collection-requests", json=body)).status_code == 409
        async with database_session_factory() as session, session.begin():
            await session.execute(update(CatalogueCategory).values(active=True))
            await session.execute(
                update(type(context))
                .where(type(context).serviceability_context_id == context.serviceability_context_id)
                .values(
                    address_snapshot_encrypted=await address_protector.protect("Booking snapshot")
                )
            )
        result = await client.post("/v1/collection-requests", json=body)
        assert result.status_code == 201, result.text
        request_id = result.json()["request_id"]
        async with database_session_factory() as session, session.begin():
            await session.execute(
                update(CatalogueCategory).values(display_name="Edited later", active=False)
            )
        detail = (await client.get(f"/v1/customer/collection-requests/{request_id}")).json()
        assert [i["display_name"] for i in detail["items"]] == ["Test TEST_A", "Test TEST_B"]
        assert detail["address"]["text"] == "Booking snapshot"
        assert (await client.post("/v1/collection-requests", json=body)).json()[
            "request_id"
        ] == request_id


async def test_preplanning_updated_time_uses_persisted_freeze_timestamp(
    database_session_factory: async_sessionmaker[AsyncSession],
    address_protector: Any,
) -> None:
    owner = await create_user(database_session_factory)
    result = await protected_request(database_session_factory, owner.user_id, address_protector)
    frozen_at = utc_now() + timedelta(seconds=1)
    batch_id = new_uuid7()
    async with database_session_factory() as session, session.begin():
        await confirm_fixture_payment(session, result, frozen_at - timedelta(seconds=1))
        session.add(
            PlanningBatch(
                planning_batch_id=batch_id,
                cell_id=result.request.cell_id,
                slot_start=result.request.slot_start,
                slot_end=result.request.slot_end,
                status="READY",
                max_attempts_snapshot=1,
                created_at=frozen_at,
            )
        )
        await session.flush()
        await session.execute(
            update(CollectionRequest)
            .where(CollectionRequest.request_id == result.request.request_id)
            .values(status="PRE_PLANNING", planning_batch_id=batch_id)
        )
    app = app_for(database_session_factory, owner.user_id, address_protector)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        detail = await client.get(f"/v1/customer/collection-requests/{result.request.request_id}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["updated_at"] == frozen_at.isoformat().replace("+00:00", "Z")
        assert detail.json()["cancellation"]["reason"] == "PLANNING_STARTED"
