from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Any, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from geoalchemy2.shape import to_shape
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.api.dependencies import (
    get_address_protector,
    get_database_session,
    get_media_policy,
    get_media_storage,
    get_session_factory,
    require_role,
)
from tirodhan.api.routes.addresses import LocationInput
from tirodhan.core.config import Settings
from tirodhan.db.values import utc_now
from tirodhan.modules.customers.ports import (
    AddressProtectionNotConfiguredError,
    AddressProtector,
)
from tirodhan.modules.customers.service import GeoPoint
from tirodhan.modules.dispatch.devices import register_push_device, revoke_push_device
from tirodhan.modules.dispatch.models import AssignmentOffer, RiderAssignment, RiderAvailability
from tirodhan.modules.dispatch.queries import (
    ActiveAssignmentRead,
    OperationalReadStateError,
    get_active_assignment,
    get_rider_availability,
    list_actionable_offers,
)
from tirodhan.modules.dispatch.service import (
    AssignmentOfferConflictError,
    AssignmentOfferExpiredError,
    AssignmentOfferNotFoundError,
    GroupAlreadyAssignedError,
    PickupGroupNotAssignableError,
    RiderAvailabilityVersionConflictError,
    RiderNotEligibleError,
    RiderNotFoundError,
    accept_assignment_offer,
    set_rider_availability_intent,
)
from tirodhan.modules.dispatch.service import (
    AssignmentStateInconsistentError as DispatchStateInconsistentError,
)
from tirodhan.modules.evidence.media_policy import MediaPolicyNotConfiguredError
from tirodhan.modules.evidence.media_ports import (
    MediaPolicy,
    MediaStorageNotConfiguredError,
    MediaStoragePort,
    MediaStorageUnavailableError,
)
from tirodhan.modules.evidence.media_service import (
    InvalidMediaTypeError,
    MediaActorAttributionError,
    MediaAlreadyRegisteredError,
    MediaAssetNotFoundError,
    MediaContentTypePolicyError,
    MediaEvidenceNotFoundError,
    MediaLifecycleStateError,
    MediaObjectNotReadyError,
    MediaRegistrationInProgressError,
    MediaRegistrationStateInconsistentError,
    MediaSizePolicyError,
    MediaWriteAuthorizationUnavailableError,
    finalize_media_asset,
    register_media_asset,
    request_upload_authorization,
)
from tirodhan.modules.evidence.models import EvidenceCapture, MediaAsset
from tirodhan.modules.evidence.service import (
    EvidenceCaptureCommandInProgressError,
    EvidenceCaptureStateInconsistentError,
    EvidenceHandoverAttributionError,
    EvidenceHandoverNotFoundError,
    EvidencePickupAttributionError,
    EvidencePickupNotCollectedError,
    EvidencePickupNotFoundError,
    InvalidEvidenceCaptureTimestampError,
    InvalidEvidenceTargetKindError,
    record_evidence_capture,
)
from tirodhan.modules.handovers.models import HandoverEvent
from tirodhan.modules.handovers.service import (
    HandoverCommandInProgressError,
    HandoverPickupNotCollectedError,
    HandoverPickupNotFoundError,
    HandoverRiderAttributionError,
    HandoverStateInconsistentError,
    InvalidHandoverCommandError,
    PickupAlreadyHandedOverError,
    ReceivingPointInactiveError,
    ReceivingPointNotFoundError,
    record_handover,
)
from tirodhan.modules.identity.service import ROLE_RIDER, AuthenticatedPrincipal
from tirodhan.modules.operations.service import (
    InvalidIncidentReasonError,
    PickupIncidentAttributionError,
    PickupIncidentConflictError,
    PickupIncidentNotFoundError,
    PickupIncidentStateError,
    open_pickup_incident,
)
from tirodhan.modules.pickups.models import PickupAttempt, PickupIncident
from tirodhan.modules.pickups.service import (
    AssignmentNotFoundError,
    AssignmentNotStartableError,
    AssignmentStateInconsistentError,
    AssignmentWrongRiderError,
    InvalidPickupOutcomeError,
    PickupAttemptReplayConflictError,
    PickupExecutionNotFoundError,
    PickupNotAttemptableError,
    PickupOwnershipMismatchError,
    RiderWorkStateInvalidError,
    record_pickup_attempt,
    start_assignment,
)
from tirodhan.modules.receiving_points.models import ReceivingPoint
from tirodhan.modules.receiving_points.queries import list_active_receiving_points
from tirodhan.modules.reliability.primitives import IdempotencyKeyConflictError

