from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


class _ScalarResult:
    def __init__(self, value: bool) -> None:
        self._value = value

    def scalar_one(self) -> bool:
        return self._value


class _Connection:
    def __init__(self, installed: bool) -> None:
        self.installed = installed
        self.statements: list[str] = []

    def execute(self, statement: object) -> _ScalarResult:
        self.statements.append(str(statement))
        return _ScalarResult(self.installed)


def _load_migration() -> ModuleType:
    path = Path("migrations/versions/0001_enable_postgis.py")
    spec = importlib.util.spec_from_file_location("migration_0001_enable_postgis", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _unexpected_extension_ddl(*args: Any, **kwargs: Any) -> None:
    pytest.fail("PostGIS extension DDL must not be issued by the application migration")


def test_existing_postgis_skips_privileged_extension_ddl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    connection = _Connection(installed=True)

    monkeypatch.setattr(migration.op, "get_bind", lambda: connection)
    monkeypatch.setattr(migration.op, "execute", _unexpected_extension_ddl)

    migration.upgrade()

    assert len(connection.statements) == 1
    assert "pg_extension" in connection.statements[0]
    assert "postgis" in connection.statements[0]


def test_missing_postgis_requires_platform_bootstrap(monkeypatch: pytest.MonkeyPatch) -> None:
    migration = _load_migration()
    connection = _Connection(installed=False)

    monkeypatch.setattr(migration.op, "get_bind", lambda: connection)
    monkeypatch.setattr(migration.op, "execute", _unexpected_extension_ddl)

    with pytest.raises(RuntimeError, match="PostGIS must be provisioned before Alembic migrations"):
        migration.upgrade()


def test_downgrade_does_not_drop_platform_owned_postgis(monkeypatch: pytest.MonkeyPatch) -> None:
    migration = _load_migration()
    monkeypatch.setattr(migration.op, "execute", _unexpected_extension_ddl)

    migration.downgrade()
