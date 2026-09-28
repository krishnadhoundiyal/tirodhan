from datetime import datetime, timedelta, timezone

import pytest

from tirodhan.modules.evidence.service import (
    TARGET_HANDOVER,
    TARGET_PICKUP,
    InvalidEvidenceCaptureTimestampError,
    InvalidEvidenceTargetKindError,
    _normalize_captured_at,
    _require_target_kind,
)


def test_evidence_target_kind_vocabulary_is_bounded() -> None:
    _require_target_kind(TARGET_PICKUP)
    _require_target_kind(TARGET_HANDOVER)
    with pytest.raises(InvalidEvidenceTargetKindError):
        _require_target_kind("MEDIA")


def test_captured_at_is_normalized_to_utc_and_requires_an_offset() -> None:
    claimed = datetime(2026, 9, 28, 18, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    assert _normalize_captured_at(claimed) == datetime(
        2026,
        9,
        28,
        13,
        0,
        tzinfo=timezone.utc,
    )
    with pytest.raises(InvalidEvidenceCaptureTimestampError):
        _normalize_captured_at(datetime(2026, 9, 28, 13, 0))