router = APIRouter(prefix="/v1/rider", tags=["rider"])


class RiderCommandModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RiderAvailabilityResponse(BaseModel):
    rider_id: UUID
    availability_intent: str
    work_state: str
    version: int
    updated_at: datetime


class RiderAvailabilityUpdate(RiderCommandModel):
    intent: Literal["OFFLINE", "AVAILABLE"]
    expected_version: int = Field(ge=1)


class AssignmentOfferResponse(BaseModel):
    offer_id: UUID
    collection_group_id: UUID
    offer_round: int
    status: str
    offered_at: datetime
    expires_at: datetime


class AssignmentSummaryResponse(BaseModel):
    assignment_id: UUID
    collection_group_id: UUID
    rider_id: UUID
    source: str
    status: str
    assigned_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class DeclaredItemResponse(BaseModel):
    item_category_code: str
    declared_quantity: int | None
    declared_weight_grams: int | None


class AssignmentPickupResponse(BaseModel):
    pickup_execution_id: UUID
    request_id: UUID
    status: str
    slot_start: datetime
    slot_end: datetime
    pickup_address: str
    pickup_latitude: float
    pickup_longitude: float
    declared_items: list[DeclaredItemResponse]


class ActiveAssignmentResponse(BaseModel):
    assignment_id: UUID
    collection_group_id: UUID
    source: str
    status: str
    assigned_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    pickups: list[AssignmentPickupResponse]


class ActiveAssignmentEnvelope(BaseModel):
    assignment: ActiveAssignmentResponse | None


class PickupAttemptRequest(RiderCommandModel):
    client_attempt_id: UUID
    outcome: Literal["COLLECTED", "NOT_COLLECTED"]


class PickupAttemptResponse(BaseModel):
    pickup_attempt_id: UUID
    pickup_execution_id: UUID
    rider_assignment_id: UUID
    client_attempt_id: UUID
    attempt_number: int
    outcome: str
    attempted_at: datetime


class PickupIncidentRequest(RiderCommandModel):
    client_incident_id: UUID
    reason_code: Literal[
        "CUSTOMER_UNAVAILABLE",
        "ADDRESS_NOT_FOUND",
        "ACCESS_BLOCKED",
        "RIDER_UNABLE_TO_REACH",
        "RIDER_UNABLE_TO_CONTINUE",
        "OTHER",
    ]


class PickupIncidentResponse(BaseModel):
    incident_id: UUID
    client_incident_id: UUID
    pickup_execution_id: UUID
    rider_assignment_id: UUID
    reason_code: str
    status: str
    opened_at: datetime


class ReceivingPointResponse(BaseModel):
    receiving_point_id: UUID
    official_name: str
    official_identifier: str | None
    latitude: float
    longitude: float
    allowed_radius_m: int
    version: int


class HandoverRequest(RiderCommandModel):
    client_handover_id: UUID
    receiving_point_id: UUID
    pickup_execution_ids: list[UUID]
    observed_location: LocationInput


class HandoverResponse(BaseModel):
    handover_event_id: UUID
    client_handover_id: UUID
    receiving_point_id: UUID
    occurred_at: datetime
    status: str
    validation_code: str
    distance_m: float
    evaluated_at: datetime


class EvidenceCaptureRequest(RiderCommandModel):
    client_capture_id: UUID
    target_kind: Literal["PICKUP", "HANDOVER"]
    target_id: UUID
    captured_at: datetime


