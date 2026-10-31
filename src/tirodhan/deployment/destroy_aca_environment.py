"""Complete only an approved ACA environment DELETE, then prove resource absence.

This recovery client does not use AzureRM's failed LRO poller, edit Terraform state,
delete resource groups, or grant permissions. It runs only in the protected destroy job.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Any, Protocol
from urllib.parse import unquote, urlsplit
from uuid import UUID

import httpx
from azure.identity import AzureCliCredential

from tirodhan.deployment.check_destroy_plan import validate_destroy_plan

ARM = "https://management.azure.com"
API_VERSION = "2025-07-01"
TRANSIENT = {429, 500, 502, 503, 504}
RETRYABLE_OPERATION = {"InternalServerError", "ServiceUnavailable", "OperationTimedOut"}
DELETING = {"Deleting", "ScheduledForDelete"}


class RecoveryError(RuntimeError):
    """Controlled diagnostics only: never expose raw HTTP/provider/token exceptions."""


@dataclass(frozen=True)
class Target:
    resource_id: str
    subscription_id: str
    resource_group: str

    @property
    def url(self) -> str:
        return f"{ARM}{self.resource_id}?api-version={API_VERSION}"

    def poll_url(self, value: str) -> str:
        try:
            parsed = urlsplit(value)
        except ValueError:
            raise RecoveryError("UNTRUSTED_OPERATION_URL") from None
        path = unquote(parsed.path)
        prefixes = (
            f"/subscriptions/{self.subscription_id}/providers/Microsoft.App/",
            f"/subscriptions/{self.subscription_id}/resourceGroups/"
            f"{self.resource_group}/providers/Microsoft.App/",
        )
        if (
            len(value) > 8192
            or parsed.scheme != "https"
            or parsed.netloc.lower() != "management.azure.com"
            or parsed.fragment
            or any(not 33 <= ord(c) <= 126 for c in value)
            or any(not 33 <= ord(c) <= 126 for c in path)
            or any(s in (".", "..") for s in path.split("/"))
            or "\\" in path
            or "%" in path
            or not any(path.casefold().startswith(p.casefold()) for p in prefixes)
        ):
            raise RecoveryError("UNTRUSTED_OPERATION_URL")
        return value


def approved_target(
    plan: dict[str, Any],
    *,
    subscription: str,
    resource_group: str,
    protected_state_account: str,
    protected_state_container: str,
) -> Target | None:
    validate_destroy_plan(
        plan,
        expected_subscription_id=subscription,
        expected_resource_group=resource_group,
        protected_state_account=protected_state_account,
        protected_state_container=protected_state_container,
        allow_empty=True,
    )
    candidates = [
        c
        for c in plan.get("resource_changes", [])
        if c.get("mode", "managed") == "managed"
        and c.get("type") == "azurerm_container_app_environment"
        and c.get("change", {}).get("actions") == ["delete"]
    ]
    if not candidates:
        return None
    if len(candidates) != 1:
        raise RecoveryError("EXPECTED_ONE_APPROVED_ENVIRONMENT")
    resource_id = candidates[0]["change"]["before"]["id"]
    parts = resource_id.split("/")
    if (
        len(parts) != 9
        or parts[0] != ""
        or parts[1].casefold() != "subscriptions"
        or parts[2].casefold() != str(UUID(subscription))
        or parts[3].casefold() != "resourcegroups"
        or parts[4].casefold() != resource_group.casefold()
        or parts[5].casefold() != "providers"
        or parts[6].casefold() != "microsoft.app"
        or parts[7].casefold() != "managedenvironments"
        or any(
            not re.fullmatch(r"[A-Za-z0-9_.()-]+", s) or s in (".", "..")
            for s in (parts[4], parts[8])
        )
    ):
        raise RecoveryError("INVALID_APPROVED_ENVIRONMENT_ID")
    return Target(resource_id, str(UUID(subscription)), resource_group)


class ArmTransport(Protocol):
    def request(self, method: str, url: str, timeout: float) -> httpx.Response: ...


class ArmClient:
    def __init__(self, client: httpx.Client, token: Callable[[], str]) -> None:
        self.client, self.token = client, token

    def request(self, method: str, url: str, timeout: float) -> httpx.Response:
        # No redirects/proxies or token-bearing command arguments. Content-Type is not
        # used to choose an LRO parser; ARM may finish with empty 200/204 responses.
        with self.client.stream(
            method,
            url,
            headers={"Authorization": f"Bearer {self.token()}"},
            timeout=min(15, timeout),
            follow_redirects=False,
        ) as response:
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > 131072:
                    raise RecoveryError("ARM_RESPONSE_TOO_LARGE")
                chunks.append(chunk)
            # iter_bytes() already decoded gzip/deflate. Do not ask the detached
            # response to decode those bytes again, or retain the wire body length.
            headers = [
                (name, value)
                for name, value in response.headers.multi_items()
                if name.lower() not in {"content-encoding", "content-length"}
            ]
            return httpx.Response(response.status_code, headers=headers, content=b"".join(chunks))


def document(response: httpx.Response) -> dict[str, Any]:
    try:
        value = response.json()
        if isinstance(value, dict):
            return value
    except ValueError:
        pass
    raise RecoveryError("MALFORMED_ARM_RESPONSE")


def error_code(response: httpx.Response) -> str:
    try:
        code = document(response).get("error", {}).get("code")
        if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", code):
            return code
    except (RecoveryError, AttributeError):
        pass
    return "Unknown"


@dataclass(frozen=True)
class Policy:
    timeout_seconds: float = 1800
    poll_seconds: float = 10
    max_delete_attempts: int = 3

    def __post_init__(self) -> None:
        if (
            not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
            or not math.isfinite(self.poll_seconds)
            or self.poll_seconds <= 0
            or self.max_delete_attempts < 1
        ):
            raise RecoveryError("INVALID_RECOVERY_POLICY")


class Deletion:
    def __init__(
        self,
        arm: ArmTransport,
        target: Target,
        policy: Policy | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.arm, self.target, self.policy = arm, target, policy or Policy()
        self.clock, self.sleep, self.wall_clock = clock, sleep, wall_clock
        self.deadline = clock() + self.policy.timeout_seconds
        self.last_observation = "START"

    def remaining(self) -> float:
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise RecoveryError(f"ENVIRONMENT_DELETION_DEADLINE_EXCEEDED_{self.last_observation}")
        return remaining

    def wait(self, response: httpx.Response | None = None) -> None:
        delay = self.policy.poll_seconds
        if response is not None and (raw := response.headers.get("Retry-After")) is not None:
            try:
                if raw.isascii() and raw.isdigit():
                    delay = max(delay, int(raw))
                else:
                    stamp = parsedate_to_datetime(raw)
                    if stamp.tzinfo is None:
                        stamp = stamp.replace(tzinfo=timezone.utc)
                    delay = max(delay, stamp.timestamp() - self.wall_clock())
            except (ValueError, OverflowError, TypeError):
                raise RecoveryError("INVALID_RETRY_AFTER") from None
        self.sleep(min(delay, self.remaining()))

    def request(self, method: str, url: str) -> httpx.Response | None:
        try:
            return self.arm.request(method, url, self.remaining())
        except httpx.TransportError:
            # Ambiguous DELETE may have reached Azure. Always re-read before resubmitting.
            return None

    @staticmethod
    def check_http(response: httpx.Response) -> None:
        raise RecoveryError(f"ARM_HTTP_{response.status_code}_{error_code(response)}")

    def run(self) -> int:
        attempts, absent = 0, 0
        operation: str | None = None
        retry_failed_operation = False
        next_delete = self.clock()
        while True:
            resource = self.request("GET", self.target.url)
            if resource is None or resource.status_code in TRANSIENT:
                self.last_observation = (
                    f"HTTP_{resource.status_code}" if resource is not None else "TRANSPORT_ERROR"
                )
                absent = 0
                self.wait(resource)
                continue
            if resource.status_code == 404 and error_code(resource) == "ResourceNotFound":
                self.last_observation = "ResourceNotFound"
                absent += 1
                if absent == 2:
                    return attempts
                self.wait(resource)
                continue
            absent = 0
            if resource.status_code != 200:
                self.check_http(resource)
            data = document(resource)
            if str(data.get("id", "")).casefold() != self.target.resource_id.casefold():
                raise RecoveryError("UNEXPECTED_RESOURCE_ID")
            properties = data.get("properties")
            if not isinstance(properties, dict):
                raise RecoveryError("MALFORMED_RESOURCE_PROPERTIES")
            state = properties.get("provisioningState")
            self.last_observation = (
                state
                if isinstance(state, str) and re.fullmatch(r"[A-Za-z]{1,64}", state)
                else "RESOURCE_PRESENT"
            )

            if operation is not None:
                result = self.request("GET", operation)
                if result is None or result.status_code in TRANSIENT or result.status_code == 202:
                    self.wait(result)
                    continue
                if result.status_code == 404:
                    # A missing operation record is never evidence of resource absence.
                    operation = None
                elif result.status_code in (200, 204):
                    state = document(result).get("status") if result.content.strip() else None
                    if (
                        result.status_code == 204
                        or not result.content.strip()
                        or state == "Succeeded"
                    ):
                        # Candidate LRO completion only. The next resource GET must confirm it.
                        operation = None
                    elif state in ("Failed", "Canceled", "Cancelled"):
                        if state == "Failed" and error_code(result) in RETRYABLE_OPERATION:
                            operation = None
                            retry_failed_operation = True
                        else:
                            raise RecoveryError(f"ARM_OPERATION_{state}_{error_code(result)}")
                    elif not isinstance(state, str) or not re.fullmatch(r"[A-Za-z]{1,64}", state):
                        raise RecoveryError("MALFORMED_OPERATION_STATUS")
                else:
                    self.check_http(result)
                self.wait(result)
                continue

            if (
                properties.get("provisioningState") in DELETING
                and attempts > 0
                and not retry_failed_operation
                or self.clock() < next_delete
            ):
                self.wait(resource)
                continue
            if attempts >= self.policy.max_delete_attempts:
                raise RecoveryError("ENVIRONMENT_DELETE_ATTEMPTS_EXHAUSTED")
            attempts += 1
            # On restart an existing deletion has no local LRO handle. One idempotent
            # DELETE reattaches to that exact resource; OperationInProgress is observed,
            # not treated as absence. Later retries require a failed/ambiguous attempt.
            retry_failed_operation = False
            result = self.request("DELETE", self.target.url)
            next_delete = self.clock() + max(30, self.policy.poll_seconds)
            if result is not None:
                if result.status_code in (200, 202, 204):
                    url = result.headers.get("Azure-AsyncOperation") or result.headers.get(
                        "Location"
                    )
                    if url:
                        operation = self.target.poll_url(url)
                elif result.status_code == 404 and error_code(result) == "ResourceNotFound":
                    pass  # Independently confirmed by resource GET, never inferred from DELETE.
                elif result.status_code == 409 and error_code(result) in (
                    "AnotherOperationInProgress",
                    "OperationInProgress",
                ):
                    pass
                elif result.status_code not in TRANSIENT:
                    self.check_http(result)
            self.wait(result)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-subscription-id", required=True)
    parser.add_argument("--expected-resource-group", required=True)
    parser.add_argument("--protected-state-account", required=True)
    parser.add_argument("--protected-state-container", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=1800)
    parser.add_argument("--poll-seconds", type=float, default=10)
    parser.add_argument("--max-delete-attempts", type=int, default=3)
    args = parser.parse_args()
    # Never emit HTTP/credential request dumps from this operator helper.
    for name in ("httpx", "httpcore", "azure.identity"):
        logging.getLogger(name).setLevel(logging.WARNING)
    try:
        policy = Policy(args.timeout_seconds, args.poll_seconds, args.max_delete_attempts)
        target = approved_target(
            json.load(sys.stdin),
            subscription=args.expected_subscription_id,
            resource_group=args.expected_resource_group,
            protected_state_account=args.protected_state_account,
            protected_state_container=args.protected_state_container,
        )
        if target is None:
            print("No managed ACA environment delete in the approved plan; recovery has no target.")
            return
        with (
            AzureCliCredential(
                tenant_id=os.environ["ARM_TENANT_ID"],
                subscription=target.subscription_id,
                process_timeout=10,
            ) as credential,
            httpx.Client(trust_env=False, follow_redirects=False) as client,
        ):
            arm = ArmClient(client, lambda: credential.get_token(f"{ARM}/.default").token)
            attempts = Deletion(arm, target, policy).run()
        print(
            f"Approved ACA environment absence confirmed by direct GET; DELETE attempts={attempts}."
        )
    except RecoveryError as error:
        print(f"ACA deletion recovery failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None
    except Exception:
        print(
            "ACA deletion recovery failed: inspect approved resource/activity status safely.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
