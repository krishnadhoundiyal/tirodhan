"""Real HTTP request shapes and deletion histories; no Azure resources or credentials."""

from __future__ import annotations

import copy
import gzip
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import format_datetime
from io import StringIO
from threading import Barrier, Lock
from typing import Any

import httpx
import pytest

from tirodhan.deployment import destroy_aca_environment as module

SUB = "11111111-1111-4111-8111-111111111111"
RG = "tirodhan-np-synthetic"
ID = (
    f"/subscriptions/{SUB}/resourceGroups/{RG}"
    "/providers/Microsoft.App/managedEnvironments/custom-aca"
)
TARGET = module.Target(ID, SUB, RG)
POLL = f"{module.ARM}/subscriptions/{SUB}/providers/Microsoft.App/locations/test/operations/123"


def plan() -> dict[str, Any]:
    return {
        "resource_changes": [
            {
                "address": "azurerm_container_app_environment.nonprod",
                "mode": "managed",
                "type": "azurerm_container_app_environment",
                "change": {"actions": ["delete"], "before": {"id": ID}},
            }
        ]
    }


def approve(value: dict[str, Any]) -> module.Target | None:
    return module.approved_target(
        value,
        subscription=SUB,
        resource_group=RG,
        protected_state_account="synthetic-state",
        protected_state_container="tfstate",
    )


def present(state: str = "Succeeded", **changes: Any) -> httpx.Response:
    return httpx.Response(
        200, json={"id": ID, "properties": {"provisioningState": state}, **changes}
    )


def absent(code: str = "ResourceNotFound") -> httpx.Response:
    return httpx.Response(404, json={"error": {"code": code}})


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.delays: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, delay: float) -> None:
        assert delay > 0
        self.delays.append(delay)
        self.now += delay


def execute(
    responses: list[Any],
    *,
    policy: module.Policy | None = None,
) -> tuple[int, list[httpx.Request], Clock]:
    calls: list[httpx.Request] = []
    clock = Clock()
    remaining = iter(responses)

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["Authorization"] == "Bearer synthetic-secret"
        assert request.method in {"GET", "DELETE"}
        if request.method == "DELETE":
            assert str(request.url) == TARGET.url
        response = next(remaining)
        if isinstance(response, Exception):
            raise response
        return response

    with httpx.Client(transport=httpx.MockTransport(handle), trust_env=False) as client:
        result = module.Deletion(
            module.ArmClient(client, lambda: "synthetic-secret"),
            TARGET,
            policy or module.Policy(timeout_seconds=120, poll_seconds=1),
            clock=clock,
            sleep=clock.sleep,
            wall_clock=lambda: 0,
        ).run()
    with pytest.raises(StopIteration):
        next(remaining)
    return result, calls, clock


@pytest.mark.parametrize("delete_status", [200, 204])
def test_present_resource_is_deleted_and_confirmed_absent(delete_status: int) -> None:
    attempts, calls, _ = execute([present(), httpx.Response(delete_status), absent(), absent()])
    assert attempts == 1
    assert [r.method for r in calls] == ["GET", "DELETE", "GET", "GET"]


def test_compressed_arm_body_is_decoded_once_before_deletion() -> None:
    payload = gzip.compress(json.dumps(present().json()).encode())
    response = httpx.Response(
        200, headers={"Content-Encoding": "gzip"}, stream=httpx.ByteStream(payload)
    )
    attempts, _, _ = execute([response, httpx.Response(204), absent(), absent()])
    assert attempts == 1


@pytest.mark.parametrize("header", ["Location", "Azure-AsyncOperation"])
def test_async_delete_tracks_operation_without_content_type_then_verifies_resource(
    header: str,
) -> None:
    attempts, calls, _ = execute(
        [
            present(),
            httpx.Response(202, headers={header: POLL, "Retry-After": "2"}),
            present("ScheduledForDelete"),
            httpx.Response(202),
            present("ScheduledForDelete"),
            httpx.Response(200, content=b'{"status":"InProgress"}'),
            present("ScheduledForDelete"),
            httpx.Response(204, headers={"Content-Type": ""}),
            absent(),
            absent(),
        ]
    )
    assert attempts == 1
    assert sum(str(r.url) == POLL for r in calls) == 3