class EvidenceCaptureResponse(BaseModel):
    evidence_capture_id: UUID
    client_capture_id: UUID
    target_kind: str
    target_id: UUID
    captured_at: datetime
    created_at: datetime


class MediaRegistrationRequest(RiderCommandModel):
    client_media_id: UUID
    evidence_capture_id: UUID
    media_type: Literal["PHOTO", "VIDEO"]
    expected_content_type: str = Field(min_length=1, max_length=255)


class MediaAssetResponse(BaseModel):
    media_asset_id: UUID
    client_media_id: UUID
    evidence_capture_id: UUID
    media_type: str
    object_key: str
    expected_content_type: str
    stored_content_type: str | None
    size_bytes: int | None
    upload_status: str
    created_at: datetime
    finalized_at: datetime | None


class UploadAuthorizationResponse(BaseModel):
    authorization: str
    expires_at: datetime | None


def _availability_response(value: RiderAvailability) -> RiderAvailabilityResponse:
    return RiderAvailabilityResponse(
        rider_id=value.rider_id,
        availability_intent=value.availability_intent,
        work_state=value.work_state,
        version=value.version,
        updated_at=value.updated_at,
    )


def _offer_response(value: AssignmentOffer) -> AssignmentOfferResponse:
    return AssignmentOfferResponse(
        offer_id=value.offer_id,
        collection_group_id=value.collection_group_id,
        offer_round=value.offer_round,
        status=value.status,
        offered_at=value.offered_at,
        expires_at=value.expires_at,
    )


def _assignment_summary(value: RiderAssignment) -> AssignmentSummaryResponse:
    return AssignmentSummaryResponse(
        assignment_id=value.assignment_id,
        collection_group_id=value.collection_group_id,
        rider_id=value.rider_id,
        source=value.source,
        status=value.status,
        assigned_at=value.assigned_at,
        started_at=value.started_at,
        completed_at=value.completed_at,
    )


def _attempt_response(value: PickupAttempt) -> PickupAttemptResponse:
    return PickupAttemptResponse(
        pickup_attempt_id=value.pickup_attempt_id,
        pickup_execution_id=value.pickup_execution_id,
        rider_assignment_id=value.rider_assignment_id,
        client_attempt_id=value.client_attempt_id,
        attempt_number=value.attempt_number,
        outcome=value.outcome,
        attempted_at=value.attempted_at,
    )


def _incident_response(value: PickupIncident) -> PickupIncidentResponse:
    return PickupIncidentResponse(
        incident_id=value.incident_id,
        client_incident_id=value.client_incident_id,
        pickup_execution_id=value.pickup_execution_id,
        rider_assignment_id=value.rider_assignment_id,
        reason_code=value.reason_code,
        status=value.status,
        opened_at=value.opened_at,
    )


def _handover_response(value: HandoverEvent) -> HandoverResponse:
    return HandoverResponse(
        handover_event_id=value.handover_event_id,
        client_handover_id=value.client_handover_id,
        receiving_point_id=value.receiving_point_id,
        occurred_at=value.occurred_at,
        status=value.status,
        validation_code=value.validation_code,
        distance_m=value.distance_m,
        evaluated_at=value.evaluated_at,
    )


def _evidence_response(
    value: EvidenceCapture,
    *,
    target_kind: str,
    target_id: UUID,
) -> EvidenceCaptureResponse:
    return EvidenceCaptureResponse(
        evidence_capture_id=value.evidence_capture_id,
        client_capture_id=value.client_capture_id,
        target_kind=target_kind,
        target_id=target_id,
        captured_at=value.captured_at,
        created_at=value.created_at,
    )


def _media_response(value: MediaAsset) -> MediaAssetResponse:
    return MediaAssetResponse(
        media_asset_id=value.media_asset_id,
        client_media_id=value.client_media_id,
        evidence_capture_id=value.evidence_capture_id,
        media_type=value.media_type,
        object_key=value.object_key,
        expected_content_type=value.expected_content_type,
        stored_content_type=value.stored_content_type,
        size_bytes=value.size_bytes,
        upload_status=value.upload_status,
        created_at=value.created_at,
        finalized_at=value.finalized_at,
    )


