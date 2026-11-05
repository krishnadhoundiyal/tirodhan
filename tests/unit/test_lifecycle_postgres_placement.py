"""Catalog placement is SKU-specific and never promises successful allocation."""

import copy

import pytest
from lifecycle_fakes import MemoryObservations, auth

from tirodhan.deployment import lifecycle_checks
from tirodhan.deployment.lifecycle_checks import Checks


def catalog(skus):
    """Represent the actual CLI capability hierarchy without account-specific data."""
    return [
        {
            "restricted": "Disabled",
            "supportedServerEditions": [{"name": "Burstable", "supportedServerSkus": skus}],
        }
    ]


@pytest.mark.parametrize(
    ("skus", "expected"),
    [
        ([{"name": "Standard_B1ms", "supportedZones": ["1", "2", "3"]}], True),
        ([{"name": "Standard_B1ms", "supportedZones": ["1", "3"]}], False),
        ([{"name": "Standard_B2s", "supportedZones": ["2"]}], False),
        (
            [
                {"name": "Standard_B1ms", "supportedZones": ["1"]},
                {"name": "Standard_B2s", "supportedZones": ["2"]},
            ],
            False,
        ),
        ([{"name": "Standard_B1ms", "supportedZones": "123"}], False),
        ([{"name": "Standard_B1ms", "supportedZones": [2]}], False),
        ([{"name": "Standard_B1ms"}], False),
        ([{"name": "Standard_B1ms", "supportedZones": ["2"], "status": "Disabled"}], False),
        ([{"name": "Standard_B1ms", "supportedZones": ["2"], "status": "Unknown"}], False),
        ([{"name": "Standard_B1ms", "supportedZones": ["2"], "status": []}], False),
        ([], False),
    ],
)
def test_admission_requires_selected_zone_on_exact_sku(monkeypatch, skus, expected):
    """Reject another SKU's zones, malformed support and unavailable placements."""
    io = MemoryObservations()
    commands = []

    def observe(*args):
        """Return catalog evidence while recording the exact read-only query."""
        commands.append(args)
        return copy.deepcopy(catalog(skus))

    monkeypatch.setattr(io.runner, "json", observe)
    assert Checks(auth("foundation-plan"), io.config, {}, io).check_postgres_sku() is expected
    assert commands == [
        (
            "az",
            "postgres",
            "flexible-server",
            "list-skus",
            "--location",
            "centralindia",
            "--subscription",
            io.tf["subscription_id"],
            "--output",
            "json",
        )
    ]


def test_restricted_capability_branch_cannot_admit_placement(monkeypatch):
    """An advertised name/zone does not override an explicit subscription restriction."""
    io = MemoryObservations()
    response = catalog([{"name": "Standard_B1ms", "supportedZones": ["2"]}])
    response[0]["restricted"] = "Enabled"
    monkeypatch.setattr(io.runner, "json", lambda *args: response)
    assert not Checks(auth("foundation-plan"), io.config, {}, io).check_postgres_sku()


@pytest.mark.parametrize(("advertised_zone", "expected"), [("3", True), ("2", False)])
def test_zone_authority_is_committed_terraform(monkeypatch, tmp_path, advertised_zone, expected):
    """Bind the check to source-selected placement instead of a hard-coded retry zone."""
    directory = tmp_path / "infra/terraform/nonprod"
    directory.mkdir(parents=True)
    (directory / "database.tf").write_text('sku_name = "B_Standard_B1ms"\nzone = "3"\n')
    monkeypatch.setattr(lifecycle_checks, "ROOT", tmp_path)
    io = MemoryObservations()
    response = catalog([{"name": "Standard_B1ms", "supportedZones": [advertised_zone]}])
    monkeypatch.setattr(io.runner, "json", lambda *args: response)
    assert Checks(auth("foundation-plan"), io.config, {}, io).check_postgres_sku() is expected


@pytest.mark.parametrize("placement", ["", "zone = var.unverified_zone", 'zone = "2"\nzone = "3"'])
def test_missing_or_ambiguous_zone_blocks_before_provider_read(monkeypatch, tmp_path, placement):
    """Omitted or unimplemented initial-placement authority cannot satisfy the gate."""
    directory = tmp_path / "infra/terraform/nonprod"
    directory.mkdir(parents=True)
    (directory / "database.tf").write_text('sku_name = "B_Standard_B1ms"\n' + placement)
    monkeypatch.setattr(lifecycle_checks, "ROOT", tmp_path)
    io = MemoryObservations()
    assert not Checks(auth("foundation-plan"), io.config, {}, io).check_postgres_sku()
    assert not io.runner.commands
