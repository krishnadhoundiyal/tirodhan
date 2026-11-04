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
from tirodhan.modules.payments.ports import PaymentProviderNotConfiguredError


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
        "fcm-credentials-json": json.dumps(
            {
                "type": "service_account",
                "project_id": "test-project",
                "client_email": "test@example.invalid",
                "token_uri": "https://oauth2.googleapis.com/token",
                "private_key": key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                ).decode(),
            }
        ),
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
        "TIRODHAN_FCM_PROJECT_ID": "test-project",
        "TIRODHAN_DB_POOL_SIZE": "2",
        "TIRODHAN_DB_MAX_OVERFLOW": "0",
        "TIRODHAN_DB_POOL_TIMEOUT": "5",
        "TIRODHAN_SERVICE_BUS_OPERATION_TIMEOUT_SECONDS": "5",
        "TIRODHAN_SERVICEABILITY_LOCK_RENEWAL_SECONDS": "60",
        "TIRODHAN_RIDER_NOTIFICATION_LOCK_RENEWAL_SECONDS": "60",
        "TIRODHAN_REFUND_LOCK_RENEWAL_SECONDS": "60",
        "TIRODHAN_PLANNING_LOCK_RENEWAL_SECONDS": "60",
        "TIRODHAN_OUTBOX_PUBLISH_BATCH_SIZE": "10",
        "TIRODHAN_RIDER_OFFER_LIFETIME_SECONDS": "60",
        "TIRODHAN_PLANNING_LEAD_TIME_MINUTES": "5",
        "TIRODHAN_PLANNING_MAX_ATTEMPTS": "3",
        "TIRODHAN_PLANNING_COMPACTION_DISTANCE_M": "100",
        "TIRODHAN_PLANNING_MAX_GROUP_REQUESTS": "10",
        "TIRODHAN_FINANCIAL_WEBHOOK_LOCK_RENEWAL_SECONDS": "60",
        "TIRODHAN_FINANCIAL_WEBHOOK_SEND_TIMEOUT_SECONDS": "3",
        "TIRODHAN_RAZORPAY_KEY_ID": "test-payment-id",
        "TIRODHAN_RAZORPAY_HTTP_TIMEOUT_SECONDS": "5",
        "TIRODHAN_RAZORPAY_ACCOUNT_ID": "test-account",
        "TIRODHAN_COMMAND_IDEMPOTENCY_TTL_SECONDS": "300",
    }
    reads = []

    def vault_read(*args):
        if "list" in args:
            return [{"name": name, "attributes": {"enabled": True}} for name in material]
        name = args[args.index("--name") + 1]
        reads.append(name)
        return {"value": material[name], "attributes": {"enabled": True}}

    monkeypatch.setattr(observations.runner, "json", vault_read)
    return Checks(auth("runtime-plan"), observations.config, {}, observations), reads


def test_twofactor_runtime_passes_without_any_kaleyra_credentials(material, monkeypatch, capsys):
    checks, reads = runtime_checks(material, monkeypatch)
    assert checks.check_secrets()
    assert "twofactor-api-key" in reads
    assert "google-maps-api-key" in reads
    assert "kaleyra-api-key" not in reads
    assert capsys.readouterr().out == ""


def test_rider_fcm_is_mandatory_even_when_customer_push_is_disabled(material, monkeypatch):
    without_fcm = {
        name: value for name, value in material.items() if name != "fcm-credentials-json"
    }
    checks, reads = runtime_checks(without_fcm, monkeypatch)
    checks.tf["runtime_env"]["TIRODHAN_CUSTOMER_FCM_ENABLED"] = "false"
    assert not checks.check_secrets()
    assert reads == []


@pytest.mark.parametrize("project", [None, "different-project"])
def test_rider_fcm_project_must_match_actual_certificate(material, monkeypatch, project):
    checks, _ = runtime_checks(material, monkeypatch)
    if project is None:
        checks.tf["runtime_env"].pop("TIRODHAN_FCM_PROJECT_ID")
    else:
        checks.tf["runtime_env"]["TIRODHAN_FCM_PROJECT_ID"] = project
    with pytest.raises(ValueError, match="^Rider FCM certificate"):
        checks.check_secrets()


