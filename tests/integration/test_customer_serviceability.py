from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.api.dependencies import get_current_customer_id
from tirodhan.core.config import Settings
from tirodhan.db.values import utc_now
from tirodhan.main import create_app
from tirodhan.modules.customers.models import UserAddress
from tirodhan.modules.customers.ports import AddressProtector
from tirodhan.modules.customers.service import (
    ADDRESS_ARCHIVED,
    AddressVersionConflictError,
    CreateAddressCommand,
    GeoPoint,
    UpdateAddressCommand,
    archive_address,
    create_address,
    list_active_addresses,
    update_address,
)
from tirodhan.modules.identity.models import AppUser
from tirodhan.modules.reliability.models import OutboxEvent
from tirodhan.modules.serviceability.models import ServiceabilityContext
from tirodhan.modules.serviceability.ports import (
    CellIdDeriver,
    LocationResolution,
    LocationResolutionStatus,
    LocationResolver,
)
from tirodhan.modules.serviceability.service import (
    SERVICEABILITY_SERVICEABLE,
    SERVICEABILITY_TECHNICAL_FAILURE,
    SERVICEABILITY_UNSERVICEABLE,
    CreateServiceabilityContextCommand,
    create_serviceability_context,
    resolve_serviceability,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def create_user(factory: async_sessionmaker[AsyncSession]) -> AppUser:
    async with factory() as session, session.begin():
        user = AppUser(status="ACTIVE")
        session.add(user)
        await session.flush()
        return user


def future() -> Any:
    return utc_now() + timedelta(hours=1)


async def saved_address(
    factory: async_sessionmaker[AsyncSession],
    protector: AddressProtector,
    user: AppUser,
    *,
    key: str,
    address_text: str = "Protected household address",
    is_default: bool = False,
) -> UserAddress:
    async with factory() as session, session.begin():
        return await create_address(
            session,
            CreateAddressCommand(
                user_id=user.user_id,
                idempotency_key=key,
                address=address_text,
                label="Home",
                location=GeoPoint(latitude=28.6139, longitude=77.2090),
                is_default=is_default,
            ),
            protector,
            idempotency_expires_at=future(),
        )


@pytest.mark.asyncio
async def test_address_create_replay_list_update_archive_and_postgis(
    database_session_factory: async_sessionmaker[AsyncSession],
    address_protector: AddressProtector,
) -> None:
    user = await create_user(database_session_factory)
    address = await saved_address(
        database_session_factory, address_protector, user, key="address-create-1"
    )
    replay = await saved_address(
        database_session_factory, address_protector, user, key="address-create-1"
    )
    assert replay.address_id == address.address_id

    async with database_session_factory() as session, session.begin():
        listed = await list_active_addresses(session, user.user_id)
        updated = await update_address(
            session,
            UpdateAddressCommand(
                user_id=user.user_id,
                address_id=address.address_id,
                idempotency_key="address-update-1",
                expected_version=1,
                address="Updated protected household address",
                label="Updated Home",
                location=GeoPoint(latitude=28.6140, longitude=77.2091),
                is_default=False,
            ),
            address_protector,
            idempotency_expires_at=future(),
        )
        updated_replay = await update_address(
            session,
            UpdateAddressCommand(
                user_id=user.user_id,
                address_id=address.address_id,
                idempotency_key="address-update-1",
                expected_version=1,
                address="Updated protected household address",
                label="Updated Home",
                location=GeoPoint(latitude=28.6140, longitude=77.2091),
                is_default=False,
            ),
            address_protector,
            idempotency_expires_at=future(),
        )

    assert [item.address_id for item in listed] == [address.address_id]
    assert updated.version == 2
    assert updated_replay.address_id == updated.address_id

    async with database_session_factory() as session:
        longitude, latitude = (
            await session.execute(
                text(
                    "SELECT ST_X(location::geometry), ST_Y(location::geometry) "
                    "FROM user_address WHERE address_id = :address_id"
                ),
                {"address_id": address.address_id},
            )
        ).one()
    assert longitude == pytest.approx(77.2091)
    assert latitude == pytest.approx(28.6140)

    with pytest.raises(AddressVersionConflictError):
        async with database_session_factory() as session, session.begin():
            await update_address(
                session,
                UpdateAddressCommand(
                    user_id=user.user_id,
                    address_id=address.address_id,
                    idempotency_key="address-update-stale",
                    expected_version=1,
                    address="Stale edit",
                ),
                address_protector,
                idempotency_expires_at=future(),
            )

    async with database_session_factory() as session, session.begin():
        archived = await archive_address(
            session,
            user_id=user.user_id,
            address_id=address.address_id,
            idempotency_key="address-archive-1",
            idempotency_expires_at=future(),
        )
    async with database_session_factory() as session, session.begin():
        archived_replay = await archive_address(
            session,
            user_id=user.user_id,
            address_id=address.address_id,
            idempotency_key="address-archive-1",
            idempotency_expires_at=future(),
        )
        active = await list_active_addresses(session, user.user_id)
    assert archived.status == ADDRESS_ARCHIVED
    assert archived_replay.status == ADDRESS_ARCHIVED
    assert active == []


@pytest.mark.asyncio
async def test_database_allows_only_one_active_default_address(
    database_session_factory: async_sessionmaker[AsyncSession],
    address_protector: AddressProtector,
) -> None:
    user = await create_user(database_session_factory)
    await saved_address(
        database_session_factory,
        address_protector,
        user,
        key="default-address-1",
        is_default=True,
    )

    with pytest.raises(IntegrityError):
        await saved_address(
            database_session_factory,
            address_protector,
            user,
            key="default-address-2",
            address_text="Another protected address",
            is_default=True,
        )


@pytest.mark.asyncio
async def test_saved_address_context_is_immutable_and_outbox_is_minimal(
    database_session_factory: async_sessionmaker[AsyncSession],
    address_protector: AddressProtector,
    caplog: pytest.LogCaptureFixture,
) -> None:
    user = await create_user(database_session_factory)
    address = await saved_address(
        database_session_factory, address_protector, user, key="snapshot-address"
    )
    command = CreateServiceabilityContextCommand(
        user_id=user.user_id,
        idempotency_key="context-saved-1",
        source_address_id=address.address_id,
        expires_at=future(),
    )
    caplog.set_level(logging.INFO)
    async with database_session_factory() as session, session.begin():
        context = await create_serviceability_context(
            session,
            command,
            address_protector,
            idempotency_expires_at=future(),
        )
    async with database_session_factory() as session, session.begin():
        replay = await create_serviceability_context(
            session,
            command,
            address_protector,
            idempotency_expires_at=future(),
        )
        event = await session.scalar(
            select(OutboxEvent).where(OutboxEvent.aggregate_id == context.serviceability_context_id)
        )
        event_count = await session.scalar(
            select(func.count(OutboxEvent.outbox_event_id)).where(
                OutboxEvent.aggregate_id == context.serviceability_context_id
            )
        )
    assert replay.serviceability_context_id == context.serviceability_context_id
    assert context.source_address_version == 1
    assert event is not None
    assert event.payload == {"serviceability_context_id": str(context.serviceability_context_id)}
    assert event_count == 1
    serialized = json.dumps(event.payload).lower()
    assert not any(
        term in serialized
        for term in ("address", "phone", "latitude", "longitude", "coordinate", "token")
    )
    assert "Protected household address" not in caplog.text

    snapshot = bytes(context.address_snapshot_encrypted)
    async with database_session_factory() as session, session.begin():
        await update_address(
            session,
            UpdateAddressCommand(
                user_id=user.user_id,
                address_id=address.address_id,
                idempotency_key="snapshot-address-edit",
                expected_version=1,
                address="A later address value",
            ),
            address_protector,
            idempotency_expires_at=future(),
        )
    async with database_session_factory() as session:
        persisted = await session.get(ServiceabilityContext, context.serviceability_context_id)
    assert persisted is not None
    assert bytes(persisted.address_snapshot_encrypted) == snapshot
    assert persisted.source_address_version == 1


@pytest.mark.asyncio
async def test_saved_address_context_prefers_client_location_without_mutating_address(
    database_session_factory: async_sessionmaker[AsyncSession],
    address_protector: AddressProtector,
) -> None:
    user = await create_user(database_session_factory)
    address = await saved_address(
        database_session_factory,
        address_protector,
        user,
        key="saved-address-client-location",
    )
    supplied_location = GeoPoint(latitude=28.7041, longitude=77.1025)

    async with database_session_factory() as session, session.begin():
        context = await create_serviceability_context(
            session,
            CreateServiceabilityContextCommand(
                user_id=user.user_id,
                idempotency_key="saved-context-client-location",
                source_address_id=address.address_id,
                location=supplied_location,
                expires_at=future(),
            ),
            address_protector,
            idempotency_expires_at=future(),
        )

    async with database_session_factory() as session:
        context_longitude, context_latitude = (
            await session.execute(
                text(
                    "SELECT ST_X(location::geometry), ST_Y(location::geometry) "
                    "FROM serviceability_context "
                    "WHERE serviceability_context_id = :context_id"
                ),
                {"context_id": context.serviceability_context_id},
            )
        ).one()
        address_longitude, address_latitude = (
            await session.execute(
                text(
                    "SELECT ST_X(location::geometry), ST_Y(location::geometry) "
                    "FROM user_address WHERE address_id = :address_id"
                ),
                {"address_id": address.address_id},
            )
        ).one()

    assert context_longitude == pytest.approx(supplied_location.longitude)
    assert context_latitude == pytest.approx(supplied_location.latitude)
    assert address_longitude == pytest.approx(77.2090)
    assert address_latitude == pytest.approx(28.6139)


class StaticResolver(LocationResolver):
    def __init__(self, result: LocationResolution) -> None:
        self.result = result
        self.supplied_locations: list[GeoPoint | None] = []

    async def resolve(
        self, *, address: str, supplied_location: GeoPoint | None
    ) -> LocationResolution:
        self.supplied_locations.append(supplied_location)
        return self.result


class StaticCellDeriver(CellIdDeriver):
    async def derive(self, location: GeoPoint) -> str:
        return "test-cell"


@pytest.mark.asyncio
async def test_one_off_client_location_and_distinct_resolution_outcomes(
    database_session_factory: async_sessionmaker[AsyncSession],
    address_protector: AddressProtector,
) -> None:
    user = await create_user(database_session_factory)
    cases = [
        (
            LocationResolution(
                LocationResolutionStatus.RESOLVED,
                GeoPoint(latitude=28.6, longitude=77.2),
            ),
            SERVICEABILITY_SERVICEABLE,
        ),
        (
            LocationResolution(
                LocationResolutionStatus.UNSERVICEABLE,
                GeoPoint(latitude=29.0, longitude=78.0),
                "OUTSIDE_AREA",
            ),
            SERVICEABILITY_UNSERVICEABLE,
        ),
        (
            LocationResolution(
                LocationResolutionStatus.TECHNICAL_FAILURE,
                failure_code="GEOCODER_UNAVAILABLE",
            ),
            SERVICEABILITY_TECHNICAL_FAILURE,
        ),
    ]
    for index, (resolution, expected_status) in enumerate(cases):
        async with database_session_factory() as session, session.begin():
            context = await create_serviceability_context(
                session,
                CreateServiceabilityContextCommand(
                    user_id=user.user_id,
                    idempotency_key=f"one-off-{index}",
                    one_off_address="One-off protected address",
                    location=GeoPoint(latitude=28.6, longitude=77.2),
                    expires_at=future(),
                ),
                address_protector,
                idempotency_expires_at=future(),
            )
        resolver = StaticResolver(resolution)
        resolved = await resolve_serviceability(
            database_session_factory,
            context_id=context.serviceability_context_id,
            protector=address_protector,
            location_resolver=resolver,
            cell_id_deriver=StaticCellDeriver(),
        )
        replay = await resolve_serviceability(
            database_session_factory,
            context_id=context.serviceability_context_id,
            protector=address_protector,
            location_resolver=resolver,
            cell_id_deriver=StaticCellDeriver(),
        )
        assert resolved.status == expected_status
        assert replay.status == expected_status
        assert len(resolver.supplied_locations) == 1
        assert resolver.supplied_locations[0] == GeoPoint(latitude=28.6, longitude=77.2)


class BarrierResolver(LocationResolver):
    def __init__(self) -> None:
        self._arrivals = 0
        self._lock = asyncio.Lock()
        self._ready = asyncio.Event()

    async def resolve(
        self, *, address: str, supplied_location: GeoPoint | None
    ) -> LocationResolution:
        async with self._lock:
            self._arrivals += 1
            arrival = self._arrivals
            if self._arrivals == 2:
                self._ready.set()
        await self._ready.wait()
        if arrival == 1:
            return LocationResolution(
                LocationResolutionStatus.RESOLVED,
                GeoPoint(latitude=28.6, longitude=77.2),
            )
        return LocationResolution(
            LocationResolutionStatus.UNSERVICEABLE,
            failure_code="OUTSIDE_AREA",
        )


@pytest.mark.asyncio
async def test_concurrent_resolution_has_one_authoritative_terminal_result(
    database_session_factory: async_sessionmaker[AsyncSession],
    address_protector: AddressProtector,
) -> None:
    user = await create_user(database_session_factory)
    async with database_session_factory() as session, session.begin():
        context = await create_serviceability_context(
            session,
            CreateServiceabilityContextCommand(
                user_id=user.user_id,
                idempotency_key="concurrent-context",
                one_off_address="Concurrent protected address",
                expires_at=future(),
            ),
            address_protector,
            idempotency_expires_at=future(),
        )

    resolver = BarrierResolver()

    async def resolve_once() -> str:
        result = await resolve_serviceability(
            database_session_factory,
            context_id=context.serviceability_context_id,
            protector=address_protector,
            location_resolver=resolver,
            cell_id_deriver=StaticCellDeriver(),
        )
        return result.status

    statuses = await asyncio.gather(resolve_once(), resolve_once())
    assert statuses[0] == statuses[1]
    assert statuses[0] in {SERVICEABILITY_SERVICEABLE, SERVICEABILITY_UNSERVICEABLE}


class ExpectedRollback(Exception):
    pass


@pytest.mark.asyncio
async def test_context_and_outbox_roll_back_together(
    database_session_factory: async_sessionmaker[AsyncSession],
    address_protector: AddressProtector,
) -> None:
    user = await create_user(database_session_factory)
    context_id = None
    with pytest.raises(ExpectedRollback):
        async with database_session_factory() as session, session.begin():
            context = await create_serviceability_context(
                session,
                CreateServiceabilityContextCommand(
                    user_id=user.user_id,
                    idempotency_key="rollback-context",
                    one_off_address="Rollback protected address",
                    expires_at=future(),
                ),
                address_protector,
                idempotency_expires_at=future(),
            )
            context_id = context.serviceability_context_id
            raise ExpectedRollback

    async with database_session_factory() as session:
        context_count = await session.scalar(
            select(func.count(ServiceabilityContext.serviceability_context_id)).where(
                ServiceabilityContext.serviceability_context_id == context_id
            )
        )
        event_count = await session.scalar(
            select(func.count(OutboxEvent.outbox_event_id)).where(
                OutboxEvent.aggregate_id == context_id
            )
        )
    assert context_count == 0
    assert event_count == 0


class ExplodingResolver(LocationResolver):
    async def resolve(
        self, *, address: str, supplied_location: GeoPoint | None
    ) -> LocationResolution:
        raise AssertionError("GET must not trigger serviceability resolution")


@pytest.mark.asyncio
async def test_primary_api_flow_and_status_get_is_read_only(
    migrated_database_url: str,
    database_session_factory: async_sessionmaker[AsyncSession],
    address_protector: AddressProtector,
) -> None:
    user = await create_user(database_session_factory)
    settings = Settings(
        _env_file=None,
        environment="test",
        database_url=migrated_database_url,
        command_idempotency_ttl_seconds=3600,
        serviceability_context_ttl_seconds=900,
    )
    application = create_app(
        settings,
        address_protector=address_protector,
        location_resolver=ExplodingResolver(),
        cell_id_deriver=StaticCellDeriver(),
    )
    application.dependency_overrides[get_current_customer_id] = lambda: user.user_id

    async with application.router.lifespan_context(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://test"
        ) as client:
            created = await client.post(
                "/v1/addresses",
                headers={"Idempotency-Key": "api-address-create"},
                json={
                    "address": "API protected address",
                    "label": "Home",
                    "location": {"latitude": 28.61, "longitude": 77.20},
                    "is_default": True,
                },
            )
            assert created.status_code == 201
            address_id = created.json()["address_id"]

            listed = await client.get("/v1/addresses")
            assert listed.status_code == 200
            assert [item["address_id"] for item in listed.json()] == [address_id]

            updated = await client.put(
                f"/v1/addresses/{address_id}",
                headers={"Idempotency-Key": "api-address-update"},
                json={
                    "address": "API updated protected address",
                    "label": "Updated",
                    "expected_version": 1,
                },
            )
            assert updated.status_code == 200
            assert updated.json()["version"] == 2

            context_created = await client.post(
                "/v1/serviceability/contexts",
                headers={"Idempotency-Key": "api-context-create"},
                json={
                    "source_address_id": address_id,
                    "location": {"latitude": 28.62, "longitude": 77.21},
                },
            )
            assert context_created.status_code == 201
            context_id = context_created.json()["serviceability_context_id"]

            status_response = await client.get(f"/v1/serviceability/contexts/{context_id}")
            assert status_response.status_code == 200
            assert status_response.json()["status"] == "PENDING"

            archived = await client.post(
                f"/v1/addresses/{address_id}/archive",
                headers={"Idempotency-Key": "api-address-archive"},
            )
            assert archived.status_code == 200
            assert archived.json()["status"] == "ARCHIVED"
