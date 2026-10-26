"""RFC 8291/8292 Web Push; exact approved origins and DNS-pinned public TLS only."""

import asyncio
import base64
import http.client
import ipaddress
import secrets
import socket
import ssl
import time
from datetime import timedelta
from urllib.parse import urlsplit

import jwt
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from tirodhan.core.config import Settings
from tirodhan.db.values import utc_now
from tirodhan.modules.customer_account.protection import canonical
from tirodhan.modules.customer_account.schemas import BrowserSubscription
from tirodhan.modules.customer_account.web_push_transport import WebPushTransport
from tirodhan.modules.customer_reads.errors import CustomerReadError


def b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def unb64(value: str) -> bytes:
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


def origin(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or parsed.port not in (None, 443)
        or not parsed.hostname.isascii()
        or parsed.hostname.endswith(".")
        or any(c.isspace() for c in value)
    ):
        raise ValueError("invalid HTTPS origin")
    try:
        ipaddress.ip_address(parsed.hostname)
    except ValueError:
        pass
    else:
        raise ValueError("IP endpoints are forbidden")
    return f"https://{parsed.hostname}"


def check_configuration(settings: Settings) -> ec.EllipticCurvePrivateKey:
    try:
        if (
            not settings.customer_web_push_enabled
            or not settings.customer_web_push_origins
            or not settings.customer_web_push_endpoint_origins
            or settings.customer_vapid_private_key_pem is None
            or settings.customer_vapid_subject is None
        ):
            raise ValueError()
        for item in (
            settings.customer_web_push_origins + settings.customer_web_push_endpoint_origins
        ):
            if origin(item) != item or urlsplit(item).path or urlsplit(item).query:
                raise ValueError()
        subject = settings.customer_vapid_subject
        if not (subject.startswith("mailto:") and "@" in subject and "\n" not in subject):
            if not subject.startswith("https://"):
                raise ValueError()
            origin(subject)
        key = serialization.load_pem_private_key(
            settings.customer_vapid_private_key_pem.get_secret_value().encode(), password=None
        )
        if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
            raise ValueError()
        return key
    except (ValueError, TypeError):
        raise CustomerReadError(503, "WEB_PUSH_UNAVAILABLE") from None


def validate_subscription(value: BrowserSubscription, settings: Settings) -> str:
    try:
        endpoint = value.endpoint.get_secret_value()
        if (
            not 1 <= len(endpoint) <= 2048
            or "\\" in endpoint
            or any(not 33 <= ord(c) <= 126 for c in endpoint)
        ):
            raise ValueError()
        endpoint_origin = origin(endpoint)
        if endpoint_origin not in settings.customer_web_push_endpoint_origins:
            raise ValueError()
        public = unb64(value.keys.p256dh)
        if len(public) != 65 or len(unb64(value.keys.auth)) != 16:
            raise ValueError()
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), public)
        if value.expirationTime is not None and value.expirationTime <= int(
            utc_now().timestamp() * 1000
        ):
            raise ValueError()
        return endpoint
    except (ValueError, TypeError):
        raise CustomerReadError(422, "INVALID_SUBSCRIPTION") from None


def encrypt_payload(subscription: BrowserSubscription, payload: bytes) -> bytes:
    if len(payload) > 3000:
        raise ValueError("push payload exceeds single-record budget")
    ua_public = unb64(subscription.keys.p256dh)
    ephemeral = ec.generate_private_key(ec.SECP256R1())
    server_public = ephemeral.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    shared = ephemeral.exchange(
        ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    )
    ikm = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=unb64(subscription.keys.auth),
        info=b"WebPush: info\x00" + ua_public + server_public,
    ).derive(shared)
    salt = secrets.token_bytes(16)
    cek = HKDF(
        algorithm=hashes.SHA256(), length=16, salt=salt, info=b"Content-Encoding: aes128gcm\x00"
    ).derive(ikm)
    nonce = HKDF(
        algorithm=hashes.SHA256(), length=12, salt=salt, info=b"Content-Encoding: nonce\x00"
    ).derive(ikm)
    return (
        salt
        + (4096).to_bytes(4, "big")
        + bytes([65])
        + server_public
        + AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    )


class PublicHttpsConnection(http.client.HTTPSConnection):
    def __init__(self, host: str) -> None:
        super().__init__(host, timeout=10)
        self.deadline = time.monotonic() + 20

    def remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("push send deadline exceeded")
        return min(10, remaining)

    def connect(self) -> None:
        # Resolve once, reject the entire mixed/private answer set, and connect to that IP.
        addresses = socket.getaddrinfo(self.host, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise OSError("nonpublic push destination")
        raw = socket.create_connection((str(addresses[0][4][0]), 443), timeout=self.remaining())
        try:
            raw.settimeout(self.remaining())
            self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=self.host)
            self.sock.settimeout(self.remaining())
        except BaseException:
            raw.close()
            raise


def post_public(endpoint: str, body: bytes, headers: dict[str, str]) -> int:
    parsed = urlsplit(endpoint)
    connection = PublicHttpsConnection(parsed.hostname or "")
    try:
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        connection.request("POST", path, body=body, headers=headers)
        response = connection.getresponse()
        # Never read/log a provider body or follow a redirect; it can contain credentials.
        return response.status
    finally:
        connection.close()


async def send_web_push(
    subscription: BrowserSubscription,
    signal: dict[str, object],
    settings: Settings,
    transport: WebPushTransport,
) -> str:
    key = check_configuration(settings)
    endpoint = validate_subscription(subscription, settings)
    token = jwt.encode(
        {
            "aud": origin(endpoint),
            "exp": utc_now() + timedelta(hours=12),
            "sub": settings.customer_vapid_subject,
        },
        key,
        algorithm="ES256",
    )
    public = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    headers = {
        "Authorization": f"vapid t={token},k={b64(public)}",
        "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream",
        "TTL": "300",
    }
    try:
        status = await transport.post(
            endpoint, encrypt_payload(subscription, canonical(signal)), headers
        )
    except (OSError, http.client.HTTPException, asyncio.TimeoutError):
        return "RETRY"
    if status in (201, 202):
        return "SENT"
    if status in (404, 410):
        return "INVALID_TOKEN"
    if status in (400, 413):
        return "PERMANENT_FAILURE"
    # Authentication/rate limit/provider errors retain retry debt, never revoke valid tokens.
    return "RETRY"
