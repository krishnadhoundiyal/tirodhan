from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hmac import compare_digest
from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.db.values import new_uuid7, utc_now
from tirodhan.modules.collection_requests.models import (
    CollectionRequest,
    CollectionRequestItem,
)
from tirodhan.modules.collection_requests.ports import (
    DeclaredRequestItem,
    PricingPort,
    PricingQuote,
)
from tirodhan.modules.collection_requests.scheduling import (
    SlotAvailabilityPort,
    UnconfiguredSlotAvailability,
    evaluate_slot,
    validate_slot,
)
from tirodhan.modules.customer_reads.errors import CustomerReadError
from tirodhan.modules.customer_reads.models import CatalogueCategory, CatalogueGroup
from tirodhan.modules.customers.ports import AddressProtector, UnconfiguredAddressProtector
from tirodhan.modules.customers.service import (
    IdempotencyCommandInProgressError,
    command_fingerprint,
)
from tirodhan.modules.evidence.models import (
    EvidenceCapture,
    HandoverEvidenceLink,
    PickupEvidenceLink,
)
from tirodhan.modules.handovers.models import HandoverEvent, HandoverEventItem
from tirodhan.modules.payments.models import Payment
from tirodhan.modules.planning.models import PickupExecution
from tirodhan.modules.reliability.models import IdempotencyRecord
from tirodhan.modules.reliability.primitives import (
    IdempotencyKeyConflictError,
    claim_idempotency_record,
    complete_idempotency_record,
    get_completed_idempotency_result,
)
from tirodhan.modules.serviceability.h3_cells import H3CellIdDeriver
from tirodhan.modules.serviceability.models import ServiceabilityContext
from tirodhan.modules.serviceability.ports import (
    CellIdDeriver,
    LocationResolver,
    UnconfiguredLocationResolver,
)
from tirodhan.modules.serviceability.service import (
    SERVICEABILITY_PENDING,
    SERVICEABILITY_SERVICEABLE,
    resolve_serviceability,
)

REQUEST_PENDING_PAYMENT = "PENDING_PAYMENT"
REQUEST_ACCEPTED = "ACCEPTED"
REQUEST_PRE_PLANNING = "PRE_PLANNING"
REQUEST_PLANNED = "PLANNED"
REQUEST_COMPLETED = "COMPLETED"
PICKUP_COLLECTED = "COLLECTED"
HANDOVER_VALIDATED = "VALIDATED"
PAYMENT_PENDING = "PENDING"


class CollectionRequestInputError(ValueError):
    pass


class ServiceabilityContextIneligibleError(RuntimeError):
    def __init__(self, message: str, *, status: int = 409, code: str = "NOT_ELIGIBLE") -> None:
        self.status = status
        self.code = code
        super().__init__(message)


class CollectionRequestNotFoundError(LookupError):
    pass


class CollectionRequestCompletionError(RuntimeError):
    pass


class CollectionRequestNotCompletableError(CollectionRequestCompletionError):
    pass


class CollectionRequestPickupMissingError(CollectionRequestCompletionError):
    pass


class CollectionRequestPickupNotCollectedError(CollectionRequestCompletionError):
    pass


class CollectionRequestValidatedHandoverMissingError(CollectionRequestCompletionError):
    pass


class CollectionRequestPickupEvidenceInsufficientError(CollectionRequestCompletionError):
    pass


class CollectionRequestHandoverEvidenceInsufficientError(CollectionRequestCompletionError):
    pass


@dataclass(frozen=True, slots=True)
class CreateCollectionRequestCommand:
    customer_id: UUID
    client_request_id: UUID
    serviceability_context_id: UUID
    slot_start: datetime
    slot_end: datetime
    items: tuple[DeclaredRequestItem, ...]
    payment_expires_at: datetime


@dataclass(frozen=True, slots=True)
class CollectionRequestResult:
    request: CollectionRequest
    items: tuple[CollectionRequestItem, ...]
    payment: Payment


