from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from functools import lru_cache
from typing import Any
from uuid import UUID

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from geoalchemy2.elements import WKTElement
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_rider_dispatch import create_fixture, create_offer

from tirodhan.core.config import Settings
from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.main import create_app
from tirodhan.modules.collection_requests.models import (
    CollectionRequest,
    CollectionRequestItem,
)
from tirodhan.modules.customers.ports import AddressProtector
from tirodhan.modules.customers.service import GeoPoint
from tirodhan.modules.dispatch import service as dispatch_service
from tirodhan.modules.dispatch.models import (
    AssignmentOffer,
    RiderAssignment,
)
from tirodhan.modules.dispatch.service import (
    RiderNotEligibleError,
    accept_assignment_offer,
    assign_group_manually,
    create_assignment_offer,
)
from tirodhan.modules.evidence.media_policy import ConfiguredMediaPolicy
from tirodhan.modules.evidence.media_ports import StoredObjectProperties, UploadAuthorization
from tirodhan.modules.evidence.models import MediaAsset
from tirodhan.modules.evidence.service import record_evidence_capture
from tirodhan.modules.handovers.service import record_handover
from tirodhan.modules.identity.models import AppUser, RefreshSession, UserRole
from tirodhan.modules.identity.service import hash_refresh_credential
from tirodhan.modules.identity.tokens import Rs256AccessTokenCodec
from tirodhan.modules.operations import service as operations_service
from tirodhan.modules.operations.service import (
    ReassignmentRiderError,
    open_pickup_incident,
    reassign_outstanding_work,
)
from tirodhan.modules.pickups.models import PickupIncident
from tirodhan.modules.pickups.service import record_pickup_attempt, start_assignment
from tirodhan.modules.planning.models import PickupExecution
from tirodhan.modules.receiving_points.models import ReceivingPoint
from tirodhan.modules.reliability.models import OutboxEvent

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


class DeterministicMediaStorage:
    def __init__(self) -> None:
        self.properties: dict[str, StoredObjectProperties | None] = {}
        self.authorization = "opaque-secret-upload-authorization"

    async def create_upload_authorization(
        self, *, object_key: str, expected_content_type: str
    ) -> UploadAuthorization:
        return UploadAuthorization(
            opaque_value=self.authorization,
            expires_at=utc_now() + timedelta(minutes=5),
        )

    async def inspect_object(self, *, object_key: str) -> StoredObjectProperties | None:
        return self.properties.get(object_key)


@lru_cache
def _token_codec() -> Rs256AccessTokenCodec:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return Rs256AccessTokenCodec(
        private_key_pem=private_pem,
        public_key_pem=public_pem,
        issuer="https://identity.test",
        audience="tirodhan-test",
    )


async def _token_for(
    factory: async_sessionmaker[AsyncSession],
    user_id: UUID,
    *,
    roles: tuple[str, ...] = (),
) -> tuple[str, UUID]:
    now = utc_now().replace(microsecond=0)
    session_id = new_uuid7()
    async with factory() as session, session.begin():
        for role_code in roles:
            existing = await session.scalar(
                select(UserRole).where(
                    UserRole.user_id == user_id,
                    UserRole.role_code == role_code,
                    UserRole.revoked_at.is_(None),
                )
            )
            if existing is None:
                session.add(
                    UserRole(
                        user_role_id=new_uuid7(),
                        user_id=user_id,
                        role_code=role_code,
                        granted_at=now,
                        granted_by_user_id=None,
                        revoked_at=None,
                        revoked_by_user_id=None,
                    )
                )
        session.add(
            RefreshSession(
                refresh_session_id=session_id,
                user_id=user_id,
                credential_hash=hash_refresh_credential(str(new_uuid7())),
                created_at=now,
                expires_at=now + timedelta(hours=1),
                revoked_at=None,
            )
        )
    token = _token_codec().issue(
        user_id=user_id,
        refresh_session_id=session_id,
        issued_at=now - timedelta(seconds=1),
        ttl_seconds=3600,
    )
    return token, session_id


async def _new_user(factory: async_sessionmaker[AsyncSession], *, status: str = "ACTIVE") -> UUID:
    user = AppUser(status=status)
    async with factory() as session, session.begin():
        session.add(user)
        await session.flush([user])
    return user.user_id


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _application(
    database_url: str,
    *,
    address_protector: AddressProtector | None = None,
    media_storage: DeterministicMediaStorage | None = None,
    media_policy: ConfiguredMediaPolicy | None = None,
) -> Any:
    return create_app(
        Settings(
            _env_file=None,
            environment="test",
            database_url=database_url,
            command_idempotency_ttl_seconds=3600,
        ),
        access_token_codec=_token_codec(),
        address_protector=address_protector,
        media_storage=media_storage,
        media_policy=media_policy,
    )


async def _prepare_operational_snapshots(
    factory: async_sessionmaker[AsyncSession],
    pickup_ids: tuple[tuple[UUID, ...], ...],
    protector: AddressProtector,
) -> dict[UUID, UUID]:
    protected = await protector.protect("221 Immutable Booking Street, Delhi")
    request_by_pickup: dict[UUID, UUID] = {}
    async with factory() as session, session.begin():
        for pickup_id in (value for group in pickup_ids for value in group):
            pickup = await session.get(PickupExecution, pickup_id)
            assert pickup is not None
            request = await session.get(CollectionRequest, pickup.request_id)
            assert request is not None
            request.pickup_address_snapshot_encrypted = protected
            request.pickup_location = WKTElement("POINT(77.2090 28.6139)", srid=4326)
            session.add(
                CollectionRequestItem(
                    request_item_id=new_uuid7(),
                    request_id=request.request_id,
                    item_category_code="PAPER",
                    declared_quantity=2,
                    declared_weight_grams=750,
                    quoted_line_amount_minor=100,
                    currency="INR",
                    pricing_rule_version="test-v1",
                    created_at=utc_now(),
                )
            )
            request_by_pickup[pickup_id] = request.request_id
    return request_by_pickup


