from datetime import datetime, timezone

import pytest

from tirodhan.modules.planning.policy import (
    PlanningConfigurationError,
    planning_cutoff_reached,
    planning_cutoff_time,
    require_compaction_distance_m,
    require_max_group_requests,
    require_planning_max_attempts,
)


def test_planning_cutoff_uses_timezone_aware_utc_instants() -> None:
    slot_start = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)
    cutoff = planning_cutoff_time(slot_start, 30)

    assert cutoff == datetime(2026, 10, 1, 9, 30, tzinfo=timezone.utc)
    assert planning_cutoff_reached(slot_start, 30, now=cutoff)


def test_planning_policy_fails_when_configuration_is_missing() -> None:
    slot_start = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)
    with pytest.raises(PlanningConfigurationError):
        planning_cutoff_time(slot_start, None)
    with pytest.raises(PlanningConfigurationError):
        require_planning_max_attempts(None)
    with pytest.raises(PlanningConfigurationError):
        require_compaction_distance_m(None)
    with pytest.raises(PlanningConfigurationError):
        require_max_group_requests(None)
