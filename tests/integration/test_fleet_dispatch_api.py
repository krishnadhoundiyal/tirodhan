from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update
from test_fleet_dispatch import CELL, fixture
from test_operational_api import _application, _headers, _token_for

from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.dispatch.models import (
    Fleet,
    FleetMembership,
    FleetServiceCell,
    PushRegistration,
    RiderServiceCell,
)
from tirodhan.modules.identity.models import AppUser, UserRole

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_rider_own_device_register_replace_revoke_and_live_rbac(
    database_session_factory, migrated_database_url
):
    factory = database_session_factory
    value = await fixture(factory)
    rider = value.rider_ids[0]
    token, _ = await _token_for(factory, rider)
    app = _application(migrated_database_url)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        body = {
            "client_device_id": "device",
            "platform": "ANDROID",
            "registration_token": "private-one",
        }
        response = await client.post(
            "/v1/rider/me/push-devices", json=body, headers=_headers(token)
        )
        assert response.status_code == 200
        established = response.json()
        assert "private" not in response.text
        replay = await client.post("/v1/rider/me/push-devices", json=body, headers=_headers(token))
        assert replay.json() == established
        replacement = await client.post(
            "/v1/rider/me/push-devices",
            json={**body, "registration_token": "private-two"},
            headers=_headers(token),
        )
        assert replacement.json() == established
        async with factory() as session:
            registration = await session.scalar(select(PushRegistration))
            assert registration.rider_id == rider
            assert registration.registration_token == "private-two"
            assert await session.scalar(select(func.count()).select_from(PushRegistration)) == 1
        spoofed = await client.post(
            "/v1/rider/me/push-devices",
            json={**body, "rider_id": str(value.rider_ids[1])},
            headers=_headers(token),
        )
        assert spoofed.status_code == 422
        assert (
            await client.delete("/v1/rider/me/push-devices/device", headers=_headers(token))
        ).status_code == 204
        assert (
            await client.delete("/v1/rider/me/push-devices/device", headers=_headers(token))
        ).status_code == 204
        async with factory() as session:
            assert (await session.scalar(select(PushRegistration))).revoked_at is not None
        async with factory() as session, session.begin():
            await session.execute(
                update(UserRole).where(UserRole.user_id == rider).values(revoked_at=utc_now())
            )
        assert (
            await client.post("/v1/rider/me/push-devices", json=body, headers=_headers(token))
        ).status_code == 403


async def test_manager_fleet_commands_replay_membership_history_coverage_and_rbac(
    database_session_factory, migrated_database_url
):
    factory = database_session_factory
    value = await fixture(factory, fleet_count=0, independent_count=2)
    manager_token, _ = await _token_for(factory, value.manager_id, roles=("MANAGER",))
    rider_token, _ = await _token_for(factory, value.rider_ids[0])
    app = _application(migrated_database_url)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):

        async def command(method, path, body, expected=200):
            payload = {"client_command_id": str(new_uuid7()), **body}
            response = await client.request(
                method, path, json=payload, headers=_headers(manager_token)
            )
            assert response.status_code == expected, response.text
            if expected == 200:
                replay = await client.request(
                    method, path, json=payload, headers=_headers(manager_token)
                )
                assert replay.json() == response.json()
            return response, payload

        response, create_payload = await command(
            "POST", "/v1/manager/fleets", {"name": "Test fleet"}
        )
        fleet_id = response.json()["resource_id"]
        other, _ = await command("POST", "/v1/manager/fleets", {"name": "Other fleet"})
        other_id = other.json()["resource_id"]
        assert (
            await client.post(
                "/v1/manager/fleets",
                json={**create_payload, "name": "Changed"},
                headers=_headers(manager_token),
            )
        ).status_code == 409
        assert (
            await client.post(
                "/v1/manager/fleets", json=create_payload, headers=_headers(rider_token)
            )
        ).status_code == 403
        assert (await client.post("/v1/manager/fleets", json=create_payload)).status_code == 401
        member, join_payload = await command(
            "POST",
            f"/v1/manager/fleets/{fleet_id}/memberships",
            {"rider_id": str(value.rider_ids[0])},
        )
        membership_id = member.json()["resource_id"]
        await command(
            "POST",
            f"/v1/manager/fleets/{other_id}/memberships",
            {"rider_id": str(value.rider_ids[0])},
            409,
        )
        await command(
            "PUT", f"/v1/manager/fleets/{fleet_id}/service-cells", {"cell_id": CELL, "active": True}
        )
        await command(
            "PUT",
            f"/v1/manager/fleets/{fleet_id}/service-cells",
            {"cell_id": CELL, "active": False},
        )
        await command(
            "PUT",
            f"/v1/manager/riders/{value.rider_ids[1]}/service-cells",
            {"cell_id": CELL, "active": False},
        )
        await command(
            "PUT",
            f"/v1/manager/riders/{value.rider_ids[1]}/service-cells",
            {"cell_id": CELL, "active": True},
        )
        await command("PUT", f"/v1/manager/fleets/{fleet_id}/status", {"status": "INACTIVE"})
        await command(
            "POST",
            f"/v1/manager/fleets/{fleet_id}/memberships",
            {"rider_id": str(value.rider_ids[1])},
            409,
        )
        await command("PUT", f"/v1/manager/fleets/{fleet_id}/status", {"status": "ACTIVE"})
        await command(
            "POST",
            f"/v1/manager/fleets/{fleet_id}/memberships/end",
            {"rider_id": str(value.rider_ids[0]), "membership_id": membership_id},
        )
        # Completed JOIN replay cannot silently recreate ended membership.
        stale = await client.post(
            f"/v1/manager/fleets/{fleet_id}/memberships",
            json=join_payload,
            headers=_headers(manager_token),
        )
        assert stale.json() == member.json()
        async with factory() as session, session.begin():
            await session.execute(
                update(AppUser)
                .where(AppUser.user_id == value.rider_ids[1])
                .values(status="DISABLED")
            )
        await command(
            "POST",
            f"/v1/manager/fleets/{fleet_id}/memberships",
            {"rider_id": str(value.rider_ids[1])},
            409,
        )
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(Fleet)) == 2
        assert await session.scalar(select(func.count()).select_from(FleetMembership)) == 1
        assert (await session.scalar(select(FleetMembership))).left_at is not None
        assert (await session.scalar(select(FleetServiceCell))).deactivated_at is not None
        assert (
            await session.scalar(
                select(func.count())
                .select_from(RiderServiceCell)
                .where(
                    RiderServiceCell.rider_id == value.rider_ids[1],
                    RiderServiceCell.deactivated_at.is_(None),
                )
            )
            == 1
        )
