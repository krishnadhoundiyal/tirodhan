"""Replacement binding does not relax the common gate's hard operational constraints."""

from dataclasses import replace
from importlib.metadata import PackageNotFoundError
from types import SimpleNamespace

import pytest
from test_routing_validation import problem as _problem_fixture
from test_routing_validation import proposal, uid

from tirodhan.modules.planning.routing.contracts import OptimizerEngine, TimeWindow
from tirodhan.modules.planning.routing.execution import RoutingAdapterError
from tirodhan.modules.planning.routing.validation import RoutingValidationError, validate_proposal
from tirodhan.modules.routing_operations.economics import EconomicPolicy
from tirodhan.modules.routing_operations.replacement_options import coordinate, pin_existing


@pytest.fixture
def problem():
    return _problem_fixture.__wrapped__()


def test_existing_transferred_cargo_is_bound_to_current_custodian(problem):
    bound = pin_existing(
        problem,
        {
            "vehicles": [
                {"rider_id": uid(31), "vehicle_id": uid(30)},
                {"rider_id": uid(900), "vehicle_id": uid(901)},
            ],
            "requests": [
                {
                    "request_id": uid(40),
                    "owner_id": uid(900),
                    "collector_id": uid(31),
                    "pickup_status": "COLLECTED",
                },
                {"request_id": uid(42), "owner_id": None},
            ],
        },
        {uid(42)},
        uid(30),
    )
    assert all(s.bound_vehicle_id == uid(30) for s in bound.stops)
    validate_proposal(bound, proposal(bound))


@pytest.mark.parametrize("engine", tuple(OptimizerEngine))
@pytest.mark.parametrize(
    "violation", ["load", "shift_end", "customer_window", "facility", "protected_prefix"]
)
def test_replacement_keeps_hard_constraints(problem, engine, violation):
    bound = pin_existing(
        problem,
        {
            "vehicles": [{"rider_id": uid(31), "vehicle_id": uid(30)}],
            "requests": [
                {"request_id": uid(40), "owner_id": uid(31)},
                {"request_id": uid(42), "owner_id": None},
            ],
        },
        {uid(42)},
        uid(30),
    )
    candidate = replace(proposal(bound), engine=engine)
    validate_proposal(bound, candidate)
    assert all(s.required for s in bound.stops)
    assert all(s.bound_vehicle_id == uid(30) for s in bound.stops)
    vehicle = bound.vehicles[0]
    if violation == "load":
        bound = replace(bound, vehicles=(replace(vehicle, capacity=(1, 1)),))
    elif violation == "shift_end":
        bound = replace(bound, vehicles=(replace(vehicle, shift=TimeWindow(0, 60)),))
    elif violation == "customer_window":
        bound = replace(
            bound,
            stops=tuple(
                replace(s, service_start_window=TimeWindow(0, 10)) if s.stop_id == uid(51) else s
                for s in bound.stops
            ),
        )
    elif violation == "facility":
        bound = replace(
            bound,
            receiving_points=tuple(replace(p, available=False) for p in bound.receiving_points),
        )
    else:
        bound = replace(bound, vehicles=(replace(vehicle, protected_prefix=(uid(52),)),))
    with pytest.raises(RoutingValidationError):
        validate_proposal(bound, candidate)


@pytest.mark.parametrize("failure", [ModuleNotFoundError, PackageNotFoundError])
def test_missing_coordinator_never_certifies_a_replacement(problem, monkeypatch, failure):
    def unavailable(*args, **kwargs):
        raise failure("ortools")

    monkeypatch.setattr(
        "tirodhan.modules.routing_operations.replacement_options.consolidate", unavailable
    )
    economics = EconomicPolicy(
        version="test",
        additional_vehicle_seconds=0,
        disruption_seconds=0,
        waiting_weight=0,
        window_risk_buffer_seconds=0,
        window_risk_weight=0,
        max_candidate_evaluations=10,
    )
    with pytest.raises(RoutingAdapterError, match="UNKNOWN_COORDINATOR_UNAVAILABLE"):
        coordinate(
            problem, proposal(problem), {"points": []}, SimpleNamespace(economics=economics), {}
        )
