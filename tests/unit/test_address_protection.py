import base64
import json

import pytest
from pydantic import SecretStr

from tirodhan.core.config import Settings
from tirodhan.main import create_app
from tirodhan.modules.customers.address_protection import (
    AddressDecryptionError,
    AddressProtectionConfigurationError,
    AesGcmAddressProtector,
)
from tirodhan.modules.customers.ports import AddressProtectionNotConfiguredError
from tirodhan.modules.identity.phone_protection import AesGcmPhoneIdentityProtector

ADDRESS = "123 Test Street, Test City"


def protector(active: str = "old") -> AesGcmAddressProtector:
    return AesGcmAddressProtector(
        active_key_id=active,
        encryption_keys={"old": b"a" * 32, "new": b"b" * 32},
    )


@pytest.mark.asyncio
async def test_address_encryption_nonce_rotation() -> None:
    old = protector()
    first, second = await old.protect(ADDRESS), await old.protect(ADDRESS)
    assert first != second
    assert ADDRESS.encode() not in first
    assert await old.unprotect(first) == ADDRESS

    rotated = protector("new")
    assert await rotated.unprotect(first) == ADDRESS
    assert await old.unprotect(await rotated.protect(ADDRESS)) == ADDRESS


@pytest.mark.asyncio
async def test_address_envelope_fails_closed() -> None:
    implementation = protector()
    original = await implementation.protect(ADDRESS)

    unknown = AesGcmAddressProtector(
        active_key_id="new",
        encryption_keys={"new": b"b" * 32},
    )
    with pytest.raises(AddressDecryptionError):
        await unknown.unprotect(original)

    for damaged in (
        b"",
        original[:10],
        b"TAD\x02" + original[4:],  # Wrong magic version
        original[:-1] + bytes([original[-1] ^ 1]),  # Tampered ciphertext
        original + b"extra",
    ):
        with pytest.raises(AddressDecryptionError):
            await implementation.unprotect(damaged)


@pytest.mark.parametrize(
    "active,keys",
    [
        ("absent", {"a": b"a" * 32}),
        ("a", {"a": b"a" * 31}),
        ("", {"": b"a" * 32}),
    ],
)
def test_address_keys_must_be_independent_256_bit_material(active, keys) -> None:
    with pytest.raises(AddressProtectionConfigurationError):
        AesGcmAddressProtector(
            active_key_id=active,
            encryption_keys=keys,
        )


@pytest.mark.parametrize(
    "keyring",
    [
        "not-json",
        "[]",
        '{"a": 1}',
        '{"a": "not-base64!"}',
        json.dumps({"a": base64.b64encode(b"a" * 31).decode()}),
    ],
)
def test_malformed_address_runtime_configuration_fails_fast(keyring) -> None:
    with pytest.raises(AddressProtectionConfigurationError):
        create_app(
            Settings(
                _env_file=None,
                environment="test",
                address_encryption_active_key_id="a",
                address_encryption_keys=SecretStr(keyring),
            )
        )


@pytest.mark.asyncio
async def test_missing_address_configuration_has_no_plaintext_fallback() -> None:
    app = create_app(Settings(_env_file=None, environment="test"))
    with pytest.raises(AddressProtectionNotConfiguredError):
        await app.state.address_protector.protect(ADDRESS)


@pytest.mark.asyncio
async def test_domain_separation_between_phone_and_address() -> None:
    address_prot = protector()
    phone_prot = AesGcmPhoneIdentityProtector(
        active_key_id="old",
        encryption_keys={"old": b"a" * 32},
        lookup_hmac_key=b"h" * 32,
    )

    address_ct = await address_prot.protect(ADDRESS)
    phone_ct = await phone_prot.protect("+919876543210")

    # Address ciphertext fails in phone protector
    from tirodhan.modules.identity.phone_protection import PhoneDecryptionError

    with pytest.raises(PhoneDecryptionError):
        await phone_prot.unprotect(address_ct)

    # Phone ciphertext fails in address protector
    with pytest.raises(AddressDecryptionError):
        await address_prot.unprotect(phone_ct)


@pytest.mark.asyncio
async def test_injected_address_protector_remains() -> None:
    custom = protector()
    app = create_app(Settings(_env_file=None, environment="test"), address_protector=custom)
    assert app.state.address_protector is custom


def test_successful_runtime_wiring() -> None:
    valid_keyring = json.dumps({"v1": base64.b64encode(b"a" * 32).decode()})
    app = create_app(
        Settings(
            _env_file=None,
            environment="test",
            address_encryption_active_key_id="v1",
            address_encryption_keys=SecretStr(valid_keyring),
        )
    )
    assert isinstance(app.state.address_protector, AesGcmAddressProtector)