def _idempotency_expiry(request: Request) -> datetime:
    settings = cast(Settings, request.app.state.settings)
    seconds = settings.command_idempotency_ttl_seconds
    if seconds is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Command idempotency retention is not configured",
        )
    return utc_now() + timedelta(seconds=seconds)


def _conflict(error: Exception) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found")


async def _active_assignment_response(
    value: ActiveAssignmentRead,
    protector: AddressProtector,
) -> ActiveAssignmentResponse:
    pickups: list[AssignmentPickupResponse] = []
    try:
        for pickup in value.pickups:
            shape = cast(Any, to_shape(pickup.pickup_location))
            pickups.append(
                AssignmentPickupResponse(
                    pickup_execution_id=pickup.pickup_execution_id,
                    request_id=pickup.request_id,
                    status=pickup.status,
                    slot_start=pickup.slot_start,
                    slot_end=pickup.slot_end,
                    pickup_address=await protector.unprotect(
                        pickup.pickup_address_snapshot_encrypted
                    ),
                    pickup_latitude=shape.y,
                    pickup_longitude=shape.x,
                    declared_items=[
                        DeclaredItemResponse(
                            item_category_code=item.item_category_code,
                            declared_quantity=item.declared_quantity,
                            declared_weight_grams=item.declared_weight_grams,
                        )
                        for item in pickup.items
                    ],
                )
            )
    except AddressProtectionNotConfiguredError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Address protection is not configured",
        ) from error
    assignment = value.assignment
    return ActiveAssignmentResponse(
        assignment_id=assignment.assignment_id,
        collection_group_id=assignment.collection_group_id,
        source=assignment.source,
        status=assignment.status,
        assigned_at=assignment.assigned_at,
        started_at=assignment.started_at,
        completed_at=assignment.completed_at,
        pickups=pickups,
    )


@router.get("/me/availability", response_model=RiderAvailabilityResponse)
async def get_my_availability(
    principal: Annotated[AuthenticatedPrincipal, Depends(require_role(ROLE_RIDER))],
    session: Annotated[AsyncSession, Depends(get_database_session)],
) -> RiderAvailabilityResponse:
    availability = await get_rider_availability(session, rider_id=principal.user_id)
    if availability is None:
        raise _not_found()
    return _availability_response(availability)


@router.put("/me/availability", response_model=RiderAvailabilityResponse)
async def put_my_availability(
    body: RiderAvailabilityUpdate,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_role(ROLE_RIDER))],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> RiderAvailabilityResponse:
    try:
        return _availability_response(
            await set_rider_availability_intent(
                session_factory,
                rider_id=principal.user_id,
                intent=body.intent,
                expected_version=body.expected_version,
            )
        )
    except RiderNotFoundError as error:
        raise _not_found() from error
    except RiderAvailabilityVersionConflictError as error:
        raise _conflict(error) from error


@router.get("/me/offers", response_model=list[AssignmentOfferResponse])
async def get_my_offers(
    principal: Annotated[AuthenticatedPrincipal, Depends(require_role(ROLE_RIDER))],
    session: Annotated[AsyncSession, Depends(get_database_session)],
) -> list[AssignmentOfferResponse]:
    return [
        _offer_response(value)
        for value in await list_actionable_offers(
            session,
            rider_id=principal.user_id,
            now=utc_now(),
            limit=100,
        )
    ]


@router.post("/offers/{offer_id}/accept", response_model=AssignmentSummaryResponse)
async def post_accept_offer(
    offer_id: UUID,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_role(ROLE_RIDER))],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> AssignmentSummaryResponse:
    try:
        return _assignment_summary(
            await accept_assignment_offer(
                session_factory,
                offer_id=offer_id,
                rider_id=principal.user_id,
            )
        )
    except (AssignmentOfferNotFoundError, AssignmentOfferConflictError) as error:
        raise _not_found() from error
    except (
        AssignmentOfferExpiredError,
        DispatchStateInconsistentError,
        GroupAlreadyAssignedError,
        PickupGroupNotAssignableError,
        RiderNotEligibleError,
        RiderNotFoundError,
        IntegrityError,
    ) as error:
        raise _conflict(error) from error


