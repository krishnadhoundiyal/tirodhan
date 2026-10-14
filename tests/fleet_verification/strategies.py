from datetime import datetime, timezone
from uuid import UUID

from hypothesis import strategies as st

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

from .oracle import Weights


@st.composite
def fleet_problems(draw, *, max_lots=3):
    count = draw(st.integers(1, max_lots))
    fleet_size = draw(st.integers(1, 2))
    loaded = draw(st.booleans())
    owned = draw(st.booleans())
    protected = owned and draw(st.booleans())
    vehicles = tuple(UUID(int=100 + i) for i in range(fleet_size))
    starts = tuple(UUID(int=200 + i) for i in range(fleet_size))
    pickups = tuple(UUID(int=300 + i) for i in range(count))
    facilities = tuple(UUID(int=400 + i) for i in range(draw(st.integers(1, 2))))
    facility = facilities[0]
    nodes = starts + pickups + facilities
    size = len(nodes)
    edges = draw(st.lists(st.integers(1, 50), min_size=size * size, max_size=size * size))
    times = [[0 if i == j else edges[i * size + j] for j in range(size)] for i in range(size)]
    # Directed shortest-path closure: asymmetric but consistent with a road graph.
    for k in range(size):
        for i in range(size):
            for j in range(size):
                times[i][j] = min(times[i][j], times[i][k] + times[k][j])
    matrix = RoadMatrix(
        UUID(int=500),
        "generated-directed-graph",
        "test-only",
        nodes,
        tuple(tuple(row) for row in times),
        tuple(tuple(value * 3 for value in row) for row in times),
    )
    lots, stops, incumbent = [], [], {}
    if loaded:
        cargo_id, discharge_id = UUID(int=600), UUID(int=601)
        lots.append(CargoLot(cargo_id, cargo_id, 1, "HOUSEHOLD", (1, 1), (facility,), 900, None))
        stops.append(
            RoutingStop(
                discharge_id,
                facility,
                StopKind.DISCHARGE,
                cargo_id,
                1,
                3,
                TimeWindow(0, 900),
                True,
                vehicles,
                vehicles[0],
                facility,
            )
        )
        incumbent[vehicles[0]] = (discharge_id,)
    for i, node in enumerate(pickups):
        destination = draw(st.sampled_from(facilities))
        lot_id, pickup_id, discharge_id = UUID(int=700 + i), UUID(int=800 + i), UUID(int=900 + i)
        start = draw(st.integers(0, 120))
        end = start + draw(st.integers(10, 300))
        required = owned and i == 0
        bound = vehicles[0] if required else None
        lots.append(
            CargoLot(
                lot_id,
                lot_id,
                1,
                "HOUSEHOLD",
                tuple(draw(st.integers(1, 3)) for _ in range(2)),
                (destination,),
                900,
                pickup_id,
            )
        )
        stops.extend(
            (
                RoutingStop(
                    pickup_id,
                    node,
                    StopKind.PICKUP,
                    lot_id,
                    1,
                    draw(st.integers(0, 8)),
                    TimeWindow(start, end),
                    required,
                    vehicles,
                    bound,
                    None,
                ),
                RoutingStop(
                    discharge_id,
                    destination,
                    StopKind.DISCHARGE,
                    lot_id,
                    1,
                    draw(st.integers(0, 8)),
                    TimeWindow(0, 900),
                    required,
                    vehicles,
                    bound,
                    destination,
                ),
            )
        )
        if required:
            incumbent[vehicles[0]] = incumbent.get(vehicles[0], ()) + (pickup_id, discharge_id)
    snapshots = tuple(
        VehicleSnapshot(
            v,
            UUID(int=1000 + i),
            1,
            ResourceKind.COMMITTED,
            UUID(int=1100 + i),
            matrix.matrix_id,
            starts[i],
            facility,
            0,
            TimeWindow(0, draw(st.integers(200, 1000))),
            (draw(st.integers(1, 6)), draw(st.integers(1, 6))),
            (CargoBalance(UUID(int=600), 1),) if loaded and i == 0 else (),
            (incumbent[v][0],) if protected and i == 0 else (),
        )
        for i, v in enumerate(vehicles)
    )
    problem = RoutingProblem(
        UUID(int=1),
        UUID(int=2),
        ROUTING_CONTRACT_V1,
        "GENERATED_TEST_POLICY",
        datetime(2030, 1, 1, tzinfo=timezone.utc),
        ("MASS", "VOLUME"),
        RoutingLimits(16, 10, 2, 1),
        (matrix,),
        snapshots,
        tuple(
            ReceivingPointSnapshot(
                destination,
                destination,
                1,
                draw(st.sampled_from((True, True, False))),
                (TimeWindow(draw(st.integers(0, 150)), 1000),),
            )
            for destination in facilities
        ),
        tuple(lots),
        tuple(stops),
    )
    weights = Weights(*(draw(st.integers(0, limit)) for limit in (80, 3, 15, 30, 2)))
    return problem, weights, incumbent
