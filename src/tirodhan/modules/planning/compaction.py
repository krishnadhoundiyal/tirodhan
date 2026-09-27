from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID

BOUNDED_GREEDY_DIAMETER_V1 = "BOUNDED_GREEDY_DIAMETER_V1"
COMPACTED = "COMPACTED"
NORMAL_SINGLETON = "NORMAL_SINGLETON"

_FAILURE_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


class PlanningPolicySnapshotError(RuntimeError):
    """The frozen batch cannot be executed by an available planner version."""


class PlanningAlgorithmFailure(RuntimeError):
    """A deliberately classified technical failure that consumes one logical attempt."""

    def __init__(self, failure_code: str) -> None:
        if not _FAILURE_CODE.fullmatch(failure_code):
            raise ValueError("planning algorithm failure code must be controlled and bounded")
        self.failure_code = failure_code
        super().__init__(failure_code)


@dataclass(frozen=True, slots=True)
class PlanningCandidateEdge:
    request_id_a: UUID
    request_id_b: UUID
    distance_m: float


@dataclass(frozen=True, slots=True)
class PlanningInput:
    planning_batch_id: UUID
    planning_batch_attempt_id: UUID
    request_ids: tuple[UUID, ...]
    candidate_edges: tuple[PlanningCandidateEdge, ...]
    compaction_distance_m: int
    max_group_requests: int
    algorithm_version: str


@dataclass(frozen=True, slots=True)
class PlannedGroup:
    planning_mode: str
    request_ids: tuple[UUID, ...]


def plan_compaction(planning_input: PlanningInput) -> tuple[PlannedGroup, ...]:
    if planning_input.algorithm_version != BOUNDED_GREEDY_DIAMETER_V1:
        raise PlanningPolicySnapshotError("unsupported planning algorithm version")
    if planning_input.compaction_distance_m <= 0:
        raise PlanningPolicySnapshotError("invalid compaction distance snapshot")
    if planning_input.max_group_requests <= 0:
        raise PlanningPolicySnapshotError("invalid max group requests snapshot")
    if not planning_input.request_ids:
        raise PlanningPolicySnapshotError("planning batch has no request population")
    if len(set(planning_input.request_ids)) != len(planning_input.request_ids):
        raise PlanningPolicySnapshotError("planning population contains duplicate request IDs")

    population = set(planning_input.request_ids)
    distances: dict[tuple[UUID, UUID], float] = {}
    for edge in planning_input.candidate_edges:
        if edge.request_id_a == edge.request_id_b:
            raise PlanningPolicySnapshotError("candidate edge cannot reference one request twice")
        if edge.request_id_a not in population or edge.request_id_b not in population:
            raise PlanningPolicySnapshotError("candidate edge contains a foreign request")
        if edge.distance_m < 0:
            raise PlanningPolicySnapshotError("candidate edge distance cannot be negative")
        pair = _pair_key(edge.request_id_a, edge.request_id_b)
        previous = distances.get(pair)
        if previous is None or edge.distance_m < previous:
            distances[pair] = edge.distance_m

    unassigned = set(population)
    groups: list[PlannedGroup] = []
    while unassigned:
        seed = min(unassigned, key=_uuid_key)
        candidates = [
            candidate
            for candidate in unassigned
            if candidate != seed and _pair_key(seed, candidate) in distances
        ]
        candidates.sort(
            key=lambda candidate: (distances[_pair_key(seed, candidate)], _uuid_key(candidate))
        )
        group = [seed]
        for candidate in candidates:
            if len(group) >= planning_input.max_group_requests:
                break
            if all(_pair_key(candidate, member) in distances for member in group):
                group.append(candidate)
        unassigned.difference_update(group)
        groups.append(
            PlannedGroup(
                planning_mode=COMPACTED if len(group) >= 2 else NORMAL_SINGLETON,
                request_ids=tuple(group),
            )
        )
    return tuple(groups)


def _uuid_key(value: UUID) -> int:
    return value.int


def _pair_key(left: UUID, right: UUID) -> tuple[UUID, UUID]:
    return (left, right) if _uuid_key(left) < _uuid_key(right) else (right, left)
