from datetime import datetime, timezone
from uuid import UUID

import pytest

from tirodhan.modules.planning.routing.contracts import (
    ROUTING_CONTRACT_V1,
    CargoBalance,
    CargoLot,
    ReceivingPointSnapshot,
    ResourceKind,
    RoadMatrix,
    RoutingLimits,
    RoutingProblem,
    RoutingStop,
    StopKind,
    TimeWindow,
    VehicleSnapshot,
)


def uid(value: int) -> UUID:
    return UUID(int=value)


@pytest.fixture
def routing_problem() -> RoutingProblem:
    matrix = tuple(tuple(0 if i == j else 10 for j in range(4)) for i in range(4))
    stops = (
        RoutingStop(
            uid(50),
            uid(1),
            StopKind.DISCHARGE,
            uid(40),
            4,
            5,
            TimeWindow(0, 900),
            True,
            (uid(30),),
            uid(30),
            uid(60),
        ),
        RoutingStop(
            uid(51),
            uid(2),
            StopKind.PICKUP,
            uid(42),
            3,
            120,
            TimeWindow(100, 110),
            True,
            (uid(30),),
            uid(30),
            None,
        ),
        RoutingStop(
            uid(52),
            uid(3),
            StopKind.DISCHARGE,
            uid(40),
            6,
            5,
            TimeWindow(0, 900),
            True,
            (uid(30),),
            uid(30),
            uid(61),
        ),
        RoutingStop(
            uid(53),
            uid(3),
            StopKind.DISCHARGE,
            uid(42),
            3,
            5,
            TimeWindow(0, 900),
            True,
            (uid(30),),
            uid(30),
            uid(61),
        ),
    )
    return RoutingProblem(
        uid(1000),
        uid(1001),
        ROUTING_CONTRACT_V1,
        "test-policy-1",
        datetime(2026, 10, 12, tzinfo=timezone.utc),
        ("grams", "milliliters"),
        RoutingLimits(20, 20, 5, 5),
        (
            RoadMatrix(
                uid(20), "graph-1", "profile-1", tuple(uid(i) for i in range(4)), matrix, matrix
            ),
        ),
        (
            VehicleSnapshot(
                uid(30),
                uid(31),
                7,
                ResourceKind.COMMITTED,
                uid(32),
                uid(20),
                uid(0),
                uid(0),
                0,
                TimeWindow(0, 1000),
                (24, 36),
                (CargoBalance(uid(40), 10),),
                (uid(50),),
            ),
        ),
        (
            ReceivingPointSnapshot(uid(60), uid(1), 2, True, (TimeWindow(0, 1000),)),
            ReceivingPointSnapshot(uid(61), uid(3), 2, True, (TimeWindow(0, 1000),)),
        ),
        (
            CargoLot(uid(40), uid(41), 10, "test-unit", (2, 3), (uid(60), uid(61)), 900, None),
            CargoLot(uid(42), uid(43), 3, "test-unit", (2, 3), (uid(61),), 900, uid(51)),
        ),
        stops,
    )
