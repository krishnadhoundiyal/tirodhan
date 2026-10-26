"""Registration gate: credentials alone never enable customer delivery."""

import json

from tirodhan.core.config import Settings
from tirodhan.modules.customer_reads.errors import CustomerReadError


def require_fcm(settings: Settings) -> None:
    if (
        not settings.customer_fcm_enabled
        or not settings.fcm_project_id
        or settings.fcm_credentials_json is None
    ):
        raise CustomerReadError(503, "PUSH_PROVIDER_UNAVAILABLE")
    from firebase_admin import credentials  # type: ignore[import-untyped]

    try:
        material = json.loads(settings.fcm_credentials_json.get_secret_value())
        if not isinstance(material, dict):
            raise ValueError()
        certificate = credentials.Certificate(material)
        if certificate.project_id != settings.fcm_project_id:
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise CustomerReadError(503, "PUSH_PROVIDER_UNAVAILABLE") from None