@router.get("/me/assignment", response_model=ActiveAssignmentEnvelope)
async def get_my_assignment(
    principal: Annotated[AuthenticatedPrincipal, Depends(require_role(ROLE_RIDER))],
    session: Annotated[AsyncSession, Depends(get_database_session)],
    protector: Annotated[AddressProtector, Depends(get_address_protector)],
) -> ActiveAssignmentEnvelope:
    try:
        assignment = await get_active_assignment(session, rider_id=principal.user_id)
    except OperationalReadStateError as error:
        raise _conflict(error) from error
    return ActiveAssignmentEnvelope(
        assignment=(
            await _active_assignment_response(assignment, protector)
            if assignment is not None
            else None
        )
    )


@router.post(
    "/assignments/{assignment_id}/start",
    response_model=AssignmentSummaryResponse,
)
async def post_start_assignment(
    assignment_id: UUID,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_role(ROLE_RIDER))],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> AssignmentSummaryResponse:
    try:
        return _assignment_summary(
            await start_assignment(
                session_factory,
                assignment_id=assignment_id,
                rider_id=principal.user_id,
            )
        )
    except (AssignmentNotFoundError, AssignmentWrongRiderError) as error:
        raise _not_found() from error
    except (
        AssignmentNotStartableError,
        AssignmentStateInconsistentError,
        RiderWorkStateInvalidError,
    ) as error:
        raise _conflict(error) from error


@router.post(
    "/pickups/{pickup_execution_id}/attempts",
    response_model=PickupAttemptResponse,
)
async def post_pickup_attempt(
    pickup_execution_id: UUID,
    body: PickupAttemptRequest,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_role(ROLE_RIDER))],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> PickupAttemptResponse:
    try:
        return _attempt_response(
            await record_pickup_attempt(
                session_factory,
                pickup_execution_id=pickup_execution_id,
                rider_id=principal.user_id,
                client_attempt_id=body.client_attempt_id,
                outcome=body.outcome,
            )
        )
    except InvalidPickupOutcomeError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except (
        AssignmentWrongRiderError,
        PickupExecutionNotFoundError,
        PickupOwnershipMismatchError,
    ) as error:
        raise _not_found() from error
    except (
        AssignmentNotStartableError,
        AssignmentStateInconsistentError,
        PickupAttemptReplayConflictError,
        PickupNotAttemptableError,
        RiderWorkStateInvalidError,
        IntegrityError,
    ) as error:
        raise _conflict(error) from error


@router.post(
    "/pickups/{pickup_execution_id}/incidents",
    response_model=PickupIncidentResponse,
)
async def post_pickup_incident(
    pickup_execution_id: UUID,
    body: PickupIncidentRequest,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_role(ROLE_RIDER))],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> PickupIncidentResponse:
    try:
        return _incident_response(
            await open_pickup_incident(
                session_factory,
                pickup_execution_id=pickup_execution_id,
                rider_id=principal.user_id,
                client_incident_id=body.client_incident_id,
                reason_code=body.reason_code,
            )
        )
    except InvalidIncidentReasonError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except (PickupIncidentNotFoundError, PickupIncidentAttributionError) as error:
        raise _not_found() from error
    except (PickupIncidentConflictError, PickupIncidentStateError, IntegrityError) as error:
        raise _conflict(error) from error


@router.get("/receiving-points", response_model=list[ReceivingPointResponse])
async def get_receiving_points(
    _principal: Annotated[AuthenticatedPrincipal, Depends(require_role(ROLE_RIDER))],
    session: Annotated[AsyncSession, Depends(get_database_session)],
) -> list[ReceivingPointResponse]:
    return [
        _receiving_point_response(value) for value in await list_active_receiving_points(session)
    ]


