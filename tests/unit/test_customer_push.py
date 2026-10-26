"""Standards vector, SSRF transport boundary and real adapter wire shape (no live send)."""

import json
import socket
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from tirodhan.core.config import Settings
from tirodhan.db.values import new_uuid7
from tirodhan.modules.customer_account import native_push, web_push
from tirodhan.modules.customer_account.protection import CustomerDataProtection
from tirodhan.modules.customer_account.schemas import BrowserSubscription
from tirodhan.modules.customer_reads.errors import CustomerReadError
from tirodhan.modules.dispatch import push


def subscription(endpoint: str = "https://push.example.test/push/opaque") -> BrowserSubscription:
    return BrowserSubscription.model_validate(
        {
            "endpoint": endpoint,
            "expirationTime": None,
            "keys": {
                "auth": "BTBZMqHH6r4Tts7J_aSIgg",
                "p256dh": "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcx"
                "aOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4",
            },
        }
    )


def web_settings(**changes: Any) -> Settings:
    key = ec.generate_private_key(ec.SECP256R1())
    values = dict(
        _env_file=None,
        customer_web_push_enabled=True,
        customer_web_push_origins=["https://customer.example.test"],
        customer_web_push_endpoint_origins=["https://push.example.test"],
        customer_vapid_subject="mailto:synthetic@example.test",
        customer_vapid_private_key_pem=key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
    )
    values.update(changes)
    return Settings(**values)


def test_rfc8291_section5_exact_published_encryption_vector(monkeypatch: Any) -> None:
    # https://www.rfc-editor.org/rfc/rfc8291#section-5 (public test keys).
    sender = ec.derive_private_key(
        int.from_bytes(web_push.unb64("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"), "big"),
        ec.SECP256R1(),
    )
    monkeypatch.setattr(web_push.ec, "generate_private_key", lambda *_: sender)
    monkeypatch.setattr(
        web_push.secrets, "token_bytes", lambda _: web_push.unb64("DGv6ra1nlYgDCS1FRnbzlw")
    )
    encrypted = web_push.encrypt_payload(
        subscription(), b"When I grow up, I want to be a watermelon"
    )
    assert web_push.b64(encrypted) == (
        "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27ml"
        "mlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPT"
        "pK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN"
    )


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://push.example.test/a",
        "https://127.0.0.1/a",
        "https://[::1]/a",
        "https://169.254.169.254/metadata",
        "https://push.example.test.evil.test/a",
        "https://push.example.test@evil.test/a",
        "https://push.example.test:8443/a",
        "https://push.example.test/a#fragment",
        "https://push.example.test./a",
        "https://push.example.test/a\\b",
        "https://push.example.test/a\n",
        "https://push.example.test/a\x00",
        "https://push.example.test/अ",
    ],
)
def test_subscription_endpoint_cannot_escape_exact_approved_origin(endpoint: str) -> None:
    with pytest.raises(CustomerReadError) as failure:
        web_push.validate_subscription(subscription(endpoint), web_settings())
    assert failure.value.status == 422


@pytest.mark.parametrize(
    "addresses",
    [
        ["127.0.0.1"],
        ["10.0.0.1"],
        ["169.254.169.254"],
        ["::1"],
        ["fc00::1"],
        ["8.8.8.8", "127.0.0.1"],
    ],
)
def test_dns_private_or_mixed_answers_never_open_socket(monkeypatch: Any, addresses: Any) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in addresses
        ],
    )

    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("socket must not open for a private/mixed DNS result")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    with pytest.raises(OSError, match="nonpublic"):
        web_push.PublicHttpsConnection("push.example.test").connect()


