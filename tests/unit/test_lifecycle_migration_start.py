"""Durable start claims arbitrate hosts, crashes, and ambiguous Azure acceptance."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier, Lock
from unittest.mock import Mock

import pytest
from azure.core.exceptions import AzureError, ResourceExistsError
from azure.core.pipeline.transport import RequestsTransport, RequestsTransportResponse
from lifecycle_fakes import auth
from requests import Response
from test_lifecycle_migration_execution import IMAGE, JOB, completed
from test_lifecycle_migration_execution import migration as migration

from tirodhan.deployment import foundation_reconcile, migration_start, prerequisites, release
from tirodhan.deployment.deployment_admission import AdmissionDenied
from tirodhan.deployment.lifecycle_checks import Checks
from tirodhan.deployment.lifecycle_evidence import ObservationError


@pytest.fixture
def start(migration, monkeypatch):
    io, job, executions, _ = migration
    checks = Checks(auth("migration-start"), io.config, {}, io)
    lock, claims, starts = Lock(), {}, []

    class Claims:
        def __init__(self, _):
            pass

        def acquire(self, key, owner):
            with lock:
                if key in claims:
                    return False
                claims[key] = owner
                return True

        def close(self):
            pass

    monkeypatch.setattr(migration_start, "ClaimStore", Claims)
    monkeypatch.setattr(
        foundation_reconcile, "admitted_with_grant", lambda _: (checks, io, object())
    )
    monkeypatch.setattr(prerequisites, "revalidate_database", lambda _: None)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)

    def accepted(*args):
        assert args[:5] == ("az", "rest", "--method", "post", "--url")
        assert args[5] == "https://management.azure.com" + JOB + "/start?api-version=2025-07-01"
        assert args[6:] == ("-o", "json")  # No template/image/command/volume override.
        with lock:
            starts.append("migration-exact-execution")
            value = completed(job, starts[-1])
            executions[JOB + "/executions/" + starts[-1]] = value
        return {"name": starts[-1]}

    io.runner.json = accepted
    return io, claims, starts, accepted


def test_concurrent_hosts_start_exactly_one_execution_and_replay(start, monkeypatch):
    io, claims, starts, accepted = start
    barrier = Barrier(2)
    original = migration_start.ClaimStore.acquire

    def race(self, key, owner):
        barrier.wait(timeout=5)
        return original(self, key, owner)

    monkeypatch.setattr(migration_start.ClaimStore, "acquire", race)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: release.run_migration(), range(2)))
    monkeypatch.setattr(migration_start.ClaimStore, "acquire", original)
    release.run_migration()
    assert len(claims) == len(starts) == 1


def test_lost_start_response_reconciles_exact_execution_without_second_start(start):
    io, claims, starts, accepted = start

    def response_lost(*args):
        accepted(*args)
        raise ObservationError("OBSERVATION_UNAVAILABLE")

    io.runner.json = response_lost
    release.run_migration()
    release.run_migration()
    assert len(claims) == len(starts) == 1


def test_crash_after_claim_before_start_never_guesses_absence_or_restarts(start, monkeypatch):
    io, claims, starts, accepted = start
    key = migration_start.operation_key(auth("migration-start"), JOB, IMAGE)
    claims[key] = "prior-process-crashed"
    clock = iter([0, 0, 1201])
    monkeypatch.setattr(release.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(release.time, "sleep", lambda _: None)
    with pytest.raises(release.ReleaseBlocked, match="timed out"):
        release.run_migration()
    assert starts == []


def test_claim_scope_survives_authorization_renewal_but_separates_releases():
    authorization = auth("migration-start")
    key = migration_start.operation_key(authorization, JOB, IMAGE)
    assert (
        migration_start.operation_key(replace(authorization, claims={"jti": "new"}), JOB, IMAGE)
        == key
    )
    assert (
        migration_start.operation_key(replace(authorization, run_id="new-release"), JOB, IMAGE)
        != key
    )


@pytest.mark.parametrize("lost_response", [False, True])
def test_blob_claim_conditional_create_and_lost_upload_response(monkeypatch, lost_response):
    documents, arguments = {}, []

    class Blob:
        def __init__(self, **kwargs):
            arguments.append(kwargs)
            self.name = kwargs["blob_name"]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def upload_blob(self, data, overwrite):
            assert overwrite is False
            if self.name in documents:
                raise ResourceExistsError()
            documents[self.name] = data
            if lost_response:
                raise AzureError("lost upload response")

        def download_blob(self, **kwargs):
            assert kwargs == {"offset": 0, "length": 4097, "max_concurrency": 1}
            return Mock(readall=lambda: documents[self.name])

    monkeypatch.setattr(migration_start, "BlobClient", Blob)
    store = object.__new__(migration_start.ClaimStore)
    store.account, store.credential = "approvedstate", object()
    assert store.acquire("a" * 64, "b" * 32)
    assert not store.acquire("a" * 64, "c" * 32)
    assert len(documents) == 1
    assert arguments[0]["container_name"] == "tfstate"
    assert arguments[0]["blob_name"] == "tirodhan/migration-starts/" + "a" * 64 + ".json"
    assert arguments[0]["retry_total"] == 0


def test_duplicate_correlated_executions_are_ambiguous_and_block(migration):
    io, job, executions, _ = migration
    for name in ("migration-one", "migration-two"):
        executions[JOB + "/executions/" + name] = completed(job, name)
    key = migration_start.operation_key(auth("migration-start"), JOB, IMAGE)
    with pytest.raises(AdmissionDenied, match="MULTIPLE_EXECUTIONS"):
        migration_start.reconcile(io, JOB, key)


@pytest.mark.parametrize("secret_ref", [None, "", "another-secret"])
def test_literal_marker_accepts_optional_null_fields_but_never_secret_substitution(
    migration, secret_ref
):
    _, job, _, _ = migration
    value = completed(job, "migration-one")
    key = migration_start.operation_key(auth("migration-start"), JOB, IMAGE)
    value["properties"]["template"]["containers"][0]["env"][0]["secretRef"] = secret_ref
    assert migration_start.belongs_to_release(value, key) is (secret_ref in (None, ""))


def test_real_blob_sdk_sends_atomic_create_condition_to_separate_claim_blob(monkeypatch):
    requests = []

    def send(self, request, **kwargs):
        requests.append(request)
        response = Response()
        response.status_code = 201
        response.headers.update(
            {"ETag": '"claim-etag"', "Last-Modified": "Sun, 01 Nov 2026 17:00:00 GMT"}
        )
        response._content = b""
        return RequestsTransportResponse(request, response)

    monkeypatch.setattr(RequestsTransport, "send", send)
    store = object.__new__(migration_start.ClaimStore)
    store.account, store.credential = "approvedstate", None
    assert store.acquire("a" * 64, "b" * 32)
    assert len(requests) == 1
    assert requests[0].headers["If-None-Match"] == "*"
    assert requests[0].method == "PUT"
    assert (
        requests[0].url
        == "https://approvedstate.blob.core.windows.net/tfstate/tirodhan/migration-starts/"
        + "a" * 64
        + ".json"
    )


@pytest.mark.parametrize(
    "data", [b"not-json", b"{}", b"x" * 4097], ids=["malformed", "missing", "oversized"]
)
def test_unknown_or_malformed_claim_cannot_authorize_start(monkeypatch, data):
    blob = Mock()
    blob.upload_blob.side_effect = ResourceExistsError()
    blob.download_blob.return_value.readall.return_value = data
    context = Mock()
    context.__enter__ = Mock(return_value=blob)
    context.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(migration_start, "BlobClient", lambda **kwargs: context)
    store = object.__new__(migration_start.ClaimStore)
    store.account, store.credential = "approvedstate", None
    with pytest.raises(ObservationError, match="CLAIM_UNVERIFIED"):
        store.acquire("a" * 64, "b" * 32)


def test_lost_response_without_execution_stays_blocked_and_never_reposts(
    start, monkeypatch, tmp_path
):
    io, claims, starts, _ = start
    io.runner.json = Mock(side_effect=ObservationError("OBSERVATION_UNAVAILABLE"))
    output = tmp_path / "job-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr(release.time, "sleep", lambda _: None)
    for _ in range(2):
        clock = iter([0, 0, 1201])
        monkeypatch.setattr(release.time, "monotonic", lambda clock=clock: next(clock))
        with pytest.raises(release.ReleaseBlocked, match="timed out"):
            release.run_migration()
    assert io.runner.json.call_count == 1
    assert len(claims) == 1 and not output.exists()
