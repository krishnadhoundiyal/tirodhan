from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol
from uuid import UUID

ROUTING_CONTRACT_V1 = "DYNAMIC_COLLECTION_ROUTING_V1"
ROUTING_CONTRACT_V2 = "DYNAMIC_COLLECTION_ROUTING_V2"
ROUTING_CONTRACT_V3 = "DYNAMIC_COLLECTION_ROUTING_V3"


class OptimizerEngine(str, Enum):
    VROOM = "VROOM"
    OR_TOOLS = "OR_TOOLS"


class SelectionEngine(str, Enum):
    LOCAL_REPAIR = "LOCAL_REPAIR"
    COMPOSITE = "COMPOSITE"


class ResourceKind(str, Enum):
    COMMITTED = "COMMITTED"
    FLEXIBLE = "FLEXIBLE"


class StopKind(str, Enum):
    PICKUP = "PICKUP"
    DISCHARGE = "DISCHARGE"


@dataclass(frozen=True, slots=True)
class TimeWindow:
    """Inclusive seconds relative to the problem's UTC epoch."""

    start: int
    end: int


@dataclass(frozen=True, slots=True)
class RoutingLimits:
    """Caller-configured technical working-set bounds, not booking admission policy."""

    max_nodes: int
    max_stops: int
    max_vehicles: int
    max_matrices: int


@dataclass(frozen=True, slots=True)
class RoadArc:
    origin: UUID
    destination: UUID
    seconds: int | None
    metres: int | None


@dataclass(frozen=True, slots=True)
class RoadMatrix:
    matrix_id: UUID
    graph_version: str
    profile_version: str
    node_ids: tuple[UUID, ...]
    travel_seconds: tuple[tuple[int | None, ...], ...]
    distance_m: tuple[tuple[int | None, ...], ...]
    sparse_arcs: tuple[RoadArc, ...] | None = None
    snap_tokens: tuple[tuple[str, str], ...] = ()

    def arc(
        self, origin: UUID, destination: UUID, indices: dict[UUID, int]
    ) -> tuple[int | None, int | None]:
        if self.sparse_arcs is not None:
            key = (origin.int, destination.int)
            position = bisect_left(
                self.sparse_arcs, key, key=lambda a: (a.origin.int, a.destination.int)
            )
            if position == len(self.sparse_arcs) or (
                self.sparse_arcs[position].origin,
                self.sparse_arcs[position].destination,
            ) != (origin, destination):
                raise KeyError("ROAD_ARC_NOT_ACQUIRED")
            arc = self.sparse_arcs[position]
            return arc.seconds, arc.metres
        i, j = indices[origin], indices[destination]
        return self.travel_seconds[i][j], self.distance_m[i][j]


@dataclass(frozen=True, slots=True)
class CargoLot:
    lot_id: UUID
    collection_request_id: UUID
    quantity: int
    quantity_unit: str
    unit_load: tuple[int, ...]
    authorized_receiving_point_ids: tuple[UUID, ...]
    discharge_deadline: int
    pickup_stop_id: UUID | None


@dataclass(frozen=True, slots=True)
class CargoBalance:
    lot_id: UUID
    quantity: int


@dataclass(frozen=True, slots=True)
class VehicleSnapshot:
    vehicle_id: UUID
    rider_id: UUID
    state_version: int
    resource_kind: ResourceKind
    operating_shift_id: UUID | None
    matrix_id: UUID
    start_node_id: UUID
    end_node_id: UUID
    available_at: int
    shift: TimeWindow
    capacity: tuple[int, ...]
    initial_cargo: tuple[CargoBalance, ...]
    protected_prefix: tuple[UUID, ...]
    breaks: tuple[TimeWindow, ...] = ()


@dataclass(frozen=True, slots=True)
class ReceivingPointSnapshot:
    receiving_point_id: UUID
    node_id: UUID
    state_version: int
    available: bool
    opening_windows: tuple[TimeWindow, ...]


@dataclass(frozen=True, slots=True)
class RoutingStop:
    stop_id: UUID
    node_id: UUID
    kind: StopKind
    lot_id: UUID
    quantity: int
    service_seconds: int
    service_start_window: TimeWindow
    required: bool
    eligible_vehicle_ids: tuple[UUID, ...]
    bound_vehicle_id: UUID | None
    receiving_point_id: UUID | None


@dataclass(frozen=True, slots=True)
class RoutingProblem:
    problem_id: UUID
    snapshot_id: UUID
    contract_version: str
    policy_version: str
    epoch: datetime
    load_dimensions: tuple[str, ...]
    limits: RoutingLimits
    matrices: tuple[RoadMatrix, ...]
    vehicles: tuple[VehicleSnapshot, ...]
    receiving_points: tuple[ReceivingPointSnapshot, ...]
    lots: tuple[CargoLot, ...]
    stops: tuple[RoutingStop, ...]


@dataclass(frozen=True, slots=True)
class ProposedRoute:
    vehicle_id: UUID
    stop_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class RouteProposal:
    problem_id: UUID
    snapshot_id: UUID
    contract_version: str
    policy_version: str
    engine: OptimizerEngine | SelectionEngine
    engine_version: str
    routes: tuple[ProposedRoute, ...]
    unserved_stop_ids: tuple[UUID, ...]


class RouteOptimizer(Protocol):
    """Both engine adapters must return proposals, never assignment/custody mutations."""

    async def propose(self, problem: RoutingProblem, *, search_seconds: int) -> RouteProposal: ...