def test_transport_pins_dns_ip_and_retains_tls_hostname(monkeypatch: Any) -> None:
    seen: list[Any] = []

    class Socket:
        def settimeout(self, timeout: float) -> None:
            assert 0 < timeout <= 10

    class Context:
        def wrap_socket(self, sock: Any, server_hostname: str) -> Any:
            seen.append(server_hostname)
            return sock

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))],
    )

    def connect(address: Any, timeout: Any) -> Any:
        seen.append(address)
        return Socket()

    monkeypatch.setattr(socket, "create_connection", connect)
    monkeypatch.setattr(web_push.ssl, "create_default_context", Context)
    web_push.PublicHttpsConnection("push.example.test").connect()
    assert seen == [("8.8.8.8", 443), "push.example.test"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,outcome",
    [
        (201, "SENT"),
        (202, "SENT"),
        (404, "INVALID_TOKEN"),
        (410, "INVALID_TOKEN"),
        (429, "RETRY"),
        (500, "RETRY"),
        (401, "RETRY"),
        (302, "RETRY"),
        (400, "PERMANENT_FAILURE"),
    ],
)
async def test_web_adapter_vapid_payload_and_classification(
    monkeypatch: Any, status: int, outcome: str
) -> None:
    settings = web_settings()
    signal = {
        "schema_version": 1,
        "event_id": str(new_uuid7()),
        "target": "COLLECTION",
        "request_id": str(new_uuid7()),
    }

    def post(endpoint: str, body: bytes, headers: dict[str, str]) -> int:
        assert endpoint == subscription().endpoint.get_secret_value()
        assert b"request_id" not in body
        assert headers["Content-Encoding"] == "aes128gcm"
        auth = headers["Authorization"]
        token, encoded_key = auth.removeprefix("vapid t=").split(",k=")
        public = ec.EllipticCurvePublicKey.from_encoded_point(
            ec.SECP256R1(), web_push.unb64(encoded_key)
        )
        claims = jwt.decode(
            token, public, algorithms=["ES256"], audience="https://push.example.test"
        )
        assert claims["sub"] == "mailto:synthetic@example.test"
        assert set(claims) == {"aud", "exp", "sub"}
        return status

    monkeypatch.setattr(web_push, "post_public", post)
    assert await web_push.send_web_push(subscription(), signal, settings) == outcome


def test_provider_configuration_is_explicit_and_fails_closed() -> None:
    for settings in [
        Settings(_env_file=None),
        web_settings(customer_web_push_enabled=False),
        web_settings(customer_web_push_origins=[]),
        web_settings(customer_web_push_endpoint_origins=["http://localhost"]),
        web_settings(customer_vapid_private_key_pem="invalid"),
    ]:
        with pytest.raises(CustomerReadError) as failure:
            web_push.check_configuration(settings)
        assert failure.value.status == 503
    with pytest.raises(CustomerReadError):
        native_push.require_fcm(
            Settings(_env_file=None, fcm_project_id="synthetic", fcm_credentials_json="{}")
        )
    for material in ["{}", "[]", "7", json.dumps("/tmp/not-a-service-account")]:
        with pytest.raises(CustomerReadError):
            native_push.require_fcm(
                Settings(
                    _env_file=None,
                    customer_fcm_enabled=True,
                    fcm_project_id="synthetic",
                    fcm_credentials_json=material,
                )
            )
    with pytest.raises(CustomerReadError):
        CustomerDataProtection(Settings(_env_file=None))


@pytest.mark.asyncio
async def test_fcm_customer_signal_uses_shared_adapter_and_no_household_payload(
    monkeypatch: Any,
) -> None:
    seen: list[Any] = []

    class Response:
        success = True
        message_id = "synthetic-message"

    class Batch:
        responses = [Response()]

    def send(message: Any, **kwargs: Any) -> Any:
        seen.append(message)
        return Batch()

    monkeypatch.setattr(push.messaging, "send_each_for_multicast", send)
    adapter = object.__new__(push.FcmPushNotificationPort)
    adapter._app = object()
    signal = push.CustomerCollectionNotification(
        new_uuid7(), new_uuid7(), "Collection update", "Open your collection."
    )
    result = await adapter.send_batch([push.PushRecipient(new_uuid7(), "private-token")], signal)
    assert result[0].status == "SENT"
    assert seen[0].data == {
        "schema_version": "1",
        "event_id": str(signal.event_id),
        "target": "COLLECTION",
        "request_id": str(signal.request_id),
    }
    assert seen[0].notification.title == signal.title
    assert "private-token" not in json.dumps(seen[0].data)
