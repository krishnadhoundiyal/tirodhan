from __future__ import annotations

import random
from collections.abc import Sequence
from uuid import UUID

from tirodhan.modules.planning.compaction import (
    BOUNDED_GREEDY_DIAMETER_V1,
    COMPACTED,
    NORMAL_SINGLETON,
    PlannedGroup,
    PlanningCandidateEdge,
    PlanningInput,
    plan_compaction,
)


def request_id(value: int) -> UUID:
    return UUID(int=value)


def planning_input(
    request_values: tuple[int, ...],
    edges: tuple[tuple[int, int, float], ...],
    *,
    max_group_requests: int = 10,
) -> PlanningInput:
    return PlanningInput(
        planning_batch_id=request_id(10_000),
        planning_batch_attempt_id=request_id(10_001),
        request_ids=tuple(request_id(value) for value in request_values),
        candidate_edges=tuple(
            PlanningCandidateEdge(request_id(left), request_id(right), distance)
            for left, right, distance in edges
        ),
        compaction_distance_m=500,
        max_group_requests=max_group_requests,
        algorithm_version=BOUNDED_GREEDY_DIAMETER_V1,
    )


def logical_partition(
    planned: Sequence[PlannedGroup],
) -> tuple[tuple[str, tuple[int, ...]], ...]:
    return tuple(
        (group.planning_mode, tuple(item.int for item in group.request_ids)) for group in planned
    )


def test_one_request_becomes_normal_singleton() -> None:
    groups = plan_compaction(planning_input((1,), ()))

    assert logical_partition(groups) == ((NORMAL_SINGLETON, (1,)),)


def test_compatible_pair_becomes_compacted_group() -> None:
    groups = plan_compaction(planning_input((2, 1), ((1, 2, 120.0),)))

    assert logical_partition(groups) == ((COMPACTED, (1, 2)),)


def test_distant_pair_becomes_two_singletons() -> None:
    groups = plan_compaction(planning_input((2, 1), ()))

    assert logical_partition(groups) == (
        (NORMAL_SINGLETON, (1,)),
        (NORMAL_SINGLETON, (2,)),
    )


def test_mixed_population_produces_compacted_and_singleton_groups() -> None:
    groups = plan_compaction(planning_input((5, 3, 1, 2, 4), ((1, 2, 30.0), (3, 4, 40.0))))

    assert logical_partition(groups) == (
        (COMPACTED, (1, 2)),
        (COMPACTED, (3, 4)),
        (NORMAL_SINGLETON, (5,)),
    )


def test_transitive_chaining_cannot_form_one_group() -> None:
    groups = plan_compaction(
        planning_input(
            (1, 2, 3),
            (
                (1, 2, 100.0),
                (2, 3, 100.0),
            ),
        )
    )

    assert logical_partition(groups) == (
        (COMPACTED, (1, 2)),
        (NORMAL_SINGLETON, (3,)),
    )


def test_candidate_near_seed_but_incompatible_with_member_is_rejected() -> None:
    groups = plan_compaction(
        planning_input(
            (1, 2, 3),
            (
                (1, 2, 50.0),
                (1, 3, 60.0),
            ),
        )
    )

    assert logical_partition(groups) == (
        (COMPACTED, (1, 2)),
        (NORMAL_SINGLETON, (3,)),
    )


def test_dense_population_respects_group_request_limit() -> None:
    values = (1, 2, 3, 4, 5)
    edges = tuple(
        (left, right, float(left + right)) for left in values for right in values if left < right
    )

    groups = plan_compaction(planning_input(values, edges, max_group_requests=2))

    assert logical_partition(groups) == (
        (COMPACTED, (1, 2)),
        (COMPACTED, (3, 4)),
        (NORMAL_SINGLETON, (5,)),
    )
    assert all(len(group.request_ids) <= 2 for group in groups)


def test_input_order_does_not_change_logical_partition() -> None:
    values = [1, 2, 3, 4, 5]
    edges = [
        (1, 2, 10.0),
        (1, 3, 20.0),
        (2, 3, 25.0),
        (4, 5, 30.0),
    ]
    expected = logical_partition(
        plan_compaction(planning_input(tuple(values), tuple(edges), max_group_requests=3))
    )

    randomizer = random.Random(20260927)
    for _ in range(20):
        randomizer.shuffle(values)
        randomizer.shuffle(edges)
        actual = logical_partition(
            plan_compaction(planning_input(tuple(values), tuple(edges), max_group_requests=3))
        )
        assert actual == expected


def test_equal_seed_distance_uses_request_id_tie_breaker() -> None:
    groups = plan_compaction(
        planning_input(
            (3, 1, 2),
            ((1, 3, 100.0), (1, 2, 100.0), (2, 3, 100.0)),
            max_group_requests=2,
        )
    )

    assert logical_partition(groups) == (
        (COMPACTED, (1, 2)),
        (NORMAL_SINGLETON, (3,)),
    )
