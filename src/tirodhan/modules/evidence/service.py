from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.customers.service import command_fingerprint
from tirodhan.modules.dispatch.models import RiderAssignment, RiderAssignmentItem
from tirodhan.modules.evidence.models import (
    EvidenceCapture,
    HandoverEvidenceLink,
    PickupEvidenceLink,
)
from tirodhan.modules.handovers.models import HandoverEvent
from tirodhan.modules.planning.models import PickupExecution
from tirodhan.modules.reliability.primitives import (
    claim_idempotency_record,
    complete_idempotency_record,
    get_completed_idempotency_result,
)

TARGET_PICKUP = "PICKUP"
TARGET_HANDOVER = "HANDOVER"
EVIDENCE_CAPTURE_IDEMPOTENCY_SCOPE = "evidence.capture"
PICKUP_COLLECTED = "COLLECTED"


class EvidenceCaptureError(RuntimeError):
    pass


class InvalidEvidenceTargetKindError(ValueError):
    pass


class InvalidEvidenceCaptureTimestampError(ValueError):
    pass


class EvidenceCaptureCommandInProgressError(EvidenceCaptureError):
    pass


class EvidenceCaptureStateInconsistentError(EvidenceCaptureError):
    pass


class EvidencePickupNotFoundError(EvidenceCaptureError):
    pass


class EvidencePickupNotCollectedError(EvidenceCaptureError):
    pass


class EvidencePickupAttributionError(EvidenceCaptureError):
    pass


class EvidenceHandoverNotFoundError(EvidenceCaptureError):
    pass


class EvidenceHandoverAttributionError(EvidenceCaptureError):
    pass


async def record_evidence_capture(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    client_capture_id: UUID,
    captured_by_user_id: UUID,
    target_kind: str,
    target_id: UUID,
    captured_at: datetime,
    idempotency_expires_at: datetime,
    now: datetime | None = None,
) -> EvidenceCapture:
    normalized_captured_at = _normalize_captured_at(captured_at)
    fingerprint = command_fingerprint(
        {
            "captured_by_user_id": captured_by_user_id,
            "target_kind": target_kind,
            "target_id": target_id,
            "captured_at": normalized_captured_at.isoformat(),
        }
    )

    async with session_factory() as session, session.begin():
        claim = await claim_idempotency_record(
            session,
            scope=EVIDENCE_CAPTURE_IDEMPOTENCY_SCOPE,
            idempotency_key=str(client_capture_id),
            request_fingerprint=fingerprint,
            expires_at=idempotency_expires_at,
        )
        if not claim.created:
            result = get_completed_idempotency_result(claim.record)
            if result is None or result.resource_id is None:
                raise EvidenceCaptureCommandInProgressError(
                    "evidence capture command is in progress"
                )
            capture = await session.get(EvidenceCapture, result.resource_id)
            if capture is None or capture.client_capture_id != client_capture_id:
                raise EvidenceCaptureStateInconsistentError(
                    "completed evidence command references an invalid capture"
                )
            return capture

        _require_target_kind(target_kind)
        await _validate_fresh_target(
            session,
            target_kind=target_kind,
            target_id=target_id,
            captured_by_user_id=captured_by_user_id,
        )

        created_at = now or utc_now()
        capture = EvidenceCapture(
            evidence_capture_id=new_uuid7(),
            client_capture_id=client_capture_id,
            captured_by_user_id=captured_by_user_id,
            captured_at=normalized_captured_at,
            created_at=created_at,
        )
        session.add(capture)
        await session.flush([capture])

        if target_kind == TARGET_PICKUP:
            link: PickupEvidenceLink | HandoverEvidenceLink = PickupEvidenceLink(
                pickup_execution_id=target_id,
                evidence_capture_id=capture.evidence_capture_id,
            )
        else:
            link = HandoverEvidenceLink(
                handover_event_id=target_id,
                evidence_capture_id=capture.evidence_capture_id,
            )
        session.add(link)
        await session.flush([link])
        await complete_idempotency_record(
            session,
            claim.record,
            result_resource_id=capture.evidence_capture_id,
            result_status_code=None,
            completed_at=created_at,
        )
        return capture


def _normalize_captured_at(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidEvidenceCaptureTimestampError("captured_at must include a UTC offset")
    return value.astimezone(timezone.utc)


def _require_target_kind(target_kind: str) -> None:
    if target_kind not in {TARGET_PICKUP, TARGET_HANDOVER}:
        raise InvalidEvidenceTargetKindError("evidence target kind is not supported")


async def _validate_fresh_target(
    session: AsyncSession,
    *,
    target_kind: str,
    target_id: UUID,
    captured_by_user_id: UUID,
) -> None:
    if target_kind == TARGET_PICKUP:
        pickup = await session.get(PickupExecution, target_id)
        if pickup is None:
            raise EvidencePickupNotFoundError("pickup execution not found")
        if pickup.status != PICKUP_COLLECTED:
            raise EvidencePickupNotCollectedError("pickup evidence requires a COLLECTED pickup")
        attributed_assignment = await session.scalar(
            select(RiderAssignmentItem.assignment_id)
            .join(
                RiderAssignment,
                RiderAssignment.assignment_id == RiderAssignmentItem.assignment_id,
            )
            .where(
                RiderAssignmentItem.pickup_execution_id == target_id,
                RiderAssignmentItem.released_at.is_(None),
                RiderAssignment.rider_id == captured_by_user_id,
            )
        )
        if attributed_assignment is None:
            raise EvidencePickupAttributionError(
                "pickup is not historically attributed to the capturing rider"
            )
        return

    handover = await session.get(HandoverEvent, target_id)
    if handover is None:
        raise EvidenceHandoverNotFoundError("handover event not found")
    if handover.rider_id != captured_by_user_id:
        raise EvidenceHandoverAttributionError("handover event belongs to another capturing rider")
