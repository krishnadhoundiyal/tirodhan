"""Validate proposed runtime values without reading developer environment or .env secrets."""

from __future__ import annotations

import json
from typing import Any, get_args, get_origin

from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

from tirodhan.core.config import Settings


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


def validate_runtime_material(runtime: dict[str, Any], material: dict[str, str]) -> None:
    from tirodhan.modules.customer_account.protection import CustomerDataProtection
    from tirodhan.modules.customer_reads.cursor import CursorCodec
    from tirodhan.modules.customers.address_protection import AesGcmAddressProtector
    from tirodhan.modules.identity.phone_protection import AesGcmPhoneIdentityProtector
    from tirodhan.modules.identity.tokens import Rs256AccessTokenCodec

    settings = proposed_settings(runtime, material)
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
