"""Validate proposed runtime values without reading developer environment or .env secrets."""

from __future__ import annotations

import json
from typing import Any, get_args, get_origin

from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

from tirodhan.core.config import Settings

# These are explicit startup requirements of the default pooled API/workers and
# planning/outbox jobs. Azure-derived namespaces, queues and identities are owned
# by the foundation and Terraform plan, not operator-supplied runtime_env values.
DEPLOYED_STARTUP_SETTINGS = (
    "db_pool_size",
    "db_max_overflow",
    "db_pool_timeout",
    "service_bus_operation_timeout_seconds",
    "serviceability_lock_renewal_seconds",
    "rider_notification_lock_renewal_seconds",
    "refund_lock_renewal_seconds",
    "planning_lock_renewal_seconds",
    "outbox_publish_batch_size",
    "rider_offer_lifetime_seconds",
    "planning_lead_time_minutes",
    "planning_max_attempts",
    "planning_compaction_distance_m",
    "planning_max_group_requests",
    "financial_webhook_lock_renewal_seconds",
    "financial_webhook_send_timeout_seconds",
    "command_idempotency_ttl_seconds",
)


class DeploymentSettings(Settings):
    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings,)


def _collection_field(annotation: Any) -> bool:
    origin = get_origin(annotation)
    if origin in (list, dict, set, tuple):
        return True
    return any(_collection_field(value) for value in get_args(annotation))


def proposed_settings(runtime: dict[str, Any], material: dict[str, str] | None = None) -> Settings:
    # Terraform runtime_env is map(string); the application environment source
    # JSON-decodes complex fields. Reproduce only that conversion on reviewed input.
    values = {k.removeprefix("TIRODHAN_").lower(): v for k, v in runtime.items()}
    if material:
        values.update({k.replace("-", "_"): v for k, v in material.items()})
    for key, value in values.items():
        field = DeploymentSettings.model_fields.get(key)
        if field is not None and isinstance(value, str) and _collection_field(field.annotation):
            values[key] = json.loads(value)
    return DeploymentSettings(**values)


def validate_optional_runtime_configuration(terraform: dict[str, Any]) -> None:
    if terraform.get("financial_inventory_schedule") is not None:
        from tirodhan.workers.financial_inventory import configured_policies

        configured_policies(proposed_settings(terraform.get("runtime_env", {})))


def validate_runtime_material(runtime: dict[str, Any], material: dict[str, str]) -> None:
    from firebase_admin import credentials  # type: ignore[import-untyped]

    from tirodhan.modules.customer_account.protection import CustomerDataProtection
    from tirodhan.modules.customer_reads.cursor import CursorCodec
    from tirodhan.modules.customers.address_protection import AesGcmAddressProtector
    from tirodhan.modules.identity.phone_protection import AesGcmPhoneIdentityProtector
    from tirodhan.modules.identity.runtime import validate_otp_settings
    from tirodhan.modules.identity.tokens import Rs256AccessTokenCodec
    from tirodhan.modules.payments.razorpay import razorpay_configured

    settings = proposed_settings(runtime, material)
    validate_otp_settings(settings)
    if settings.google_maps_api_key is None or settings.google_maps_http_timeout_seconds is None:
        raise ValueError("Google Geocoding configuration is incomplete")
    Rs256AccessTokenCodec(
        private_key_pem=material["auth-jwt-private-key-pem"],
        public_key_pem=material["auth-jwt-public-key-pem"],
        issuer=settings.auth_token_issuer or "",
        audience=settings.auth_token_audience or "",
    )
    AesGcmPhoneIdentityProtector.from_configuration(
        active_key_id=settings.phone_encryption_active_key_id or "",
        encryption_keys_json=material["phone-encryption-keys"],
        lookup_hmac_key_base64=material["phone-lookup-hmac-key"],
    )
    AesGcmAddressProtector.from_configuration(
        active_key_id=settings.address_encryption_active_key_id or "",
        encryption_keys_json=material["address-encryption-keys"],
    )
    CustomerDataProtection(settings)
    CursorCodec(material["customer-cursor-signing-key"].encode())
    if settings.customer_cursor_ttl_seconds is None:
        raise ValueError("Customer cursor expiry policy is unavailable")
    if any(getattr(settings, field) is None for field in DEPLOYED_STARTUP_SETTINGS):
        raise ValueError("Default workload startup configuration is incomplete")
    if not razorpay_configured(settings) or not (settings.razorpay_account_id or "").strip():
        raise ValueError("Default financial workload configuration is incomplete")
    # Rider notifications are a default workload even when customer FCM is disabled.
    # Parse the actual service-account certificate locally; never send a notification.
    try:
        if not settings.fcm_project_id or not settings.fcm_project_id.strip():
            raise ValueError
        certificate = credentials.Certificate(json.loads(material["fcm-credentials-json"]))
        if certificate.project_id != settings.fcm_project_id:
            raise ValueError
    except Exception:
        raise ValueError(
            "Rider FCM certificate and project configuration are unavailable or invalid"
        ) from None