async def _seed_receiving_points(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[ReceivingPoint, ReceivingPoint]:
    now = utc_now()
    active = ReceivingPoint(
        receiving_point_id=new_uuid7(),
        official_name="Alpha Receiving Centre",
        official_identifier="RPC-A",
        location=WKTElement("POINT(77.2090 28.6139)", srid=4326),
        allowed_radius_m=100,
        status="ACTIVE",
        version=1,
        created_at=now,
        updated_at=now,
    )
    inactive = ReceivingPoint(
        receiving_point_id=new_uuid7(),
        official_name="Zeta Closed Centre",
        official_identifier="RPC-Z",
        location=WKTElement("POINT(77.2090 28.6139)", srid=4326),
        allowed_radius_m=100,
        status="INACTIVE",
        version=1,
        created_at=now,
        updated_at=now,
    )
    async with factory() as session, session.begin():
        session.add_all([active, inactive])
    return active, inactive


async def test_operational_namespaces_use_live_roles_user_and_session_state(
    database_session_factory: async_sessionmaker[AsyncSession],
    migrated_database_url: str,
    address_protector: AddressProtector,
) -> None:
    fixture = await create_fixture(database_session_factory, rider_count=2)
    customer_id = await _new_user(database_session_factory)
    rider_token, rider_session = await _token_for(
        database_session_factory,
        fixture.rider_ids[0],
        roles=("CUSTOMER", "MANAGER"),
    )
    rider_only_token, _ = await _token_for(database_session_factory, fixture.rider_ids[1])
    manager_token, _ = await _token_for(
        database_session_factory, fixture.manager_id, roles=("MANAGER",)
    )
    customer_token, _ = await _token_for(database_session_factory, customer_id, roles=("CUSTOMER",))
    application = _application(migrated_database_url, address_protector=address_protector)

    async with application.router.lifespan_context(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://test"
        ) as client:
            assert (await client.get("/v1/rider/me/availability")).status_code == 401
            assert (
                await client.get("/v1/rider/me/availability", headers=_headers(customer_token))
            ).status_code == 403
            assert (
                await client.get("/v1/rider/me/availability", headers=_headers(manager_token))
            ).status_code == 403
            assert (
                await client.get("/v1/rider/me/availability", headers=_headers(rider_token))
            ).status_code == 200
            assert (
                await client.get("/v1/manager/riders", headers=_headers(rider_only_token))
            ).status_code == 403
            assert (
                await client.get("/v1/manager/riders", headers=_headers(customer_token))
            ).status_code == 403
            assert (
                await client.get("/v1/manager/riders", headers=_headers(manager_token))
            ).status_code == 200
            assert (
                await client.get("/v1/manager/riders", headers=_headers(rider_token))
            ).status_code == 200

            async with database_session_factory() as session, session.begin():
                await session.execute(
                    update(UserRole)
                    .where(
                        UserRole.user_id == fixture.rider_ids[0],
                        UserRole.role_code == "MANAGER",
                        UserRole.revoked_at.is_(None),
                    )
                    .values(revoked_at=utc_now())
                )
            assert (
                await client.get("/v1/manager/riders", headers=_headers(rider_token))
            ).status_code == 403
            assert (
                await client.get("/v1/rider/me/availability", headers=_headers(rider_token))
            ).status_code == 200

            async with database_session_factory() as session, session.begin():
                await session.execute(
                    update(UserRole)
                    .where(
                        UserRole.user_id == fixture.rider_ids[0],
                        UserRole.role_code == "RIDER",
                        UserRole.revoked_at.is_(None),
                    )
                    .values(revoked_at=utc_now())
                )
            assert (
                await client.get("/v1/rider/me/availability", headers=_headers(rider_token))
            ).status_code == 403

            async with database_session_factory() as session, session.begin():
                await session.execute(
                    update(RefreshSession)
                    .where(RefreshSession.refresh_session_id == rider_session)
                    .values(revoked_at=utc_now())
                )
            assert (
                await client.get("/v1/rider/me/availability", headers=_headers(rider_token))
            ).status_code == 401

            async with database_session_factory() as session, session.begin():
                await session.execute(
                    update(AppUser)
                    .where(AppUser.user_id == fixture.manager_id)
                    .values(status="DISABLED")
                )
            assert (
                await client.get("/v1/manager/riders", headers=_headers(manager_token))
            ).status_code == 401


async def test_rider_availability_offer_and_assignment_read_model(
    database_session_factory: async_sessionmaker[AsyncSession],
    migrated_database_url: str,
    address_protector: AddressProtector,
) -> None:
    fixture = await create_fixture(
        database_session_factory, group_count=4, rider_count=2, pickups_per_group=1
    )
    await _prepare_operational_snapshots(
        database_session_factory, fixture.pickup_ids, address_protector
    )
    rider_token, _ = await _token_for(database_session_factory, fixture.rider_ids[0])
    other_token, _ = await _token_for(database_session_factory, fixture.rider_ids[1])
    current_offer = await create_offer(database_session_factory, fixture, group_index=0)
    await create_offer(database_session_factory, fixture, group_index=1, rider_index=1)
    expired = await create_offer(
        database_session_factory,
        fixture,
        group_index=2,
        now=utc_now() - timedelta(minutes=10),
    )
    closed = await create_offer(database_session_factory, fixture, group_index=3)
    async with database_session_factory() as session, session.begin():
        await session.execute(
            update(AssignmentOffer)
            .where(AssignmentOffer.offer_id == closed.offer_id)
            .values(status="CLOSED_LOST", responded_at=utc_now())
        )
    application = _application(migrated_database_url, address_protector=address_protector)

    async with application.router.lifespan_context(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://test"
        ) as client:
            initial = await client.get("/v1/rider/me/availability", headers=_headers(rider_token))
            assert initial.json()["work_state"] == "IDLE"
            assert (
                await client.put(
                    "/v1/rider/me/availability",
                    headers=_headers(rider_token),
                    json={"intent": "OFFLINE", "expected_version": 1, "work_state": "BUSY"},
                )
            ).status_code == 422
            offline = await client.put(
                "/v1/rider/me/availability",
                headers=_headers(rider_token),
                json={"intent": "OFFLINE", "expected_version": 1},
            )
            assert offline.json()["version"] == 2
            assert offline.json()["work_state"] == "IDLE"
            assert (
                await client.put(
                    "/v1/rider/me/availability",
                    headers=_headers(rider_token),
                    json={"intent": "AVAILABLE", "expected_version": 1},
                )
            ).status_code == 409
            available = await client.put(
                "/v1/rider/me/availability",
                headers=_headers(rider_token),
                json={"intent": "AVAILABLE", "expected_version": 2},
            )
            assert available.json()["version"] == 3

            offers = (await client.get("/v1/rider/me/offers", headers=_headers(rider_token))).json()
            assert [value["offer_id"] for value in offers] == [str(current_offer.offer_id)]
            assert str(expired.offer_id) not in {value["offer_id"] for value in offers}
            assert (
                await client.post(
                    f"/v1/rider/offers/{expired.offer_id}/accept",
                    headers=_headers(rider_token),
                )
            ).status_code == 409
            assert (
                await client.post(
                    f"/v1/rider/offers/{current_offer.offer_id}/accept",
                    headers=_headers(other_token),
                )
            ).status_code == 404
            accepted = await client.post(
                f"/v1/rider/offers/{current_offer.offer_id}/accept",
                headers=_headers(rider_token),
            )
            replay = await client.post(
                f"/v1/rider/offers/{current_offer.offer_id}/accept",
                headers=_headers(rider_token),
            )
            assert accepted.status_code == replay.status_code == 200
            assert accepted.json()["assignment_id"] == replay.json()["assignment_id"]

            view = await client.get("/v1/rider/me/assignment", headers=_headers(rider_token))
            body = view.json()["assignment"]
            assert body["assignment_id"] == accepted.json()["assignment_id"]
            assert body["pickups"][0]["pickup_address"] == ("221 Immutable Booking Street, Delhi")
            assert body["pickups"][0]["pickup_latitude"] == pytest.approx(28.6139)
            assert body["pickups"][0]["declared_items"] == [
                {
                    "item_category_code": "PAPER",
                    "declared_quantity": 2,
                    "declared_weight_grams": 750,
                }
            ]
            serialized = str(body)
            assert "customer_id" not in serialized
            assert "payment" not in serialized
            assert "quoted" not in serialized
            assert (
                await client.get("/v1/rider/me/assignment", headers=_headers(other_token))
            ).json() == {"assignment": None}
            assignment_id = accepted.json()["assignment_id"]
            assert (
                await client.post(
                    f"/v1/rider/assignments/{assignment_id}/start",
                    headers=_headers(other_token),
                )
            ).status_code == 404
            assert (
                await client.post(
                    f"/v1/rider/assignments/{assignment_id}/start",
                    headers=_headers(rider_token),
                )
            ).status_code == 200

    missing_protector_app = _application(migrated_database_url)
    async with missing_protector_app.router.lifespan_context(missing_protector_app):
        async with AsyncClient(
            transport=ASGITransport(app=missing_protector_app), base_url="http://test"
        ) as client:
            assert (
                await client.get("/v1/rider/me/assignment", headers=_headers(rider_token))
            ).status_code == 503


async def test_pickup_attempt_and_incident_http_idempotency_and_ownership(
    database_session_factory: async_sessionmaker[AsyncSession],
    migrated_database_url: str,
    address_protector: AddressProtector,
) -> None:
    fixture = await create_fixture(database_session_factory, rider_count=2, pickups_per_group=2)
    rider_token, _ = await _token_for(database_session_factory, fixture.rider_ids[0])
    other_token, _ = await _token_for(database_session_factory, fixture.rider_ids[1])
    assignment = await assign_group_manually(
        database_session_factory,
        collection_group_id=fixture.group_ids[0],
        rider_id=fixture.rider_ids[0],
        manager_user_id=fixture.manager_id,
    )
    application = _application(migrated_database_url, address_protector=address_protector)
    attempt_id = new_uuid7()
    incident_id = new_uuid7()

    async with application.router.lifespan_context(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://test"
        ) as client:
            assert (
                await client.post(
                    f"/v1/rider/assignments/{assignment.assignment_id}/start",
                    headers=_headers(rider_token),
                )
            ).status_code == 200
            payload = {"client_attempt_id": str(attempt_id), "outcome": "NOT_COLLECTED"}
            first = await client.post(
                f"/v1/rider/pickups/{fixture.pickup_ids[0][0]}/attempts",
                headers=_headers(rider_token),
                json=payload,
            )
            replay = await client.post(
                f"/v1/rider/pickups/{fixture.pickup_ids[0][0]}/attempts",
                headers=_headers(rider_token),
                json=payload,
            )
            assert first.status_code == replay.status_code == 200
            assert first.json()["pickup_attempt_id"] == replay.json()["pickup_attempt_id"]
            assert (
                await client.post(
                    f"/v1/rider/pickups/{fixture.pickup_ids[0][0]}/attempts",
                    headers=_headers(rider_token),
                    json={"client_attempt_id": str(attempt_id), "outcome": "COLLECTED"},
                )
            ).status_code == 409
            incident_payload = {
                "client_incident_id": str(incident_id),
                "reason_code": "CUSTOMER_UNAVAILABLE",
            }
            incident = await client.post(
                f"/v1/rider/pickups/{fixture.pickup_ids[0][0]}/incidents",
                headers=_headers(rider_token),
                json=incident_payload,
            )
            incident_replay = await client.post(
                f"/v1/rider/pickups/{fixture.pickup_ids[0][0]}/incidents",
                headers=_headers(rider_token),
                json=incident_payload,
            )
            assert incident.status_code == incident_replay.status_code == 200
            assert incident.json()["incident_id"] == incident_replay.json()["incident_id"]
            assert (
                await client.post(
                    f"/v1/rider/pickups/{fixture.pickup_ids[0][0]}/incidents",
                    headers=_headers(rider_token),
                    json={"client_incident_id": str(new_uuid7()), "reason_code": "INVALID"},
                )
            ).status_code == 422
            assert (
                await client.post(
                    f"/v1/rider/pickups/{fixture.pickup_ids[0][0]}/incidents",
                    headers=_headers(other_token),
                    json={
                        "client_incident_id": str(new_uuid7()),
                        "reason_code": "OTHER",
                    },
                )
            ).status_code == 404
            collected = await client.post(
                f"/v1/rider/pickups/{fixture.pickup_ids[0][1]}/attempts",
                headers=_headers(rider_token),
                json={"client_attempt_id": str(new_uuid7()), "outcome": "COLLECTED"},
            )
            assert collected.status_code == 200


async def test_handover_evidence_and_media_http_flow_is_attributed_and_provider_neutral(
    database_session_factory: async_sessionmaker[AsyncSession],
    migrated_database_url: str,
    address_protector: AddressProtector,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fixture = await create_fixture(database_session_factory, rider_count=2, pickups_per_group=2)
    rider_token, _ = await _token_for(database_session_factory, fixture.rider_ids[0])
    other_token, _ = await _token_for(database_session_factory, fixture.rider_ids[1])
    assignment = await assign_group_manually(
        database_session_factory,
        collection_group_id=fixture.group_ids[0],
        rider_id=fixture.rider_ids[0],
        manager_user_id=fixture.manager_id,
    )
    await start_assignment(
        database_session_factory,
        assignment_id=assignment.assignment_id,
        rider_id=fixture.rider_ids[0],
    )
    for pickup_id in fixture.pickup_ids[0]:
        await record_pickup_attempt(
            database_session_factory,
            pickup_execution_id=pickup_id,
            rider_id=fixture.rider_ids[0],
            client_attempt_id=new_uuid7(),
            outcome="COLLECTED",
        )
    active_point, inactive_point = await _seed_receiving_points(database_session_factory)
    storage = DeterministicMediaStorage()
    policy = ConfiguredMediaPolicy(
        allowed_content_types={"PHOTO": {"image/jpeg"}, "VIDEO": {"video/mp4"}},
        maximum_size_bytes={"PHOTO": 1_000_000, "VIDEO": 5_000_000},
    )
    application = _application(
        migrated_database_url,
        address_protector=address_protector,
        media_storage=storage,
        media_policy=policy,
    )
    handover_id = new_uuid7()
    capture_id = new_uuid7()
    caplog.set_level(logging.INFO)

    async with application.router.lifespan_context(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://test"
        ) as client:
            points = (
                await client.get("/v1/rider/receiving-points", headers=_headers(rider_token))
            ).json()
            assert [value["receiving_point_id"] for value in points] == [
                str(active_point.receiving_point_id)
            ]
            assert str(inactive_point.receiving_point_id) not in str(points)
            handover_payload = {
                "client_handover_id": str(handover_id),
                "receiving_point_id": str(active_point.receiving_point_id),
                "pickup_execution_ids": [str(fixture.pickup_ids[0][0])],
                "observed_location": {"latitude": 28.6139, "longitude": 77.2090},
            }
            handover = await client.post(
                "/v1/rider/handovers",
                headers=_headers(rider_token),
                json=handover_payload,
            )
            replay = await client.post(
                "/v1/rider/handovers",
                headers=_headers(rider_token),
                json=handover_payload,
            )
            assert handover.status_code == replay.status_code == 200
            assert handover.json()["status"] == "VALIDATED"
            assert "observed_location" not in handover.json()
            rejected = await client.post(
                "/v1/rider/handovers",
                headers=_headers(rider_token),
                json={
                    "client_handover_id": str(new_uuid7()),
                    "receiving_point_id": str(active_point.receiving_point_id),
                    "pickup_execution_ids": [str(fixture.pickup_ids[0][1])],
                    "observed_location": {"latitude": 29.0, "longitude": 78.0},
                },
            )
            assert rejected.status_code == 200
            assert rejected.json()["status"] == "REJECTED"
            assert (
                await client.post(
                    "/v1/rider/handovers",
                    headers=_headers(other_token),
                    json={**handover_payload, "client_handover_id": str(new_uuid7())},
                )
            ).status_code == 404

            capture_payload = {
                "client_capture_id": str(capture_id),
                "target_kind": "PICKUP",
                "target_id": str(fixture.pickup_ids[0][0]),
                "captured_at": utc_now().isoformat(),
            }
            capture = await client.post(
                "/v1/rider/evidence-captures",
                headers=_headers(rider_token),
                json=capture_payload,
            )
            capture_replay = await client.post(
                "/v1/rider/evidence-captures",
                headers=_headers(rider_token),
                json=capture_payload,
            )
            assert capture.status_code == capture_replay.status_code == 200
            assert (
                capture.json()["evidence_capture_id"]
                == capture_replay.json()["evidence_capture_id"]
            )
            assert (
                await client.post(
                    "/v1/rider/evidence-captures",
                    headers=_headers(rider_token),
                    json={
                        **capture_payload,
                        "client_capture_id": str(new_uuid7()),
                        "target_kind": "X",
                    },
                )
            ).status_code == 422
            assert (
                await client.post(
                    "/v1/rider/evidence-captures",
                    headers=_headers(rider_token),
                    json={
                        **capture_payload,
                        "client_capture_id": str(new_uuid7()),
                        "captured_at": "2026-01-01T00:00:00",
                    },
                )
            ).status_code == 422
            assert (
                await client.post(
                    "/v1/rider/evidence-captures",
                    headers=_headers(other_token),
                    json={**capture_payload, "client_capture_id": str(new_uuid7())},
                )
            ).status_code == 404

            registration = await client.post(
                "/v1/rider/media-assets",
                headers=_headers(rider_token),
                json={
                    "client_media_id": str(new_uuid7()),
                    "evidence_capture_id": capture.json()["evidence_capture_id"],
                    "media_type": "PHOTO",
                    "expected_content_type": "image/jpeg",
                },
            )
            assert registration.status_code == 200
            asset = registration.json()
            authorization = await client.post(
                f"/v1/rider/media-assets/{asset['media_asset_id']}/upload-authorization",
                headers=_headers(rider_token),
            )
            assert authorization.status_code == 200
            assert authorization.json()["authorization"] == storage.authorization
            assert storage.authorization not in caplog.text
            assert (
                await client.post(
                    f"/v1/rider/media-assets/{asset['media_asset_id']}/upload-authorization",
                    headers=_headers(other_token),
                )
            ).status_code == 404
            storage.properties[asset["object_key"]] = StoredObjectProperties(
                content_type="image/jpeg", size_bytes=1234
            )
            finalized = await client.post(
                f"/v1/rider/media-assets/{asset['media_asset_id']}/finalize",
                headers=_headers(rider_token),
            )
            assert finalized.status_code == 200
            assert finalized.json()["upload_status"] == "FINALIZED"
            assert (
                await client.post(
                    f"/v1/rider/media-assets/{asset['media_asset_id']}/upload-authorization",
                    headers=_headers(rider_token),
                )
            ).status_code == 409
            assert (
                await client.post(
                    "/v1/rider/media-assets",
                    headers=_headers(rider_token),
                    json={
                        "client_media_id": str(new_uuid7()),
                        "evidence_capture_id": capture.json()["evidence_capture_id"],
                        "media_type": "PHOTO",
                        "expected_content_type": "image/jpeg",
                    },
                )
            ).status_code == 409
    assert "28.6139" not in caplog.text
    assert "77.209" not in caplog.text


async def test_media_missing_object_mismatch_and_unconfigured_runtime_are_controlled(
    database_session_factory: async_sessionmaker[AsyncSession],
    migrated_database_url: str,
    address_protector: AddressProtector,
) -> None:
    fixture = await create_fixture(database_session_factory, rider_count=1, pickups_per_group=1)
    rider_token, _ = await _token_for(database_session_factory, fixture.rider_ids[0])
    assignment = await assign_group_manually(
        database_session_factory,
        collection_group_id=fixture.group_ids[0],
        rider_id=fixture.rider_ids[0],
        manager_user_id=fixture.manager_id,
    )
    await start_assignment(
        database_session_factory,
        assignment_id=assignment.assignment_id,
        rider_id=fixture.rider_ids[0],
    )
    await record_pickup_attempt(
        database_session_factory,
        pickup_execution_id=fixture.pickup_ids[0][0],
        rider_id=fixture.rider_ids[0],
        client_attempt_id=new_uuid7(),
        outcome="COLLECTED",
    )
    capture = await record_evidence_capture(
        database_session_factory,
        client_capture_id=new_uuid7(),
        captured_by_user_id=fixture.rider_ids[0],
        target_kind="PICKUP",
        target_id=fixture.pickup_ids[0][0],
        captured_at=utc_now(),
        idempotency_expires_at=utc_now() + timedelta(hours=1),
    )
    storage = DeterministicMediaStorage()
    policy = ConfiguredMediaPolicy(
        allowed_content_types={"PHOTO": {"image/jpeg"}},
        maximum_size_bytes={"PHOTO": 1000},
    )
    configured = _application(
        migrated_database_url,
        address_protector=address_protector,
        media_storage=storage,
        media_policy=policy,
    )
    async with configured.router.lifespan_context(configured):
        async with AsyncClient(
            transport=ASGITransport(app=configured), base_url="http://test"
        ) as client:
            registration = await client.post(
                "/v1/rider/media-assets",
                headers=_headers(rider_token),
                json={
                    "client_media_id": str(new_uuid7()),
                    "evidence_capture_id": str(capture.evidence_capture_id),
                    "media_type": "PHOTO",
                    "expected_content_type": "image/jpeg",
                },
            )
            asset = registration.json()
            finalize_url = f"/v1/rider/media-assets/{asset['media_asset_id']}/finalize"
            assert (
                await client.post(finalize_url, headers=_headers(rider_token))
            ).status_code == 409
            storage.properties[asset["object_key"]] = StoredObjectProperties(
                content_type="image/png", size_bytes=100
            )
            assert (
                await client.post(finalize_url, headers=_headers(rider_token))
            ).status_code == 409
            storage.properties[asset["object_key"]] = StoredObjectProperties(
                content_type="image/jpeg", size_bytes=1001
            )
            assert (
                await client.post(finalize_url, headers=_headers(rider_token))
            ).status_code == 409
    async with database_session_factory() as session:
        durable = await session.get(MediaAsset, UUID(asset["media_asset_id"]))
        assert durable is not None
        assert durable.upload_status == "PENDING_UPLOAD"

    unconfigured = _application(migrated_database_url, address_protector=address_protector)
    async with unconfigured.router.lifespan_context(unconfigured):
        async with AsyncClient(
            transport=ASGITransport(app=unconfigured), base_url="http://test"
        ) as client:
            assert (
                await client.post(
                    "/v1/rider/media-assets",
                    headers=_headers(rider_token),
                    json={
                        "client_media_id": str(new_uuid7()),
                        "evidence_capture_id": str(capture.evidence_capture_id),
                        "media_type": "PHOTO",
                        "expected_content_type": "image/jpeg",
                    },
                )
            ).status_code == 503
            assert (
                await client.post(
                    f"/v1/rider/media-assets/{asset['media_asset_id']}/upload-authorization",
                    headers=_headers(rider_token),
                )
            ).status_code == 503


async def test_manager_views_manual_assignment_and_fresh_role_eligibility(
    database_session_factory: async_sessionmaker[AsyncSession],
    migrated_database_url: str,
    address_protector: AddressProtector,
) -> None:
    fixture = await create_fixture(
        database_session_factory, group_count=3, rider_count=2, pickups_per_group=2
    )
    manager_token, _ = await _token_for(
        database_session_factory, fixture.manager_id, roles=("MANAGER",)
    )
    rider_token, _ = await _token_for(database_session_factory, fixture.rider_ids[0])
    customer_id = await _new_user(database_session_factory)
    customer_token, _ = await _token_for(database_session_factory, customer_id, roles=("CUSTOMER",))
    application = _application(migrated_database_url, address_protector=address_protector)

    async with application.router.lifespan_context(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://test"
        ) as client:
            riders = await client.get("/v1/manager/riders?limit=1", headers=_headers(manager_token))
            assert riders.status_code == 200
            assert len(riders.json()) == 1
            assert set(riders.json()[0]) == {
                "rider_id",
                "profile_status",
                "vehicle_type_code",
                "capacity_class_code",
                "availability_intent",
                "work_state",
                "version",
                "updated_at",
            }
            pending = await client.get(
                "/v1/manager/collection-groups/pending-assignment?limit=2",
                headers=_headers(manager_token),
            )
            assert pending.status_code == 200
            assert len(pending.json()) == 2
            assert pending.json()[0]["cell_id"] == "dispatch-cell"
            assert "address" not in str(pending.json()).lower()
            assert "location" not in str(pending.json()).lower()
            assert (
                await client.post(
                    f"/v1/manager/collection-groups/{fixture.group_ids[0]}/assign",
                    headers=_headers(customer_token),
                    json={"rider_id": str(fixture.rider_ids[0])},
                )
            ).status_code == 403
            assert (
                await client.post(
                    f"/v1/manager/collection-groups/{fixture.group_ids[0]}/assign",
                    headers=_headers(rider_token),
                    json={"rider_id": str(fixture.rider_ids[0])},
                )
            ).status_code == 403
            spoof = await client.post(
                f"/v1/manager/collection-groups/{fixture.group_ids[0]}/assign",
                headers=_headers(manager_token),
                json={
                    "rider_id": str(fixture.rider_ids[0]),
                    "manager_user_id": str(customer_id),
                },
            )
            assert spoof.status_code == 422
            assigned = await client.post(
                f"/v1/manager/collection-groups/{fixture.group_ids[0]}/assign",
                headers=_headers(manager_token),
                json={"rider_id": str(fixture.rider_ids[0])},
            )
            assert assigned.status_code == 200
            async with database_session_factory() as session:
                durable = await session.get(RiderAssignment, UUID(assigned.json()["assignment_id"]))
                assert durable is not None
                assert durable.assigned_by_user_id == fixture.manager_id
            remaining = await client.get(
                "/v1/manager/collection-groups/pending-assignment",
                headers=_headers(manager_token),
            )
            assert str(fixture.group_ids[0]) not in {
                value["collection_group_id"] for value in remaining.json()
            }

            async with database_session_factory() as session, session.begin():
                await session.execute(
                    update(UserRole)
                    .where(
                        UserRole.user_id == fixture.rider_ids[1],
                        UserRole.role_code == "RIDER",
                        UserRole.revoked_at.is_(None),
                    )
                    .values(revoked_at=utc_now())
                )
            assert (
                await client.post(
                    f"/v1/manager/collection-groups/{fixture.group_ids[1]}/assign",
                    headers=_headers(manager_token),
                    json={"rider_id": str(fixture.rider_ids[1])},
                )
            ).status_code == 409
            visible = await client.get("/v1/manager/riders", headers=_headers(manager_token))
            assert str(fixture.rider_ids[1]) not in {value["rider_id"] for value in visible.json()}

    with pytest.raises(RiderNotEligibleError):
        await create_assignment_offer(
            database_session_factory,
            collection_group_id=fixture.group_ids[1],
            rider_id=fixture.rider_ids[1],
            offer_round=1,
            expires_at=utc_now() + timedelta(minutes=5),
        )
    async with database_session_factory() as session, session.begin():
        session.add(
            UserRole(
                user_role_id=new_uuid7(),
                user_id=fixture.rider_ids[1],
                role_code="RIDER",
                granted_at=utc_now(),
                granted_by_user_id=None,
                revoked_at=None,
                revoked_by_user_id=None,
            )
        )
        await session.execute(
            update(AppUser).where(AppUser.user_id == fixture.rider_ids[1]).values(status="DISABLED")
        )
    with pytest.raises(RiderNotEligibleError):
        await assign_group_manually(
            database_session_factory,
            collection_group_id=fixture.group_ids[2],
            rider_id=fixture.rider_ids[1],
            manager_user_id=fixture.manager_id,
        )


async def test_fresh_assignment_authorization_is_locked_and_historical_replays_survive(
    database_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = await create_fixture(
        database_session_factory, group_count=3, rider_count=3, pickups_per_group=1
    )
    offer = await create_offer(database_session_factory, fixture, group_index=0, rider_index=0)
    offered_assignment = await accept_assignment_offer(
        database_session_factory,
        offer_id=offer.offer_id,
        rider_id=fixture.rider_ids[0],
    )
    manual_assignment = await assign_group_manually(
        database_session_factory,
        collection_group_id=fixture.group_ids[1],
        rider_id=fixture.rider_ids[1],
        manager_user_id=fixture.manager_id,
    )
    async with database_session_factory() as session, session.begin():
        await session.execute(
            update(UserRole)
            .where(UserRole.user_id.in_(fixture.rider_ids[:2]), UserRole.role_code == "RIDER")
            .values(revoked_at=utc_now())
        )
        await session.execute(
            update(AppUser)
            .where(AppUser.user_id.in_(fixture.rider_ids[:2]))
            .values(status="DISABLED")
        )
    assert (
        await accept_assignment_offer(
            database_session_factory,
            offer_id=offer.offer_id,
            rider_id=fixture.rider_ids[0],
        )
    ).assignment_id == offered_assignment.assignment_id
    assert (
        await assign_group_manually(
            database_session_factory,
            collection_group_id=fixture.group_ids[1],
            rider_id=fixture.rider_ids[1],
            manager_user_id=fixture.manager_id,
        )
    ).assignment_id == manual_assignment.assignment_id

    authorization_locked = asyncio.Event()
    allow_assignment = asyncio.Event()
    original = dispatch_service._require_fresh_rider_authorization

    async def gated_authorization(session: AsyncSession, rider_id: UUID) -> None:
        await original(session, rider_id)
        authorization_locked.set()
        await allow_assignment.wait()

    monkeypatch.setattr(
        dispatch_service,
        "_require_fresh_rider_authorization",
        gated_authorization,
    )
    assignment_task = asyncio.create_task(
        assign_group_manually(
            database_session_factory,
            collection_group_id=fixture.group_ids[2],
            rider_id=fixture.rider_ids[2],
            manager_user_id=fixture.manager_id,
        )
    )
    await asyncio.wait_for(authorization_locked.wait(), timeout=2)

    async def revoke_current_eligibility() -> None:
        async with database_session_factory() as session, session.begin():
            await session.execute(
                update(AppUser)
                .where(AppUser.user_id == fixture.rider_ids[2])
                .values(status="DISABLED")
            )
            await session.execute(
                update(UserRole)
                .where(
                    UserRole.user_id == fixture.rider_ids[2],
                    UserRole.role_code == "RIDER",
                    UserRole.revoked_at.is_(None),
                )
                .values(revoked_at=utc_now())
            )

    revocation_task = asyncio.create_task(revoke_current_eligibility())
    await asyncio.sleep(0.1)
    assert not revocation_task.done()
    allow_assignment.set()
    established = await asyncio.wait_for(assignment_task, timeout=2)
    await asyncio.wait_for(revocation_task, timeout=2)
    assert established.rider_id == fixture.rider_ids[2]


async def test_fresh_reassignment_authorization_is_locked_and_completed_replay_survives(
    database_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = await create_fixture(database_session_factory, rider_count=2, pickups_per_group=1)
    predecessor = await assign_group_manually(
        database_session_factory,
        collection_group_id=fixture.group_ids[0],
        rider_id=fixture.rider_ids[0],
        manager_user_id=fixture.manager_id,
    )
    authorization_locked = asyncio.Event()
    allow_reassignment = asyncio.Event()
    original = operations_service.lock_active_user_role

    async def gated_authorization(session: AsyncSession, *, user_id: UUID, role_code: str) -> bool:
        eligible = await original(session, user_id=user_id, role_code=role_code)
        authorization_locked.set()
        await allow_reassignment.wait()
        return eligible

    monkeypatch.setattr(operations_service, "lock_active_user_role", gated_authorization)
    command_id = new_uuid7()
    expires_at = utc_now() + timedelta(hours=1)
    reassignment_task = asyncio.create_task(
        reassign_outstanding_work(
            database_session_factory,
            predecessor_assignment_id=predecessor.assignment_id,
            replacement_rider_id=fixture.rider_ids[1],
            manager_user_id=fixture.manager_id,
            client_reassignment_id=command_id,
            idempotency_expires_at=expires_at,
        )
    )
    await asyncio.wait_for(authorization_locked.wait(), timeout=2)

    async def disable_and_revoke_replacement() -> None:
        async with database_session_factory() as session, session.begin():
            await session.execute(
                update(AppUser)
                .where(AppUser.user_id == fixture.rider_ids[1])
                .values(status="DISABLED")
            )
            await session.execute(
                update(UserRole)
                .where(
                    UserRole.user_id == fixture.rider_ids[1],
                    UserRole.role_code == "RIDER",
                    UserRole.revoked_at.is_(None),
                )
                .values(revoked_at=utc_now())
            )

    revocation_task = asyncio.create_task(disable_and_revoke_replacement())
    await asyncio.sleep(0.1)
    assert not revocation_task.done()
    allow_reassignment.set()
    successor = await asyncio.wait_for(reassignment_task, timeout=2)
    await asyncio.wait_for(revocation_task, timeout=2)
    replay = await reassign_outstanding_work(
        database_session_factory,
        predecessor_assignment_id=predecessor.assignment_id,
        replacement_rider_id=fixture.rider_ids[1],
        manager_user_id=fixture.manager_id,
        client_reassignment_id=command_id,
        idempotency_expires_at=expires_at,
    )
    assert replay.assignment_id == successor.assignment_id


async def test_manager_incident_reassignment_and_completion_endpoints(
    database_session_factory: async_sessionmaker[AsyncSession],
    migrated_database_url: str,
    address_protector: AddressProtector,
) -> None:
    fixture = await create_fixture(
        database_session_factory, group_count=2, rider_count=3, pickups_per_group=1
    )
    manager_token, _ = await _token_for(
        database_session_factory, fixture.manager_id, roles=("MANAGER",)
    )
    predecessor_token, _ = await _token_for(database_session_factory, fixture.rider_ids[0])
    assignment = await assign_group_manually(
        database_session_factory,
        collection_group_id=fixture.group_ids[0],
        rider_id=fixture.rider_ids[0],
        manager_user_id=fixture.manager_id,
    )
    await start_assignment(
        database_session_factory,
        assignment_id=assignment.assignment_id,
        rider_id=fixture.rider_ids[0],
    )
    incident = await open_pickup_incident(
        database_session_factory,
        pickup_execution_id=fixture.pickup_ids[0][0],
        rider_id=fixture.rider_ids[0],
        client_incident_id=new_uuid7(),
        reason_code="RIDER_UNABLE_TO_CONTINUE",
    )
    application = _application(migrated_database_url, address_protector=address_protector)
    reassignment_id = new_uuid7()
    async with application.router.lifespan_context(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://test"
        ) as client:
            incidents = await client.get("/v1/manager/incidents", headers=_headers(manager_token))
            assert incidents.status_code == 200
            assert [value["incident_id"] for value in incidents.json()] == [
                str(incident.incident_id)
            ]
            assert "address" not in str(incidents.json()).lower()
            payload = {
                "client_reassignment_id": str(reassignment_id),
                "replacement_rider_id": str(fixture.rider_ids[1]),
                "incident_id": str(incident.incident_id),
            }
            successor = await client.post(
                f"/v1/manager/assignments/{assignment.assignment_id}/reassign",
                headers=_headers(manager_token),
                json=payload,
            )
            async with database_session_factory() as session, session.begin():
                await session.execute(
                    update(UserRole)
                    .where(
                        UserRole.user_id == fixture.rider_ids[1],
                        UserRole.role_code == "RIDER",
                        UserRole.revoked_at.is_(None),
                    )
                    .values(revoked_at=utc_now())
                )
                await session.execute(
                    update(AppUser)
                    .where(AppUser.user_id == fixture.rider_ids[1])
                    .values(status="DISABLED")
                )
            replay = await client.post(
                f"/v1/manager/assignments/{assignment.assignment_id}/reassign",
                headers=_headers(manager_token),
                json=payload,
            )
            assert successor.status_code == replay.status_code == 200
            assert successor.json()["assignment_id"] == replay.json()["assignment_id"]
            assert (
                await client.get("/v1/rider/me/assignment", headers=_headers(predecessor_token))
            ).json() == {"assignment": None}
            assert (
                await client.post(
                    f"/v1/manager/assignments/{assignment.assignment_id}/reassign",
                    headers=_headers(manager_token),
                    json={**payload, "replacement_rider_id": str(fixture.rider_ids[2])},
                )
            ).status_code == 409
            async with database_session_factory() as session:
                durable_incident = await session.get(PickupIncident, incident.incident_id)
                assert durable_incident is not None
                assert durable_incident.resolved_by_user_id == fixture.manager_id
            request_id = await _request_for_pickup(
                database_session_factory, fixture.pickup_ids[1][0]
            )
            assert (
                await client.post(
                    f"/v1/manager/collection-requests/{request_id}/complete",
                    headers=_headers(manager_token),
                )
            ).status_code == 409

    second_assignment = await assign_group_manually(
        database_session_factory,
        collection_group_id=fixture.group_ids[1],
        rider_id=fixture.rider_ids[2],
        manager_user_id=fixture.manager_id,
    )
    async with database_session_factory() as session, session.begin():
        await session.execute(
            update(UserRole)
            .where(
                UserRole.user_id == fixture.rider_ids[1],
                UserRole.role_code == "RIDER",
                UserRole.revoked_at.is_(None),
            )
            .values(revoked_at=utc_now())
        )
    with pytest.raises(ReassignmentRiderError):
        await reassign_outstanding_work(
            database_session_factory,
            predecessor_assignment_id=second_assignment.assignment_id,
            replacement_rider_id=fixture.rider_ids[1],
            manager_user_id=fixture.manager_id,
            client_reassignment_id=new_uuid7(),
            idempotency_expires_at=utc_now() + timedelta(hours=1),
        )


async def _request_for_pickup(factory: async_sessionmaker[AsyncSession], pickup_id: UUID) -> UUID:
    async with factory() as session:
        value = await session.scalar(
            select(PickupExecution.request_id).where(
                PickupExecution.pickup_execution_id == pickup_id
            )
        )
        assert value is not None
        return value


async def test_manager_completion_success_replay_and_no_new_outbox_event(
    database_session_factory: async_sessionmaker[AsyncSession],
    migrated_database_url: str,
    address_protector: AddressProtector,
) -> None:
    fixture = await create_fixture(database_session_factory, rider_count=1, pickups_per_group=1)
    manager_token, _ = await _token_for(
        database_session_factory, fixture.manager_id, roles=("MANAGER",)
    )
    assignment = await assign_group_manually(
        database_session_factory,
        collection_group_id=fixture.group_ids[0],
        rider_id=fixture.rider_ids[0],
        manager_user_id=fixture.manager_id,
    )
    await start_assignment(
        database_session_factory,
        assignment_id=assignment.assignment_id,
        rider_id=fixture.rider_ids[0],
    )
    pickup_id = fixture.pickup_ids[0][0]
    await record_pickup_attempt(
        database_session_factory,
        pickup_execution_id=pickup_id,
        rider_id=fixture.rider_ids[0],
        client_attempt_id=new_uuid7(),
        outcome="COLLECTED",
    )
    point, _ = await _seed_receiving_points(database_session_factory)
    handover = await record_handover(
        database_session_factory,
        client_handover_id=new_uuid7(),
        rider_id=fixture.rider_ids[0],
        receiving_point_id=point.receiving_point_id,
        pickup_execution_ids=[pickup_id],
        observed_location=GeoPoint(latitude=28.6139, longitude=77.2090),
        idempotency_expires_at=utc_now() + timedelta(hours=1),
    )
    await record_evidence_capture(
        database_session_factory,
        client_capture_id=new_uuid7(),
        captured_by_user_id=fixture.rider_ids[0],
        target_kind="PICKUP",
        target_id=pickup_id,
        captured_at=utc_now(),
        idempotency_expires_at=utc_now() + timedelta(hours=1),
    )
    await record_evidence_capture(
        database_session_factory,
        client_capture_id=new_uuid7(),
        captured_by_user_id=fixture.rider_ids[0],
        target_kind="HANDOVER",
        target_id=handover.handover_event_id,
        captured_at=utc_now(),
        idempotency_expires_at=utc_now() + timedelta(hours=1),
    )
    request_id = await _request_for_pickup(database_session_factory, pickup_id)
    async with database_session_factory() as session:
        before = int(await session.scalar(select(func.count()).select_from(OutboxEvent)) or 0)
    application = _application(migrated_database_url, address_protector=address_protector)
    async with application.router.lifespan_context(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://test"
        ) as client:
            first = await client.post(
                f"/v1/manager/collection-requests/{request_id}/complete",
                headers=_headers(manager_token),
            )
            replay = await client.post(
                f"/v1/manager/collection-requests/{request_id}/complete",
                headers=_headers(manager_token),
            )
            assert first.status_code == replay.status_code == 200
            assert first.json()["status"] == "COMPLETED"
            assert first.json()["completed_at"] == replay.json()["completed_at"]
    async with database_session_factory() as session:
        after = int(await session.scalar(select(func.count()).select_from(OutboxEvent)) or 0)
    assert after == before
