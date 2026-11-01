"""Invalid or unavailable SQL proof denies effects and never exposes raw failures."""

from unittest.mock import Mock

import jwt
import pytest
from lifecycle_fakes import MemoryObservations, auth

from tirodhan.deployment import foundation_lifecycle, prerequisites
from tirodhan.deployment.deployment_admission import AdmissionDenied
from tirodhan.deployment.lifecycle_checks import Checks
from tirodhan.deployment.prerequisites import Grant


def context(monkeypatch, operation):
    io = MemoryObservations()
    authorization = auth(operation)
    checks = Checks(authorization, io.config, {}, io)
    grant = Grant(authorization, operation, io.current.fingerprint, ("database",), {})
    monkeypatch.setattr(prerequisites, "require_release_authorization", lambda _: authorization)
    monkeypatch.setattr(prerequisites, "configuration", lambda: io.config)
    return io, checks, grant


@pytest.mark.parametrize(
    "error", [jwt.ExpiredSignatureError, jwt.InvalidSignatureError, KeyError, ValueError]
)
def test_database_observation_failures_are_controlled_denials(monkeypatch, error):
    _, _, grant = context(monkeypatch, "migration-apply")
    monkeypatch.setattr(
        Checks, "check_database", Mock(side_effect=error("private-provider-detail"))
    )
    with pytest.raises(AdmissionDenied, match="^DATABASE_EVIDENCE_NO_LONGER_CURRENT$"):
        prerequisites.revalidate_database(grant)


@pytest.mark.parametrize("result", [False, None, 1, "true"])
def test_revalidation_accepts_only_boolean_true(monkeypatch, result):
    _, _, grant = context(monkeypatch, "runtime-apply")
    monkeypatch.setattr(Checks, "check_database", lambda _: result)
    with pytest.raises(AdmissionDenied, match="^DATABASE_EVIDENCE_NO_LONGER_CURRENT$"):
        prerequisites.revalidate_database(grant)


@pytest.mark.parametrize("operation", ["migration-apply", "runtime-apply"])
@pytest.mark.parametrize("error", [jwt.ExpiredSignatureError, KeyError])
def test_invalid_database_evidence_stops_cli_before_native_effects(
    monkeypatch, tmp_path, capsys, operation, error
):
    io, checks, grant = context(monkeypatch, operation)
    monkeypatch.setattr(
        foundation_lifecycle, "admitted_with_grant", lambda *a, **k: (checks, io, grant)
    )
    monkeypatch.setattr(
        Checks, "check_database", Mock(side_effect=error("private-provider-detail"))
    )
    native, prepare, compensate = Mock(), Mock(), Mock()
    monkeypatch.setattr(io.runner, "run", native)
    monkeypatch.setattr(foundation_lifecycle, "prepare_compensation", prepare)
    monkeypatch.setattr(foundation_lifecycle, "compensate", compensate)
    monkeypatch.setattr(
        "sys.argv", ["lifecycle", operation, "--plan", str(tmp_path / "reviewed.tfplan")]
    )
    with pytest.raises(
        SystemExit, match="^LIFECYCLE_BLOCKED: deployment or cleanup is not verified$"
    ):
        foundation_lifecycle.main()
    native.assert_not_called()
    prepare.assert_not_called()
    compensate.assert_not_called()
    output = capsys.readouterr()
    assert "private-provider-detail" not in output.out + output.err
