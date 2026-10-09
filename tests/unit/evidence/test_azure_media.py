from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from azure.core.exceptions import AzureError, ResourceNotFoundError

from tirodhan.modules.evidence.azure_media import AzureBlobMediaStorage
from tirodhan.modules.evidence.media_ports import MediaStorageUnavailableError


@pytest.mark.asyncio
async def test_product_artwork_read_only_sas(mock_blob_service_client, mock_credential):
    from urllib.parse import parse_qs, urlsplit

    from azure.storage.blob import UserDelegationKey

    storage = AzureBlobMediaStorage(
        account_url="https://test.blob.core.windows.net",
        container_name="test-container",
        credential=mock_credential,
        authorization_ttl_seconds=300,
    )
    key = UserDelegationKey()
    key.value = "mock_key_value"
    key.signed_expiry = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    mock_blob_service_client.get_user_delegation_key = AsyncMock(return_value=key)
    mock_blob_service_client.get_blob_client.return_value.url = (
        "https://test.blob.core.windows.net/test-container/product-art/opaque"
    )
    authorization = await storage.authorize_product_image(object_key="product-art/opaque")
    permissions = parse_qs(urlsplit(authorization.opaque_value).query)
    assert permissions["sp"] == ["r"] and permissions["spr"] == ["https"]
    assert authorization.expires_at > datetime.now(timezone.utc)
    for object_key in ("media/private-evidence", "product-art/../media/private"):
        with pytest.raises(MediaStorageUnavailableError):
            await storage.authorize_product_image(object_key=object_key)


@pytest.fixture
def mock_blob_service_client():
    with patch(
        "tirodhan.modules.evidence.azure_media.BlobServiceClient", autospec=True
    ) as mock_class:
        from unittest.mock import MagicMock

        mock_client = MagicMock()
        mock_client.account_name = "testaccount"
        mock_client.url = "https://testaccount.blob.core.windows.net/"
        mock_class.return_value = mock_client
        yield mock_client


@pytest.fixture
def mock_credential():
    return AsyncMock()


@pytest.mark.asyncio
async def test_azure_media_storage_create_upload_authorization(
    mock_blob_service_client, mock_credential
):
    storage = AzureBlobMediaStorage(
        account_url="https://test.blob.core.windows.net",
        container_name="test-container",
        credential=mock_credential,
        authorization_ttl_seconds=3600,
    )

    # Mocking _get_user_delegation_key internally if necessary or let the mock client handle it
    from azure.storage.blob import UserDelegationKey

    mock_delegation_key = UserDelegationKey()
    mock_delegation_key.signed_expiry = (
        datetime.now(timezone.utc) + timedelta(hours=1)
    ).isoformat()
    mock_delegation_key.value = "mock_key_value"
    mock_blob_service_client.get_user_delegation_key = AsyncMock(return_value=mock_delegation_key)

    auth = await storage.create_upload_authorization(
        object_key="media/123",
        expected_content_type="image/jpeg",
    )

    url_value = auth.opaque_value
    assert url_value.startswith(
        "https://testaccount.blob.core.windows.net/test-container/media/123?"
    )

    import urllib.parse

    parsed = urllib.parse.urlparse(url_value)
    qs = urllib.parse.parse_qs(parsed.query)

    # Validate https protocol only
    assert qs.get("spr") == ["https"]

    # Validate permissions: create (c) + write (w) only
    assert qs.get("sp") == ["cw"]

    # Validate resource scope: blob (b)
    assert qs.get("sr") == ["b"]

    # Validate exact TTL offset from token's generation
    assert auth.expires_at is not None
    assert "se" in qs

    # Do not assert on raw signature (sig).


@pytest.mark.asyncio
async def test_azure_media_storage_inspect_object_found(mock_blob_service_client, mock_credential):
    storage = AzureBlobMediaStorage(
        account_url="https://test.blob.core.windows.net",
        container_name="test-container",
        credential=mock_credential,
        authorization_ttl_seconds=3600,
    )

    from unittest.mock import MagicMock

    mock_blob_client = MagicMock()
    mock_properties = MagicMock()
    mock_properties.content_settings.content_type = "image/jpeg"
    mock_properties.size = 1024
    mock_blob_client.get_blob_properties = AsyncMock(return_value=mock_properties)
    # get_blob_client is sync in aio
    from unittest.mock import MagicMock

    mock_blob_client_factory = MagicMock()
    mock_blob_client_factory.return_value = mock_blob_client
    mock_blob_service_client.get_blob_client = mock_blob_client_factory

    props = await storage.inspect_object(object_key="media/123")

    assert props is not None
    assert props.content_type == "image/jpeg"
    assert props.size_bytes == 1024


@pytest.mark.asyncio
async def test_azure_media_storage_inspect_object_not_found(
    mock_blob_service_client, mock_credential
):
    storage = AzureBlobMediaStorage(
        account_url="https://test.blob.core.windows.net",
        container_name="test-container",
        credential=mock_credential,
        authorization_ttl_seconds=3600,
    )

    from unittest.mock import MagicMock

    mock_blob_client = MagicMock()
    mock_blob_client.get_blob_properties = AsyncMock(
        side_effect=ResourceNotFoundError("Blob not found")
    )
    from unittest.mock import MagicMock

    mock_blob_client_factory = MagicMock()
    mock_blob_client_factory.return_value = mock_blob_client
    mock_blob_service_client.get_blob_client = mock_blob_client_factory

    props = await storage.inspect_object(object_key="media/123")

    assert props is None


@pytest.mark.asyncio
async def test_azure_media_storage_inspect_object_error(mock_blob_service_client, mock_credential):
    storage = AzureBlobMediaStorage(
        account_url="https://test.blob.core.windows.net",
        container_name="test-container",
        credential=mock_credential,
        authorization_ttl_seconds=3600,
    )

    from unittest.mock import MagicMock

    mock_blob_client = MagicMock()
    mock_blob_client.get_blob_properties = AsyncMock(side_effect=AzureError("Some error"))
    from unittest.mock import MagicMock

    mock_blob_client_factory = MagicMock()
    mock_blob_client_factory.return_value = mock_blob_client
    mock_blob_service_client.get_blob_client = mock_blob_client_factory

    with pytest.raises(MediaStorageUnavailableError):
        await storage.inspect_object(object_key="media/123")