def test_invalid_rider_fcm_material_is_rejected_without_echoing_it(material, monkeypatch):
    checks, _ = runtime_checks(
        material | {"fcm-credentials-json": "do-not-echo-this-value"}, monkeypatch
    )
    with pytest.raises(ValueError, match="^Rider FCM certificate") as error:
        checks.check_secrets()
    assert "do-not-echo-this-value" not in str(error.value)


@pytest.mark.parametrize(
    "missing",
    [
        "TIRODHAN_DB_POOL_SIZE",
        "TIRODHAN_OUTBOX_PUBLISH_BATCH_SIZE",
        "TIRODHAN_PLANNING_LOCK_RENEWAL_SECONDS",
        "TIRODHAN_RIDER_OFFER_LIFETIME_SECONDS",
        "TIRODHAN_PLANNING_MAX_ATTEMPTS",
        "TIRODHAN_FINANCIAL_WEBHOOK_LOCK_RENEWAL_SECONDS",
        "TIRODHAN_FINANCIAL_WEBHOOK_SEND_TIMEOUT_SECONDS",
    ],
)
def test_default_workload_startup_inputs_cannot_be_omitted(material, monkeypatch, missing):
    checks, _ = runtime_checks(material, monkeypatch)
    checks.tf["runtime_env"].pop(missing)
    with pytest.raises(ValueError, match="^Default workload startup configuration"):
        checks.check_secrets()


@pytest.mark.parametrize(
    "missing",
    [
        "TIRODHAN_RAZORPAY_KEY_ID",
        "TIRODHAN_RAZORPAY_HTTP_TIMEOUT_SECONDS",
        "TIRODHAN_RAZORPAY_ACCOUNT_ID",
    ],
)
def test_financial_startup_cannot_pass_with_only_secret_presence(material, monkeypatch, missing):
    checks, _ = runtime_checks(material, monkeypatch)
    checks.tf["runtime_env"].pop(missing)
    with pytest.raises((ValueError, PaymentProviderNotConfiguredError)):
        checks.check_secrets()


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


@pytest.mark.parametrize(
    "attributes,usable",
    [
        ({"enabled": True}, True),
        ({"enabled": True, "notBefore": 999, "expires": 1001}, True),
        ({"enabled": True, "notBefore": 1000}, True),
        ({"enabled": True, "notBefore": 1001}, False),
        ({"enabled": True, "expires": 1000}, False),
        ({"enabled": False}, False),
        ({"enabled": "true"}, False),
        ({"enabled": True, "expires": float("inf")}, False),
        ({"enabled": True, "notBefore": float("nan")}, False),
        ({"enabled": True, "notBefore": True}, False),
        ({"enabled": True, "notBefore": "1970-01-01T00:16:39Z"}, True),
        ({"enabled": True, "notBefore": "1970-01-01T00:16:41+00:00"}, False),
        ({"enabled": True, "expires": "1970-01-01T00:16:41+00:00"}, True),
        ({"enabled": True, "expires": "1970-01-01T00:16:41"}, False),
        ({"enabled": True, "expires": "do-not-echo"}, False),
        (None, False),
    ],
)
def test_metadata_activation_is_required_and_unknown_fails_closed(monkeypatch, attributes, usable):
    monkeypatch.setattr("tirodhan.deployment.lifecycle_checks.time.time", lambda: 1000)
    assert Checks.secret_metadata_usable(attributes) is usable


@pytest.mark.parametrize("phase", ["list", "show"])
def test_future_activation_cannot_pass_listing_or_latest_value_read(material, monkeypatch, phase):
    checks, reads = runtime_checks(material, monkeypatch)
    previous = checks.io.runner.json

    def read(*args):
        response = previous(*args)
        if phase in args:
            if isinstance(response, list):
                for row in response:
                    row["attributes"]["notBefore"] = 10**12
            else:
                response["attributes"]["notBefore"] = 10**12
        return response

    monkeypatch.setattr(checks.io.runner, "json", read)
    assert not checks.check_secrets()
    if phase == "list":
        assert reads == []
