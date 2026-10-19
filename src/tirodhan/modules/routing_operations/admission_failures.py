"""A failed search/provider/input snapshot is not a physical-capacity proof."""

from enum import Enum


class FailureKind(str, Enum):
    INVALID_COMMAND = "INVALID_COMMAND"
    NO_DECLARED_RESOURCE = "NO_DECLARED_RESOURCE"
    UNKNOWN_SEARCH = "UNKNOWN_SEARCH"
    SOURCE_OR_INFRASTRUCTURE = "SOURCE_OR_INFRASTRUCTURE"


TERMINAL_COMMANDS = frozenset(
    {
        "SLOT_UNAVAILABLE",
        "ADMISSION_EXPIRED",
        "CAPACITY_RESERVATION_EXPIRED",
        "RESERVATION_REPLAY_CONFLICT",
        "SERVICEABILITY_CONTEXT_REQUIRED",
        "SERVICEABILITY_CONTEXT_CHANGED",
        "OPERATING_REGION_REQUIRED",
        "ADMISSION_REGION_CHANGED",
        "LIVE_AUTHORIZATION_REQUIRED",
        "REATTEMPT_BOOKING_REQUIRED",
    }
)


def classify(code: str) -> FailureKind:
    if code in TERMINAL_COMMANDS:
        return FailureKind.INVALID_COMMAND
    if code == "NO_COMMITTED_CAPACITY":
        return FailureKind.NO_DECLARED_RESOURCE
    if code.startswith("UNKNOWN_") or code == "NO_FEASIBLE_REGIONAL_CAPACITY":
        return FailureKind.UNKNOWN_SEARCH
    return FailureKind.SOURCE_OR_INFRASTRUCTURE


def retryable(code: str) -> bool:
    return classify(code) in (FailureKind.UNKNOWN_SEARCH, FailureKind.SOURCE_OR_INFRASTRUCTURE)
