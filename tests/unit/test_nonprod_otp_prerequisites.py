"""Selected OTP credentials gate runtime admission without contacting a provider."""

import base64
import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from lifecycle_fakes import MemoryObservations, auth

from tirodhan.deployment.deployment_admission import AdmissionDenied
from tirodhan.deployment.lifecycle_checks import Checks
from tirodhan.deployment.release import MIGRATION_SECRET_NAMES
from tirodhan.modules.identity.ports import OtpProviderConfigurationError


@pytest.fixture(scope="module")
def material():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    encoded = base64.b64encode(b"a" * 32).decode()
    independent = base64.b64encode(b"b" * 32).decode()
    return {
        "auth-jwt-private-key-pem": key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
        "auth-jwt-public-key-pem": key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode(),
        "phone-encryption-keys": json.dumps({"test": encoded}),
        "phone-lookup-hmac-key": independent,
        "address-encryption-keys": json.dumps({"test": encoded}),
        "customer-data-encryption-keys": json.dumps({"test": encoded}),
        "customer-data-hmac-key": independent,
        "customer-cursor-signing-key": "test-cursor-key-" + "x" * 32,
        "twofactor-api-key": "test-provider-key",
        "google-maps-api-key": "test-geocoding-key",
        "razorpay-key-secret": "test-payment-key",
        "razorpay-webhook-secret": "test-webhook-key",
        "ghcr-pull-pat": "test-pull-key",
        "fluent-bit-sas": "test-log-key",
    }


def runtime_checks(material, monkeypatch):
    observations = MemoryObservations()
    observations.config["terraform"]["runtime_env"] = {
        "TIRODHAN_OTP_PROVIDER": "2FACTOR",
        "TIRODHAN_TWOFACTOR_HTTP_TIMEOUT_SECONDS": "5",
        "TIRODHAN_GOOGLE_MAPS_HTTP_TIMEOUT_SECONDS": "5",
        "TIRODHAN_PHONE_ENCRYPTION_ACTIVE_KEY_ID": "test",
        "TIRODHAN_ADDRESS_ENCRYPTION_ACTIVE_KEY_ID": "test",
        "TIRODHAN_CUSTOMER_DATA_ACTIVE_KEY_ID": "test",
        "TIRODHAN_CUSTOMER_CURSOR_TTL_SECONDS": "300",
        "TIRODHAN_AUTH_TOKEN_ISSUER": "test-issuer",
        "TIRODHAN_AUTH_TOKEN_AUDIENCE": "test-audience",
    }
    reads = []

    def vault_read(*args):
        if "list" in args:
            return [{"name": name, "attributes": {"enabled": True}} for name in material]
        name = args[args.index("--name") + 1]
        reads.append(name)
        return {"value": material[name]}

    monkeypatch.setattr(observations.runner, "json", vault_read)
    return Checks(auth("runtime-plan"), observations.config, {}, observations), reads


def test_twofactor_runtime_passes_without_any_kaleyra_credentials(material, monkeypatch, capsys):
    checks, reads = runtime_checks(material, monkeypatch)
    assert checks.check_secrets()
    assert "twofactor-api-key" in reads
    assert "google-maps-api-key" in reads
    assert "kaleyra-api-key" not in reads
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("provider", [None, "", "UNAPPROVED"])
def test_absent_or_unknown_provider_cannot_omit_mandatory_otp(material, monkeypatch, provider):
    checks, reads = runtime_checks(material, monkeypatch)
    checks.tf["runtime_env"]["TIRODHAN_OTP_PROVIDER"] = provider
    with pytest.raises(AdmissionDenied, match="^OTP_PROVIDER_NOT_SELECTED$"):
        checks.check_secrets()
    assert reads == []


@pytest.mark.parametrize("missing", ["twofactor-api-key", "google-maps-api-key"])
def test_selected_provider_and_geocoding_credentials_remain_mandatory(
    material, monkeypatch, missing
):
    checks, reads = runtime_checks({k: v for k, v in material.items() if k != missing}, monkeypatch)
    assert not checks.check_secrets()
    assert reads == []


@pytest.mark.parametrize("value", [None, "0", "61", "nan"])
def test_twofactor_requires_a_bounded_configured_timeout(material, monkeypatch, value):
    checks, _ = runtime_checks(material, monkeypatch)
    runtime = checks.tf["runtime_env"]
    if value is None:
        runtime.pop("TIRODHAN_TWOFACTOR_HTTP_TIMEOUT_SECONDS")
    else:
        runtime["TIRODHAN_TWOFACTOR_HTTP_TIMEOUT_SECONDS"] = value
    with pytest.raises((ValueError, OtpProviderConfigurationError)):
        checks.check_secrets()


def test_developer_environment_cannot_supply_missing_provider_settings(material, monkeypatch):
    checks, _ = runtime_checks(material, monkeypatch)
    checks.tf["runtime_env"].pop("TIRODHAN_TWOFACTOR_HTTP_TIMEOUT_SECONDS")
    monkeypatch.setenv("TIRODHAN_TWOFACTOR_HTTP_TIMEOUT_SECONDS", "5")
    with pytest.raises(OtpProviderConfigurationError):
        checks.check_secrets()


def test_explicit_legacy_selection_does_not_fall_back_to_twofactor(material, monkeypatch):
    checks, reads = runtime_checks(material, monkeypatch)
    checks.tf["runtime_env"]["TIRODHAN_OTP_PROVIDER"] = "KALEYRA_VERIFY"
    assert "kaleyra-api-key" in checks.required_secret_names()
    assert "twofactor-api-key" not in checks.required_secret_names()
    assert not checks.check_secrets()
    assert reads == []


@pytest.mark.parametrize("key", ["internal whitespace", "x" * 201, ".", ".."])
def test_malformed_twofactor_key_fails_before_runtime(material, monkeypatch, key):
    checks, _ = runtime_checks(material | {"twofactor-api-key": key}, monkeypatch)
    with pytest.raises(OtpProviderConfigurationError):
        checks.check_secrets()


@pytest.mark.parametrize("template", [".", "..", "x" * 201, "line\nbreak"])
def test_malformed_twofactor_template_fails_before_runtime(material, monkeypatch, template):
    checks, _ = runtime_checks(material, monkeypatch)
    checks.tf["runtime_env"]["TIRODHAN_TWOFACTOR_TEMPLATE_NAME"] = template
    with pytest.raises(OtpProviderConfigurationError):
        checks.check_secrets()


@pytest.mark.parametrize("timeout", [None, "0", "61", "nan"])
def test_geocoding_timeout_cannot_be_omitted_or_invalid(material, monkeypatch, timeout):
    checks, _ = runtime_checks(material, monkeypatch)
    if timeout is None:
        checks.tf["runtime_env"].pop("TIRODHAN_GOOGLE_MAPS_HTTP_TIMEOUT_SECONDS")
    else:
        checks.tf["runtime_env"]["TIRODHAN_GOOGLE_MAPS_HTTP_TIMEOUT_SECONDS"] = timeout
    with pytest.raises(ValueError):
        checks.check_secrets()


@pytest.mark.parametrize("operation", ["foundation-plan", "migration-plan", "migration-apply"])
def test_stages_without_application_runtime_do_not_require_otp(operation):
    observations = MemoryObservations()
    observations.config["terraform"]["runtime_env"] = {}
    checks = Checks(auth(operation), observations.config, {}, observations)
    assert checks.required_secret_names() == set(MIGRATION_SECRET_NAMES)
