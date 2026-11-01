"""Offline admission boundary. Integration is held until its contract is installed."""

from __future__ import annotations


def require_release_authorization() -> None:
    raise SystemExit(
        "DEPLOYMENT_HOLD: lifecycle integration is not released; Azure provisioning is denied"
    )


if __name__ == "__main__":
    require_release_authorization()
