from unittest.mock import AsyncMock, patch

import pytest

from tirodhan.core.config import Settings
from tirodhan.main import _media_policy, _media_storage, create_app
from tirodhan.modules.evidence.azure_media import AzureBlobMediaStorage
from tirodhan.modules.evidence.media_policy import ConfiguredMediaPolicy, UnconfiguredMediaPolicy
from tirodhan.modules.evidence.media_ports import UnconfiguredMediaStoragePort


def test_media_storage_wiring_unconfigured() -> None:
    settings = Settings(
        media_blob_account_url=None,
        media_blob_container_name=None,
        media_upload_authorization_ttl_seconds=None,
    )
    storage = _media_storage(settings)
    assert isinstance(storage, UnconfiguredMediaStoragePort)


def test_media_storage_wiring_configured() -> None:
    settings = Settings(
        media_blob_account_url="https://test.blob.core.windows.net",
        media_blob_container_name="test-container",
        media_upload_authorization_ttl_seconds=600,
    )
    storage = _media_storage(settings)
    assert isinstance(storage, AzureBlobMediaStorage)


def test_media_storage_wiring_partial() -> None:
    settings = Settings(
        media_blob_account_url="https://test.blob.core.windows.net",
        media_blob_container_name=None,
        media_upload_authorization_ttl_seconds=600,
    )
    storage = _media_storage(settings)
    assert isinstance(storage, UnconfiguredMediaStoragePort)


def test_media_policy_wiring_unconfigured() -> None:
    settings = Settings(
        media_photo_allowed_content_types=None,
        media_photo_max_size_bytes=None,
        media_video_allowed_content_types=None,
        media_video_max_size_bytes=None,
    )
    policy = _media_policy(settings)
    assert isinstance(policy, UnconfiguredMediaPolicy)


@pytest.mark.asyncio
async def test_app_lifespan_closes_automatic_azure_runtime() -> None:
    settings = Settings(
        media_blob_account_url="https://test.blob.core.windows.net",
        media_blob_container_name="test-container",
        media_upload_authorization_ttl_seconds=600,
    )
    with patch("tirodhan.modules.evidence.azure_media.BlobServiceClient", autospec=True):
        app = create_app(settings)

    storage = app.state.media_storage
    assert isinstance(storage, AzureBlobMediaStorage)

    storage.close = AsyncMock()

    async with app.router.lifespan_context(app):
        pass

    storage.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_app_lifespan_does_not_close_injected_azure_runtime() -> None:
    settings = Settings(
        media_blob_account_url="https://test.blob.core.windows.net",
        media_blob_container_name="test-container",
        media_upload_authorization_ttl_seconds=600,
    )
    with patch("tirodhan.modules.evidence.azure_media.BlobServiceClient", autospec=True):
        injected_storage = AzureBlobMediaStorage(
            account_url="https://test.blob.core.windows.net",
            container_name="test-container",
            credential=AsyncMock(),
            authorization_ttl_seconds=600,
        )

    app = create_app(settings, media_storage=injected_storage)

    injected_storage.close = AsyncMock()

    async with app.router.lifespan_context(app):
        pass

    injected_storage.close.assert_not_called()


def test_media_policy_wiring_configured() -> None:
    settings = Settings(
        media_photo_allowed_content_types=["image/jpeg"],
        media_photo_max_size_bytes=1000,
        media_video_allowed_content_types=["video/mp4"],
        media_video_max_size_bytes=2000,
    )
    policy = _media_policy(settings)
    assert isinstance(policy, ConfiguredMediaPolicy)
    assert policy.is_content_type_allowed("PHOTO", "image/jpeg") is True
    assert policy.max_size_bytes("PHOTO") == 1000


def test_media_policy_wiring_partial() -> None:
    settings = Settings(
        media_photo_allowed_content_types=["image/jpeg"],
        media_photo_max_size_bytes=None,
        media_video_allowed_content_types=["video/mp4"],
        media_video_max_size_bytes=2000,
    )
    policy = _media_policy(settings)
    assert isinstance(policy, UnconfiguredMediaPolicy)