def _validate_quote(quote: PricingQuote, item_count: int) -> None:
    if len(quote.items) != item_count:
        raise CollectionRequestInputError("pricing quote item count does not match the request")
    if quote.total_amount_minor < 0:
        raise CollectionRequestInputError("pricing quote total cannot be negative")
    if len(quote.currency) != 3:
        raise CollectionRequestInputError("pricing quote currency must be a three-letter code")
    if any(item.quoted_line_amount_minor < 0 for item in quote.items):
        raise CollectionRequestInputError("pricing quote line amounts cannot be negative")


async def _load_request_result(
    session: AsyncSession, *, request_id: UUID, customer_id: UUID
) -> CollectionRequestResult:
    request = await session.scalar(
        select(CollectionRequest).where(
            CollectionRequest.request_id == request_id,
            CollectionRequest.customer_id == customer_id,
        )
    )
    if request is None:
        raise CollectionRequestNotFoundError("collection request not found")
    items = tuple(
        await session.scalars(
            select(CollectionRequestItem)
            .where(CollectionRequestItem.request_id == request_id)
            .order_by(CollectionRequestItem.created_at, CollectionRequestItem.request_item_id)
        )
    )
    payment = await session.scalar(select(Payment).where(Payment.request_id == request_id))
    if payment is None:
        raise RuntimeError("collection request is missing its logical payment")
    return CollectionRequestResult(request=request, items=items, payment=payment)


async def _validate_completion_prerequisites(session: AsyncSession, *, request_id: UUID) -> None:
    pickup = await session.scalar(
        select(PickupExecution).where(PickupExecution.request_id == request_id)
    )
    if pickup is None:
        raise CollectionRequestPickupMissingError("collection request has no pickup execution")
    if pickup.status != PICKUP_COLLECTED:
        raise CollectionRequestPickupNotCollectedError("collection request pickup is not collected")

    validated_handover_id = await session.scalar(
        select(HandoverEventItem.handover_event_id)
        .join(
            HandoverEvent,
            HandoverEvent.handover_event_id == HandoverEventItem.handover_event_id,
        )
        .where(
            HandoverEventItem.pickup_execution_id == pickup.pickup_execution_id,
            HandoverEventItem.status == HANDOVER_VALIDATED,
            HandoverEvent.status == HANDOVER_VALIDATED,
        )
    )
    if validated_handover_id is None:
        raise CollectionRequestValidatedHandoverMissingError(
            "collection request pickup has no validated handover"
        )

    has_pickup_evidence = await session.scalar(
        select(
            exists()
            .where(PickupEvidenceLink.pickup_execution_id == pickup.pickup_execution_id)
            .where(EvidenceCapture.evidence_capture_id == PickupEvidenceLink.evidence_capture_id)
        )
    )
    if not has_pickup_evidence:
        raise CollectionRequestPickupEvidenceInsufficientError(
            "collection request pickup has no evidence capture"
        )

    has_handover_evidence = await session.scalar(
        select(
            exists()
            .where(HandoverEvidenceLink.handover_event_id == validated_handover_id)
            .where(EvidenceCapture.evidence_capture_id == HandoverEvidenceLink.evidence_capture_id)
        )
    )
    if not has_handover_evidence:
        raise CollectionRequestHandoverEvidenceInsufficientError(
            "validated handover has no evidence capture"
        )


async def complete_collection_request(
    session_factory: async_sessionmaker[AsyncSession],
    request_id: UUID,
    *,
    now: datetime | None = None,
) -> CollectionRequest:
    """Complete a planned request once its persisted fulfilment evidence is sufficient."""
    async with session_factory() as session, session.begin():
        request = await session.scalar(
            select(CollectionRequest)
            .where(CollectionRequest.request_id == request_id)
            .with_for_update()
        )
        if request is None:
            raise CollectionRequestNotFoundError("collection request not found")
        if request.status == REQUEST_COMPLETED:
            return request
        if request.status != REQUEST_PLANNED:
            raise CollectionRequestNotCompletableError(
                f"collection request in status {request.status!r} cannot be completed"
            )

        await _validate_completion_prerequisites(session, request_id=request.request_id)
        request.status = REQUEST_COMPLETED
        request.completed_at = now or utc_now()
        await session.flush([request])
        return request