def _receiving_point_response(value: ReceivingPoint) -> ReceivingPointResponse:
    shape = cast(Any, to_shape(value.location))
    return ReceivingPointResponse(
        receiving_point_id=value.receiving_point_id,
        official_name=value.official_name,
        official_identifier=value.official_identifier,
        latitude=shape.y,
        longitude=shape.x,
        allowed_radius_m=value.allowed_radius_m,
        version=value.version,
    )


@router.post("/handovers", response_model=HandoverResponse)
async def post_handover(
    body: HandoverRequest,
    request: Request,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_role(ROLE_RIDER))],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> HandoverResponse:
    try:
        return _handover_response(
            await record_handover(
                session_factory,
                client_handover_id=body.client_handover_id,
                rider_id=principal.user_id,
                receiving_point_id=body.receiving_point_id,
                pickup_execution_ids=body.pickup_execution_ids,
                observed_location=GeoPoint(
                    latitude=body.observed_location.latitude,
                    longitude=body.observed_location.longitude,
                ),
                idempotency_expires_at=_idempotency_expiry(request),
            )
        )
    except InvalidHandoverCommandError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except (
        ReceivingPointNotFoundError,
        HandoverPickupNotFoundError,
        HandoverRiderAttributionError,
    ) as error:
        raise _not_found() from error
    except (
        IdempotencyKeyConflictError,
        HandoverCommandInProgressError,
        HandoverStateInconsistentError,
        ReceivingPointInactiveError,
        HandoverPickupNotCollectedError,
        PickupAlreadyHandedOverError,
        IntegrityError,
    ) as error:
        raise _conflict(error) from error


@router.post("/evidence-captures", response_model=EvidenceCaptureResponse)
async def post_evidence_capture(
    body: EvidenceCaptureRequest,
    request: Request,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_role(ROLE_RIDER))],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> EvidenceCaptureResponse:
    try:
        capture = await record_evidence_capture(
            session_factory,
            client_capture_id=body.client_capture_id,
            captured_by_user_id=principal.user_id,
            target_kind=body.target_kind,
            target_id=body.target_id,
            captured_at=body.captured_at,
            idempotency_expires_at=_idempotency_expiry(request),
        )
        return _evidence_response(
            capture,
            target_kind=body.target_kind,
            target_id=body.target_id,
        )
    except (InvalidEvidenceTargetKindError, InvalidEvidenceCaptureTimestampError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except (
        EvidencePickupNotFoundError,
        EvidencePickupAttributionError,
        EvidenceHandoverNotFoundError,
        EvidenceHandoverAttributionError,
    ) as error:
        raise _not_found() from error
    except (
        IdempotencyKeyConflictError,
        EvidenceCaptureCommandInProgressError,
        EvidenceCaptureStateInconsistentError,
        EvidencePickupNotCollectedError,
        IntegrityError,
    ) as error:
        raise _conflict(error) from error


@router.post("/media-assets", response_model=MediaAssetResponse)
async def post_media_asset(
    body: MediaRegistrationRequest,
    request: Request,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_role(ROLE_RIDER))],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    policy: Annotated[MediaPolicy, Depends(get_media_policy)],
) -> MediaAssetResponse:
    try:
        return _media_response(
            await register_media_asset(
                session_factory,
                client_media_id=body.client_media_id,
                evidence_capture_id=body.evidence_capture_id,
                requesting_user_id=principal.user_id,
                media_type=body.media_type,
                expected_content_type=body.expected_content_type,
                policy=policy,
                idempotency_expires_at=_idempotency_expiry(request),
            )
        )
    except (InvalidMediaTypeError, MediaContentTypePolicyError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except MediaPolicyNotConfiguredError as error:
        raise HTTPException(status_code=503, detail="Media policy is not configured") from error
    except (
        MediaActorAttributionError,
        MediaAssetNotFoundError,
        MediaEvidenceNotFoundError,
    ) as error:
        raise _not_found() from error
    except (
        IdempotencyKeyConflictError,
        MediaAlreadyRegisteredError,
        MediaRegistrationInProgressError,
        MediaRegistrationStateInconsistentError,
        IntegrityError,
    ) as error:
        raise _conflict(error) from error


@router.post(
    "/media-assets/{media_asset_id}/upload-authorization",
    response_model=UploadAuthorizationResponse,
)
async def post_media_upload_authorization(
    media_asset_id: UUID,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_role(ROLE_RIDER))],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    storage: Annotated[MediaStoragePort, Depends(get_media_storage)],
) -> UploadAuthorizationResponse:
    try:
        result = await request_upload_authorization(
            session_factory,
            media_asset_id=media_asset_id,
            requesting_user_id=principal.user_id,
            storage=storage,
        )
        return UploadAuthorizationResponse(
            authorization=result.opaque_value,
            expires_at=result.expires_at,
        )
    except (MediaStorageNotConfiguredError, MediaStorageUnavailableError) as error:
        raise HTTPException(status_code=503, detail="Media storage is unavailable") from error
    except (MediaAssetNotFoundError, MediaActorAttributionError) as error:
        raise _not_found() from error
    except MediaWriteAuthorizationUnavailableError as error:
        raise _conflict(error) from error