def test_empty_poll_success_does_not_imply_resource_absence() -> None:
    attempts, calls, _ = execute(
        [
            present(),
            httpx.Response(202, headers={"Location": POLL}),
            present("ScheduledForDelete"),
            httpx.Response(200, headers={"Content-Type": ""}),
            present("ScheduledForDelete"),
            absent(),
            absent(),
        ]
    )
    assert attempts == 1 and str(calls[-1].url) == TARGET.url


def test_exact_replay_of_completed_deletion_makes_no_delete_request() -> None:
    attempts, calls, _ = execute([absent(), absent()])
    assert attempts == 0 and all(r.method == "GET" for r in calls)


def test_resume_after_crash_reattaches_to_existing_delete_without_changing_target() -> None:
    attempts, calls, _ = execute(
        [
            present("ScheduledForDelete"),
            httpx.Response(202, headers={"Location": POLL}),
            present("ScheduledForDelete"),
            httpx.Response(200, json={"status": "Succeeded"}),
            absent(),
            absent(),
        ]
    )
    assert attempts == 1 and calls[1].method == "DELETE"


def test_ambiguous_send_rechecks_before_repeating_same_idempotent_delete() -> None:
    # Simulates a crash/lost response after Azure accepted DELETE.
    attempts, calls, _ = execute(
        [
            present(),
            httpx.ReadTimeout("private provider response"),
            present("ScheduledForDelete"),
            absent(),
            absent(),
        ]
    )
    assert attempts == 1 and [r.method for r in calls].count("DELETE") == 1


def test_delete_in_progress_conflict_is_not_treated_as_completed() -> None:
    attempts, _, _ = execute(
        [
            present(),
            httpx.Response(409, json={"error": {"code": "OperationInProgress"}}),
            present("Deleting"),
            absent(),
            absent(),
        ]
    )
    assert attempts == 1


def test_operation_404_is_not_resource_404() -> None:
    attempts, calls, _ = execute(
        [
            present(),
            httpx.Response(202, headers={"Location": POLL}),
            present("Deleting"),
            absent(),
            present("Deleting"),
            absent(),
            absent(),
        ]
    )
    assert attempts == 1 and len(calls) == 7


def test_one_transient_absence_cannot_finish_deletion() -> None:
    attempts, _, _ = execute(
        [
            absent(),
            present(),
            httpx.Response(204),
            absent(),
            absent(),
        ]
    )
    assert attempts == 1


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(429, headers={"Retry-After": "7"}),
        httpx.Response(503),
        httpx.ConnectError("sensitive URL"),
    ],
)
def test_transient_read_is_never_absence_or_permission_to_delete(response: Any) -> None:
    attempts, calls, clock = execute([response, absent(), absent()])
    assert attempts == 0 and all(r.method == "GET" for r in calls)
    if isinstance(response, httpx.Response) and response.status_code == 429:
        assert clock.delays[0] == 7


def test_http_date_retry_after_is_honoured() -> None:
    after = format_datetime(datetime.fromtimestamp(12, timezone.utc), usegmt=True)
    _, _, clock = execute([httpx.Response(429, headers={"Retry-After": after}), absent(), absent()])
    assert clock.delays[0] == 12


def test_transient_operation_poll_is_retried_without_another_delete() -> None:
    attempts, _, clock = execute(
        [
            present(),
            httpx.Response(202, headers={"Location": POLL}),
            present("Deleting"),
            httpx.Response(429, headers={"Retry-After": "8"}),
            present("Deleting"),
            httpx.Response(204),
            absent(),
            absent(),
        ]
    )
    assert attempts == 1 and 8 in clock.delays


@pytest.mark.parametrize(
    "reply",
    [
        httpx.Response(200, json={"unexpected": "body"}),
        httpx.Response(200, content=b"broken"),
        httpx.Response(403, json={"error": {"code": "AuthorizationFailed"}}),
    ],
)
def test_untrusted_operation_response_is_not_success(reply: httpx.Response) -> None:
    with pytest.raises(module.RecoveryError):
        execute([present(), httpx.Response(202, headers={"Location": POLL}), present(), reply])


def test_intermittent_not_found_is_reconfirmed_after_transient_error() -> None:
    attempts, calls, _ = execute([absent(), httpx.Response(503), absent(), absent()])
    assert attempts == 0 and len(calls) == 4


