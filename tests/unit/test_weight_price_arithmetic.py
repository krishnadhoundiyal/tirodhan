"""Synthetic policies exercise exact arithmetic and declarations, not production rates."""

from dataclasses import replace
from fractions import Fraction

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from tirodhan.modules.business_admin.weight_contracts import (
    MAX_GRAMS,
    WeightItemInput,
    WeightPolicyFields,
    kilograms_to_grams,
)
from tirodhan.modules.business_admin.weight_pricing import (
    WeightPricingConflict,
    normalize_items,
    price_minor,
    unrounded_price,
)
from tirodhan.modules.collection_requests.ports import DeclaredRequestItem


def policy(**changes):
    return WeightPolicyFields(
        **dict(
            first_band_upper_grams=4000,
            numerical_threshold_grams=12000,
            first_band_label="Below configured B",
            middle_band_label="B through T",
            open_band_label="Above configured T",
            display_unit="kg",
            minimum_amount_minor=1000,
            maximum_amount_minor=5000,
            included_weight_grams=2000,
            saturation_weight_grams=30000,
            curvature="2.5",
            currency="INR",
            active=True,
        )
        | changes
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"first_band_upper_grams": 0},
        {"numerical_threshold_grams": 4000},
        {"numerical_threshold_grams": 3999},
        {"maximum_amount_minor": 999},
        {"saturation_weight_grams": 2000},
        {"curvature": "0"},
        {"curvature": 1.5},
        {"curvature": "1.0000001"},
        {"curvature": "NaN"},
        {"currency": "USD"},
        {"display_unit": "lb"},
        {"first_band_label": ""},
        {"first_band_upper_grams": True},
        {"minimum_amount_minor": 99},
    ],
)
def test_invalid_configuration(changes):
    with pytest.raises(ValidationError):
        policy(**changes)


@pytest.mark.parametrize(
    "value",
    ["0", "-1", "+1", "1e3", "1,2", " 1", "1.", ".1", "NaN", "Infinity", "1.0001", "2147483.648"],
)
def test_malformed_precision_and_overflow(value):
    with pytest.raises(ValueError):
        kilograms_to_grams(value)


@pytest.mark.parametrize(
    "value,grams", [("0.001", 1), ("12.001", 12001), ("2147483.647", MAX_GRAMS), ("001.200", 1200)]
)
def test_exact_kilogram_conversion(value, grams):
    assert kilograms_to_grams(value) == grams


def test_numeric_json_float_and_unknown_band_are_rejected():
    with pytest.raises(ValidationError):
        WeightItemInput(item_category_code="A", weight_kg=12.5)
    with pytest.raises(ValidationError):
        WeightItemInput(item_category_code="A", weight_band="UNKNOWN")


def test_finite_bands_quantities_and_single_aggregation():
    items, total = normalize_items(
        (
            DeclaredRequestItem("A", 99, weight_band="BELOW_B"),
            DeclaredRequestItem("B", 3, weight_band="B_TO_T"),
            DeclaredRequestItem("A", 2, weight_band="ABOVE_T", weight_kg="12.001"),
        ),
        policy(),
    )
    assert [i.declared_weight_grams for i in items] == [4000, 12000, 12001]
    assert total == 28001  # Never multiplied by quantities 99, 3 or 2.
    assert normalize_items(items, policy()) == (items, total)


@pytest.mark.parametrize(
    "item",
    [
        DeclaredRequestItem("A"),
        DeclaredRequestItem("A", weight_band="BELOW_B", weight_kg="1"),
        DeclaredRequestItem("A", weight_band="ABOVE_T"),
        DeclaredRequestItem("A", weight_band="ABOVE_T", weight_kg="12"),
        DeclaredRequestItem("A", weight_band="ABOVE_T", weight_kg="11.999"),
        DeclaredRequestItem("A", declared_weight_grams=1, weight_band="B_TO_T"),
    ],
)
def test_missing_and_conflicting_declarations(item):
    with pytest.raises(WeightPricingConflict):
        normalize_items([item], policy())


def test_aggregate_overflow_and_missing_policy_parameters():
    item = DeclaredRequestItem("A", weight_band="ABOVE_T", weight_kg="2147483.647")
    with pytest.raises(WeightPricingConflict, match="AGGREGATE"):
        normalize_items([item, replace(item, weight_band="BELOW_B", weight_kg=None)], policy())
    values = policy().model_dump()
    del values["numerical_threshold_grams"]
    with pytest.raises(ValidationError):
        WeightPolicyFields(**values)


def test_boundaries_and_half_up_minor_unit_rounding():
    p = policy()
    assert price_minor(0, p) == price_minor(2000, p) == 1000
    assert 1000 < price_minor(9000, p) < 5000
    assert price_minor(30000, p) == price_minor(MAX_GRAMS, p) == 5000
    half = policy(
        minimum_amount_minor=100,
        maximum_amount_minor=101,
        included_weight_grams=0,
        saturation_weight_grams=3,
        curvature="1",
    )
    assert unrounded_price(1, half) == Fraction(201, 2)
    assert price_minor(1, half) == 101
    assert price_minor(999, policy(maximum_amount_minor=1000)) == 1000


@given(st.integers(0, MAX_GRAMS), st.integers(0, MAX_GRAMS))
def test_bounded_monotonic_rounded_money(a, b):
    x, y = sorted((a, b))
    assert 1000 <= price_minor(x, policy()) <= price_minor(y, policy()) <= 5000


@given(st.integers(1, 999999), st.integers(0, 100), st.integers(1, 100))
def test_diminishing_rate_within_variable_interval(curve, index, step):
    p = policy(curvature=str(curve))
    span = p.saturation_weight_grams - p.included_weight_grams
    width = max(1, span // (index + 2 * step + 1))
    x = p.included_weight_grams + index * width
    y, z = x + step * width, x + 2 * step * width
    assert z <= p.saturation_weight_grams
    assert unrounded_price(y, p) - unrounded_price(x, p) >= (
        unrounded_price(z, p) - unrounded_price(y, p)
    )


def test_legacy_item_repr_for_committed_replay_remains_unchanged():
    assert repr(DeclaredRequestItem("A", 2, 100)) == (
        "DeclaredRequestItem(item_category_code='A', declared_quantity=2, "
        "declared_weight_grams=100)"
    )
