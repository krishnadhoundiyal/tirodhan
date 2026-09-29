from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from azure.core.exceptions import AzureError, ResourceNotFoundError
from azure.identity.aio import DefaultAzureCredential
from azure.storage.blob import BlobSasPermissions, UserDelegationKey, generate_blob_sas
from azure.storage.blob.aio import BlobServiceClient

from tirodhan.modules.evidence.media_ports import (
    MediaStorageUnavailableError,
    StoredObjectProperties,
    UploadAuthorization,
)

logger = logging.getLogger(__name__)


class AzureBlobMediaStorage:
    def __init__(
        self,
        *,
        account_url: str,
        container_name: str,
        credential: DefaultAzureCredential,
        authorization_ttl_seconds: int,
    ) -> None:
        self._account_url = account_url
        self._container_name = container_name
        self._credential = credential
        self._authorization_ttl_seconds = authorization_ttl_seconds
        self._client = BlobServiceClient(account_url=self._account_url, credential=self._credential)
        self._cached_key: UserDelegationKey | None = None

    async def _get_user_delegation_key(self) -> UserDelegationKey:
        # Optional: Add simple in-memory key cache to avoid requesting new key for each SAS
        now = datetime.now(timezone.utc)
        required_duration = self._authorization_ttl_seconds + 300  # 5 minutes buffer

        # Key cache logic - parse signed_expiry (ISO 8601 format string)
        if self._cached_key and self._cached_key.signed_expiry:
            try:
                # signed_expiry is formatted like '2023-10-18T20:30:00Z'
                expiry_str = self._cached_key.signed_expiry.replace("Z", "+00:00")
                key_expiry = datetime.fromisoformat(expiry_str)
                if key_expiry > now + timedelta(seconds=required_duration):
                    return self._cached_key
            except (ValueError, TypeError):
                pass  # Fallback to fetching a new key

        try:
            key_start = now - timedelta(minutes=5)
            key_expiry = now + timedelta(
                seconds=required_duration + 3600
            )  # Fetch for at least 1 hr more
            self._cached_key = await self._client.get_user_delegation_key(
                key_start_time=key_start,
                key_expiry_time=key_expiry,
            )
            return self._cached_key
        except AzureError as e:
            logger.error("azure_storage_delegation_key_error", exc_info=e)
            raise MediaStorageUnavailableError("unable to acquire upload delegation key") from e

    async def create_upload_authorization(
        self,
        *,
        object_key: str,
        expected_content_type: str,
    ) -> UploadAuthorization:
        try:
            delegation_key = await self._get_user_delegation_key()

            now = datetime.now(timezone.utc)
            expiry = now + timedelta(seconds=self._authorization_ttl_seconds)

            account_name = self._client.account_name
            if account_name is None:
                raise MediaStorageUnavailableError("unable to resolve blob account name")

            sas_token = generate_blob_sas(
                account_name=account_name,
                container_name=self._container_name,
                blob_name=object_key,
                user_delegation_key=delegation_key,
                permission=BlobSasPermissions(create=True, write=True),
                expiry=expiry,
                protocol="https",
            )

            # Use https base URL.
            base_url = (
                self._client.url if self._client.url.endswith("/") else f"{self._client.url}/"
            )
            sas_url = f"{base_url}{self._container_name}/{object_key}?{sas_token}"

            return UploadAuthorization(
                opaque_value=sas_url,
                expires_at=expiry,
            )

        except AzureError as e:
            logger.error("azure_storage_upload_authorization_error", exc_info=e)
            raise MediaStorageUnavailableError("unable to create upload authorization") from e

    async def inspect_object(
        self,
        *,
        object_key: str,
    ) -> StoredObjectProperties | None:
        try:
            blob_client = self._client.get_blob_client(
                container=self._container_name, blob=object_key
            )
            properties = await blob_client.get_blob_properties()

            content_settings = properties.content_settings
            content_type = (content_settings.content_type if content_settings else "") or ""
            size_bytes = properties.size

            return StoredObjectProperties(
                content_type=content_type,
                size_bytes=size_bytes,
            )

        except ResourceNotFoundError:
            return None
        except AzureError as e:
            logger.error("azure_storage_inspect_error", exc_info=e)
            raise MediaStorageUnavailableError("unable to inspect storage object") from e

    async def close(self) -> None:
        await self._client.close()
        await self._credential.close()