async def create_collection_request(
    session_factory: async_sessionmaker[AsyncSession],
    command: CreateCollectionRequestCommand,
    pricing: PricingPort,
    *,
    idempotency_expires_at: datetime,
    protector: AddressProtector | None = None,
    location_resolver: LocationResolver | None = None,
    cell_id_deriver: CellIdDeriver | None = None,
    slot_availability: SlotAvailabilityPort | None = None,
    planning_lead_time_minutes: int | None = None,
) -> CollectionRequestResult:
    if not command.items:
        raise CollectionRequestInputError("at least one declared item is required")
    if command.slot_start.tzinfo is None or command.slot_end.tzinfo is None:
        raise CollectionRequestInputError("pickup slot timestamps must be timezone-aware")
    if command.slot_end <= command.slot_start:
        raise CollectionRequestInputError("pickup slot end must be after its start")

    fingerprint = command_fingerprint(
        {
            "customer_id": command.customer_id,
            "client_request_id": command.client_request_id,
            "serviceability_context_id": command.serviceability_context_id,
            "slot_start": command.slot_start,
            "slot_end": command.slot_end,
            "items": command.items,
        }
    )
    scope = f"collection-request.create:{command.customer_id}"
    idempotency_key = str(command.client_request_id)

    # A completed replay does not invoke pricing again. This read-only session is
    # closed before a new quote is requested.
    async with session_factory() as session:
        existing = await session.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.scope == scope,
                IdempotencyRecord.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            if not compare_digest(existing.request_fingerprint, fingerprint):
                raise IdempotencyKeyConflictError(
                    "idempotency key is already associated with a different request"
                )
            replay = get_completed_idempotency_result(existing)
            if replay is None or replay.resource_id is None:
                raise IdempotencyCommandInProgressError(
                    "collection request creation is already in progress"
                )
            return await _load_request_result(
                session, request_id=replay.resource_id, customer_id=command.customer_id
            )

    # Only fresh commands revalidate time; durable exact replay survives expiry,
    # planning, and later operational policy/category changes.
    slot = validate_slot(command.slot_start, command.slot_end, now=utc_now())
    availability = slot_availability or UnconfiguredSlotAvailability()

    async with session_factory() as session:
        context = await session.scalar(
            select(ServiceabilityContext).where(
                ServiceabilityContext.serviceability_context_id
                == command.serviceability_context_id,
                ServiceabilityContext.user_id == command.customer_id,
            )
        )
        if context is None:
            raise ServiceabilityContextIneligibleError(
                "owned serviceability context not found", status=404
            )
        if context.expires_at <= utc_now():
            raise ServiceabilityContextIneligibleError(
                "serviceability context has expired", code="SERVICEABILITY_EXPIRED"
            )
        context_status = context.status

    if context_status == SERVICEABILITY_PENDING:
        context = await resolve_serviceability(
            session_factory,
            context_id=command.serviceability_context_id,
            protector=protector or UnconfiguredAddressProtector(),
            location_resolver=location_resolver or UnconfiguredLocationResolver(),
            cell_id_deriver=cell_id_deriver or H3CellIdDeriver(),
        )
    if context.status != SERVICEABILITY_SERVICEABLE:
        raise ServiceabilityContextIneligibleError(
            "serviceability context is not serviceable",
            status=503 if context.status == "TECHNICAL_FAILURE" else 409,
            code="SERVICEABILITY_UNSERVICEABLE"
            if context.status == "UNSERVICEABLE"
            else "NOT_ELIGIBLE",
        )
    if context.location is None or context.cell_id is None:
        raise ServiceabilityContextIneligibleError(
            "serviceability context is missing its resolved location or cell"
        )

    # Pricing may eventually be external. It deliberately runs without a DB session.
    quote = await pricing.quote(command.items)
    _validate_quote(quote, len(command.items))

    async with session_factory() as session, session.begin():
        claim = await claim_idempotency_record(
            session,
            scope=scope,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            expires_at=idempotency_expires_at,
        )
        if not claim.created:
            replay = get_completed_idempotency_result(claim.record)
            if replay is None or replay.resource_id is None:
                raise IdempotencyCommandInProgressError(
                    "collection request creation is already in progress"
                )
            return await _load_request_result(
                session, request_id=replay.resource_id, customer_id=command.customer_id
            )

        context = await session.scalar(
            select(ServiceabilityContext).where(
                ServiceabilityContext.serviceability_context_id
                == command.serviceability_context_id,
                ServiceabilityContext.user_id == command.customer_id,
            )
        )
        now = utc_now()
        if context is None:
            raise ServiceabilityContextIneligibleError(
                "owned serviceability context not found", status=404
            )
        if context.status != SERVICEABILITY_SERVICEABLE:
            raise ServiceabilityContextIneligibleError("serviceability context is not serviceable")
        if context.expires_at <= now:
            raise ServiceabilityContextIneligibleError(
                "serviceability context has expired", code="SERVICEABILITY_EXPIRED"
            )
        if context.location is None or context.cell_id is None:
            raise ServiceabilityContextIneligibleError(
                "serviceability context is missing its resolved location or cell"
            )
        # Use the planning serialization boundary and query live availability in
        # the very transaction that creates request/payment/items/idempotency.
        dates = await availability.service_dates(session, cell_id=context.cell_id, now=now)
        from tirodhan.modules.collection_requests.scheduling import (
            SERVICE_TIMEZONE,
            SlotConflictError,
        )

        if slot.start.astimezone(SERVICE_TIMEZONE).date() not in dates:
            raise SlotConflictError()
        await evaluate_slot(
            session,
            availability,
            cell_id=context.cell_id,
            slot=slot,
            now=now,
            lead_time_minutes=planning_lead_time_minutes,
            booking=True,
        )
        # Recheck context/payment expiry after waiting on the planning lock.
        now = utc_now()
        if context.expires_at <= now:
            raise CustomerReadError(409, "SERVICEABILITY_EXPIRED")
        categories = (
            await session.execute(
                select(CatalogueCategory, CatalogueGroup)
                .join(CatalogueGroup)
                .where(
                    CatalogueCategory.category_code.in_(
                        [item.item_category_code for item in command.items]
                    )
                )
                .with_for_update(of=(CatalogueCategory, CatalogueGroup))
                .order_by(CatalogueGroup.group_id, CatalogueCategory.category_id)
            )
        ).all()
        if any(not category.active or not group.active for category, group in categories):
            raise CustomerReadError(409, "NOT_ELIGIBLE")
        display_names = {
            category.category_code: category.display_name for category, group in categories
        }
        if command.payment_expires_at <= now:
            raise CollectionRequestInputError("payment expiry must be in the future")

        request = CollectionRequest(
            request_id=new_uuid7(),
            client_request_id=command.client_request_id,
            customer_id=command.customer_id,
            serviceability_context_id=context.serviceability_context_id,
            pickup_address_snapshot_encrypted=bytes(context.address_snapshot_encrypted),
            pickup_location=context.location,
            cell_id=context.cell_id,
            slot_start=command.slot_start,
            slot_end=command.slot_end,
            quoted_amount_minor=quote.total_amount_minor,
            currency=quote.currency.upper(),
            status=REQUEST_PENDING_PAYMENT,
            payment_expires_at=command.payment_expires_at,
            created_at=now,
        )
        session.add(request)
        request_items = tuple(
            CollectionRequestItem(
                request_item_id=new_uuid7(),
                request_id=request.request_id,
                item_category_code=declared.item_category_code,
                display_name_snapshot=display_names.get(declared.item_category_code),
                declared_quantity=declared.declared_quantity,
                declared_weight_grams=declared.declared_weight_grams,
                quoted_line_amount_minor=quoted.quoted_line_amount_minor,
                currency=quote.currency.upper(),
                pricing_rule_version=quoted.pricing_rule_version,
                created_at=now,
            )
            for declared, quoted in zip(command.items, quote.items, strict=True)
        )
        payment = Payment(
            payment_id=new_uuid7(),
            request_id=request.request_id,
            amount_minor=quote.total_amount_minor,
            currency=quote.currency.upper(),
            status=PAYMENT_PENDING,
            created_at=now,
        )
        session.add_all([*request_items, payment])
        await session.flush()
        await complete_idempotency_record(
            session,
            claim.record,
            result_resource_id=request.request_id,
            result_status_code=201,
        )
        return CollectionRequestResult(request=request, items=request_items, payment=payment)