def test_reported_operation_success_cannot_hide_a_still_existing_resource() -> None:
    with pytest.raises(module.RecoveryError, match="DEADLINE_EXCEEDED"):
        execute(
            [
                present(),
                httpx.Response(202, headers={"Location": POLL}),
                present("Deleting"),
                httpx.Response(200, json={"status": "Succeeded"}),
            ],
            policy=module.Policy(2, 1, 3),
        )


@pytest.mark.parametrize("retry_after", ["-1", "not-a-date"])
def test_invalid_retry_after_is_not_a_fast_retry_loop(retry_after: str) -> None:
    with pytest.raises(module.RecoveryError, match="INVALID_RETRY_AFTER"):
        execute([httpx.Response(429, headers={"Retry-After": retry_after})])


def test_response_body_is_bounded() -> None:
    with pytest.raises(module.RecoveryError, match="ARM_RESPONSE_TOO_LARGE"):
        execute([httpx.Response(200, content=b"a" * 131073)])


@pytest.mark.parametrize(
    "policy", [(-1, 1, 3), (float("inf"), 1, 3), (10, 0, 3), (10, float("nan"), 3), (10, 1, 0)]
)
def test_invalid_policy_never_creates_a_client_or_request(policy: Any) -> None:
    with pytest.raises(module.RecoveryError, match="INVALID_RECOVERY_POLICY"):
        module.Policy(*policy)


def test_retryable_terminal_operation_failure_actually_resubmits_delete() -> None:
    deletes: list[str] = []
    clock = Clock()

    class Azure:
        def request(self, method: str, url: str, timeout: float) -> httpx.Response:
            if method == "DELETE":
                deletes.append(url)
                return (
                    httpx.Response(202, headers={"Location": POLL})
                    if len(deletes) == 1
                    else httpx.Response(204)
                )
            if url == POLL:
                return httpx.Response(
                    200, json={"status": "Failed", "error": {"code": "InternalServerError"}}
                )
            return absent() if len(deletes) == 2 else present("ScheduledForDelete")

    attempts = module.Deletion(
        Azure(), TARGET, module.Policy(120, 1, 3), clock=clock, sleep=clock.sleep
    ).run()
    assert attempts == 2 and deletes == [TARGET.url, TARGET.url]


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "InvalidAuthenticationToken"),
        (403, "AuthorizationFailed"),
        (409, "ManagedEnvironmentInUse"),
        (409, "ScopeLocked"),
        (400, "InvalidApiVersionParameter"),
    ],
)
def test_permanent_delete_blocker_fails_without_exposing_provider_message(
    status: int, code: str
) -> None:
    error = httpx.Response(status, json={"error": {"code": code, "message": "private-token"}})
    with pytest.raises(module.RecoveryError) as result:
        execute([present(), error])
    assert code in str(result.value) and "private-token" not in str(result.value)


@pytest.mark.parametrize(
    "reply",
    [absent("ResourceGroupNotFound"), absent("NotFound"), httpx.Response(404), httpx.Response(403)],
)
def test_unknown_404_or_denied_read_is_not_verified_absence(reply: httpx.Response) -> None:
    with pytest.raises(module.RecoveryError, match="ARM_HTTP"):
        execute([reply])


@pytest.mark.parametrize(
    "reply",
    [
        present(id=ID + "-other"),
        httpx.Response(200, content=b"bad"),
        httpx.Response(200, json={"id": ID}),
    ],
)
def test_malformed_or_wrong_resource_cannot_authorize_delete(reply: httpx.Response) -> None:
    with pytest.raises(module.RecoveryError):
        execute([reply])


@pytest.mark.parametrize("state", ["Failed", "Canceled", "Cancelled"])
def test_terminal_operation_blockers_do_not_turn_green(state: str) -> None:
    with pytest.raises(module.RecoveryError, match=f"ARM_OPERATION_{state}"):
        execute(
            [
                present(),
                httpx.Response(202, headers={"Location": POLL}),
                present(),
                httpx.Response(200, json={"status": state, "error": {"code": "ScopeLocked"}}),
            ]
        )


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/operation",
        "http://management.azure.com/operation",
        POLL.replace(SUB, "22222222-2222-4222-8222-222222222222"),
        POLL.replace("Microsoft.App", "Microsoft.Storage"),
        POLL.replace("management.azure.com", "user@management.azure.com"),
        POLL + "#fragment",
        POLL.replace("/locations/", "/%2e%2e/"),
        POLL.replace("management.azure.com", "management.azure.com:8443"),
    ],
)
def test_untrusted_operation_url_never_receives_a_token(url: str) -> None:
    with pytest.raises(module.RecoveryError, match="UNTRUSTED_OPERATION_URL"):
        execute([present(), httpx.Response(202, headers={"Location": url})])