@router.post("/media-assets/{media_asset_id}/finalize", response_model=MediaAssetResponse)
async def post_finalize_media_asset(
    media_asset_id: UUID,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_role(ROLE_RIDER))],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    storage: Annotated[MediaStoragePort, Depends(get_media_storage)],
    policy: Annotated[MediaPolicy, Depends(get_media_policy)],
) -> MediaAssetResponse:
    try:
        return _media_response(
            await finalize_media_asset(
                session_factory,
                media_asset_id=media_asset_id,
                requesting_user_id=principal.user_id,
                storage=storage,
                policy=policy,
            )
        )
    except (
        MediaStorageNotConfiguredError,
        MediaStorageUnavailableError,
        MediaPolicyNotConfiguredError,
    ) as error:
        raise HTTPException(status_code=503, detail="Media runtime is unavailable") from error
    except (MediaAssetNotFoundError, MediaActorAttributionError) as error:
        raise _not_found() from error
    except (
        MediaContentTypePolicyError,
        MediaSizePolicyError,
        MediaObjectNotReadyError,
        MediaLifecycleStateError,
    ) as error:
        raise _conflict(error) from error


class PushDeviceRequest(RiderCommandModel):
    client_device_id: str = Field(min_length=1, max_length=200)
    platform: Literal["ANDROID", "IOS"]
    registration_token: str = Field(min_length=1, max_length=4096, repr=False)


class PushDeviceResponse(BaseModel):
    push_registration_id: UUID
    client_device_id: str
    provider: str
    platform: str


@router.post("/me/push-devices", response_model=PushDeviceResponse)
async def post_push_device(
    body: PushDeviceRequest,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_role(ROLE_RIDER))],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> PushDeviceResponse:
    try:
        registration = await register_push_device(
            session_factory,
            rider_id=principal.user_id,
            client_device_id=body.client_device_id,
            platform=body.platform,
            registration_token=body.registration_token,
        )
    except RiderNotFoundError as error:
        raise _not_found() from error
    except RiderNotEligibleError as error:
        raise _conflict(error) from error
    return PushDeviceResponse(
        push_registration_id=registration.push_registration_id,
        client_device_id=registration.client_device_id,
        provider=registration.provider,
        platform=registration.platform,
    )


@router.delete("/me/push-devices/{client_device_id}", status_code=204)
async def delete_push_device(
    client_device_id: str,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_role(ROLE_RIDER))],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> None:
    try:
        await revoke_push_device(
            session_factory, rider_id=principal.user_id, client_device_id=client_device_id
        )
    except RiderNotFoundError as error:
        raise _not_found() from error
    except RiderNotEligibleError as error:
        raise _conflict(error) from error
