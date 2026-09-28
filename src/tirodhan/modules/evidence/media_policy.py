from __future__ import annotations

from collections.abc import Collection, Mapping


class MediaPolicyNotConfiguredError(RuntimeError):
    pass


class ConfiguredMediaPolicy:
    def __init__(
        self,
        *,
        allowed_content_types: Mapping[str, Collection[str]],
        maximum_size_bytes: Mapping[str, int],
    ) -> None:
        self._allowed_content_types = {
            media_type: frozenset(content_types)
            for media_type, content_types in allowed_content_types.items()
        }
        self._maximum_size_bytes = dict(maximum_size_bytes)
        if any(limit <= 0 for limit in self._maximum_size_bytes.values()):
            raise ValueError("configured media size limits must be positive")

    def is_content_type_allowed(self, media_type: str, content_type: str) -> bool:
        allowed = self._allowed_content_types.get(media_type)
        if allowed is None:
            raise MediaPolicyNotConfiguredError(
                f"content-type policy is not configured for {media_type}"
            )
        return content_type in allowed

    def max_size_bytes(self, media_type: str) -> int:
        maximum = self._maximum_size_bytes.get(media_type)
        if maximum is None:
            raise MediaPolicyNotConfiguredError(f"size policy is not configured for {media_type}")
        return maximum
