from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class UploadAuthorization:
    opaque_value: str
    expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class StoredObjectProperties:
    content_type: str
    size_bytes: int


class MediaStoragePort(Protocol):
    async def create_upload_authorization(
        self,
        *,
        object_key: str,
        expected_content_type: str,
    ) -> UploadAuthorization: ...

    async def inspect_object(
        self,
        *,
        object_key: str,
    ) -> StoredObjectProperties | None: ...


class MediaPolicy(Protocol):
    def is_content_type_allowed(self, media_type: str, content_type: str) -> bool: ...

    def max_size_bytes(self, media_type: str) -> int: ...
