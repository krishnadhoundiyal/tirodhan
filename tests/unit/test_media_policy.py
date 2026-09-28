import pytest

from tirodhan.modules.evidence.media_policy import (
    ConfiguredMediaPolicy,
    MediaPolicyNotConfiguredError,
)


def test_configured_media_policy_uses_explicit_type_specific_values() -> None:
    policy = ConfiguredMediaPolicy(
        allowed_content_types={"PHOTO": {"image/example"}, "VIDEO": {"video/example"}},
        maximum_size_bytes={"PHOTO": 100, "VIDEO": 200},
    )

    assert policy.is_content_type_allowed("PHOTO", "image/example")
    assert not policy.is_content_type_allowed("PHOTO", "image/other")
    assert policy.max_size_bytes("PHOTO") == 100
    assert policy.max_size_bytes("VIDEO") == 200


def test_media_policy_fails_explicitly_when_type_configuration_is_missing() -> None:
    policy = ConfiguredMediaPolicy(
        allowed_content_types={},
        maximum_size_bytes={},
    )

    with pytest.raises(MediaPolicyNotConfiguredError):
        policy.is_content_type_allowed("PHOTO", "image/example")
    with pytest.raises(MediaPolicyNotConfiguredError):
        policy.max_size_bytes("PHOTO")


def test_media_policy_rejects_nonpositive_configured_size_limits() -> None:
    with pytest.raises(ValueError, match="positive"):
        ConfiguredMediaPolicy(
            allowed_content_types={"PHOTO": {"image/example"}},
            maximum_size_bytes={"PHOTO": 0},
        )
