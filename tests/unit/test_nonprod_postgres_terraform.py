from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATABASE_TF = ROOT / "infra/terraform/nonprod/database.tf"


def test_nonprod_postgres_ignores_azure_assigned_zone_drift() -> None:
    terraform = DATABASE_TF.read_text(encoding="utf-8")

    assert 'sku_name                      = "B_Standard_B1ms"' in terraform
    assert "high_availability" not in terraform
    assert "lifecycle {" in terraform
    assert "ignore_changes = [zone]" in terraform