def test_async_operation_header_has_precedence_over_location() -> None:
    attempts, calls, _ = execute(
        [
            present(),
            httpx.Response(
                202, headers={"Azure-AsyncOperation": POLL, "Location": "https://evil.example"}
            ),
            present(),
            httpx.Response(204),
            absent(),
            absent(),
        ]
    )
    assert attempts == 1 and str(calls[3].url) == POLL


@pytest.mark.parametrize("scheduled", [True, False])
def test_stuck_delete_and_false_success_are_bounded_and_never_green(scheduled: bool) -> None:
    clock = Clock()
    calls = []

    class Stuck:
        def request(self, method: str, url: str, timeout: float) -> httpx.Response:
            calls.append((method, url))
            return (
                httpx.Response(204)
                if method == "DELETE"
                else present("ScheduledForDelete" if scheduled else "Succeeded")
            )

    with pytest.raises(module.RecoveryError, match="DEADLINE_EXCEEDED|ATTEMPTS_EXHAUSTED"):
        module.Deletion(
            Stuck(), TARGET, module.Policy(120, 1, 3), clock=clock, sleep=clock.sleep
        ).run()
    assert 1 <= sum(method == "DELETE" for method, _ in calls) <= 3
    assert clock.now <= 120


@pytest.mark.parametrize(
    "path",
    [
        ID + "/child",
        ID + "?query",
        ID.replace(RG, "other-rg"),
        ID.replace("managedEnvironments", "containerApps"),
        ID.replace("custom-aca", ".."),
        ID.replace("custom-aca", "encoded%2fchild"),
    ],
)
def test_plan_scope_cannot_be_expanded_by_resource_id(path: str) -> None:
    value = plan()
    value["resource_changes"][0]["change"]["before"]["id"] = path
    with pytest.raises(RuntimeError):
        approve(value)


def test_plan_validation_precedes_any_credentials_and_uses_exact_id() -> None:
    assert approve(plan()) == TARGET
    assert approve({"resource_changes": []}) is None
    invalid = plan()
    invalid["resource_changes"][0]["change"]["actions"] = ["create"]
    with pytest.raises(RuntimeError):
        approve(invalid)
    multiple = plan()
    multiple["resource_changes"].append(copy.deepcopy(multiple["resource_changes"][0]))
    with pytest.raises(module.RecoveryError, match="EXPECTED_ONE"):
        approve(multiple)


def test_redirect_is_not_followed() -> None:
    with pytest.raises(module.RecoveryError, match="ARM_HTTP_302"):
        execute([httpx.Response(302, headers={"Location": "https://evil.example"})])


def test_empty_plan_cli_never_initializes_azure_credentials(monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setattr("sys.stdin", StringIO(json.dumps({"resource_changes": []})))
    monkeypatch.setattr(
        "sys.argv",
        [
            "helper",
            "--expected-subscription-id",
            SUB,
            "--expected-resource-group",
            RG,
            "--protected-state-account",
            "state",
            "--protected-state-container",
            "tfstate",
        ],
    )

    def forbidden(**kwargs: Any) -> None:
        pytest.fail("an empty approved plan has no deletion authority")

    monkeypatch.setattr(module, "AzureCliCredential", forbidden)
    module.main()
    assert "no target" in capsys.readouterr().out


def test_concurrent_replays_have_only_the_same_approved_delete_effect() -> None:
    both_read = Barrier(2)
    lock = Lock()
    deleted = False
    deletes: list[str] = []

    class Shared:
        def request(self, method: str, url: str, timeout: float) -> httpx.Response:
            nonlocal deleted
            if method == "DELETE":
                with lock:
                    assert url == TARGET.url
                    deleted = True
                    deletes.append(url)
                return httpx.Response(204)
            with lock:
                missing = deleted
            if missing:
                return absent()
            both_read.wait(timeout=3)
            return present()

    def run() -> int:
        clock = Clock()
        return module.Deletion(
            Shared(), TARGET, module.Policy(10, 1, 3), clock=clock, sleep=clock.sleep
        ).run()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: run(), range(2)))
    assert results == [1, 1] and deletes == [TARGET.url, TARGET.url]
