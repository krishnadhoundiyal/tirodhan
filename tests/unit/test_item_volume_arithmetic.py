"""Occupied volume is an independent declaration, never a weight or count conversion."""

from dataclasses import replace

import pytest
from pydantic import ValidationError

from tirodhan.modules.business_admin.weight_contracts import MAX_VOLUME_ML, WeightItemInput
from tirodhan.modules.business_admin.weight_pricing import (
    WeightPricingConflict,
    declaration_fingerprint,
    declared_volume,
)
from tirodhan.modules.collection_requests.ports import DeclaredRequestItem
from tirodhan.modules.customers.service import command_fingerprint


def test_volume_sums_whole_lines_without_quantity_or_weight_conversion():
    items = [
        DeclaredRequestItem("A", 99, 8000, declared_volume_ml=1000),
        DeclaredRequestItem("B", 3, 1, declared_volume_ml=2500),
    ]
    assert declared_volume(items) == 3500
    assert declared_volume([replace(i, declared_weight_grams=999999) for i in items]) == 3500
    assert declared_volume([replace(items[0], declared_volume_ml=MAX_VOLUME_ML)]) == MAX_VOLUME_ML
    with pytest.raises(WeightPricingConflict, match="AGGREGATE_VOLUME_OUT_OF_RANGE"):
        declared_volume([replace(items[0], declared_volume_ml=MAX_VOLUME_ML), items[1]])


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "1000", MAX_VOLUME_ML + 1])
def test_invalid_volume_rejected_at_transport_and_domain_boundaries(value):
    with pytest.raises(ValidationError):
        WeightItemInput(item_category_code="A", declared_volume_ml=value)
    with pytest.raises(WeightPricingConflict, match="ITEM_VOLUME_OUT_OF_RANGE"):
        declared_volume([DeclaredRequestItem("A", declared_volume_ml=value)])


def test_missing_volume_is_unknown_and_never_zero():
    assert declared_volume([]) is None
    assert declared_volume([DeclaredRequestItem("A")]) is None
    assert (
        declared_volume([DeclaredRequestItem("A", declared_volume_ml=1), DeclaredRequestItem("B")])
        is None
    )


def test_volume_binds_declarations_without_changing_legacy_fingerprints():
    item = DeclaredRequestItem("A", 2, 4000, "BELOW_B")
    old = command_fingerprint(
        {
            "items": [
                dict(
                    item_category_code="A",
                    declared_quantity=2,
                    declared_weight_grams=4000,
                    weight_band="BELOW_B",
                    weight_kg=None,
                )
            ]
        }
    ).hex()
    assert declaration_fingerprint([item]) == old
    assert repr(item) == repr(replace(item, declared_volume_ml=1000))
    assert declaration_fingerprint([replace(item, declared_volume_ml=1000)]) != old
