# ER Diagram

This file contains the consolidated relationship view for quick review.

For entity semantics, see `DOMAIN_MODEL.md`.

For physical table/constraint details, see `SCHEMA_DESIGN.md`.

```mermaid
erDiagram
    APP_USER ||--o{ USER_PHONE : has
    APP_USER ||--o{ USER_ROLE : has
    APP_USER ||--o{ REFRESH_SESSION : owns
    APP_USER ||--o{ USER_ADDRESS : saves
    APP_USER ||--o| RIDER_PROFILE : may_be

    APP_USER ||--o{ SERVICEABILITY_CONTEXT : creates
    USER_ADDRESS ||--o{ SERVICEABILITY_CONTEXT : may_source

    APP_USER ||--o{ COLLECTION_REQUEST : places
    SERVICEABILITY_CONTEXT ||--o| COLLECTION_REQUEST : produces
    COLLECTION_REQUEST ||--|{ COLLECTION_REQUEST_ITEM : contains

    COLLECTION_REQUEST ||--|| PAYMENT : payable_by
    PAYMENT ||--o{ PAYMENT_ATTEMPT : attempted_through
    PAYMENT_ATTEMPT ||--o{ PAYMENT_PROVIDER_EVENT : receives
    PAYMENT ||--o{ REFUND : may_have
    REFUND ||--o{ PAYMENT_PROVIDER_EVENT : receives

    PLANNING_BATCH ||--o{ PLANNING_BATCH_ATTEMPT : executed_as
    PLANNING_BATCH ||--|{ COLLECTION_REQUEST : freezes
    PLANNING_BATCH ||--|{ COLLECTION_GROUP : produces
    COLLECTION_GROUP ||--|{ COLLECTION_GROUP_MEMBER : contains
    COLLECTION_REQUEST ||--o| COLLECTION_GROUP_MEMBER : planned_into

    FLEET ||--o{ FLEET_MEMBERSHIP : has
    RIDER_PROFILE ||--o{ FLEET_MEMBERSHIP : joins
    RIDER_PROFILE ||--|| RIDER_AVAILABILITY : current_state

    COLLECTION_GROUP ||--o{ ASSIGNMENT_OFFER : offered_as
    RIDER_PROFILE ||--o{ ASSIGNMENT_OFFER : receives

    COLLECTION_GROUP ||--o{ RIDER_ASSIGNMENT : assignment_history
    RIDER_PROFILE ||--o{ RIDER_ASSIGNMENT : performs

    COLLECTION_REQUEST ||--o| PICKUP_EXECUTION : fulfilled_by
    COLLECTION_GROUP ||--|{ PICKUP_EXECUTION : groups

    RIDER_ASSIGNMENT ||--|{ RIDER_ASSIGNMENT_ITEM : contains
    PICKUP_EXECUTION ||--o{ RIDER_ASSIGNMENT_ITEM : assigned_through

    PICKUP_EXECUTION ||--o{ PICKUP_ATTEMPT : attempted
    RIDER_ASSIGNMENT ||--o{ PICKUP_ATTEMPT : performed_under
    PICKUP_EXECUTION ||--o{ PICKUP_INCIDENT : may_raise

    EVIDENCE_CAPTURE ||--|{ MEDIA_ASSET : contains
    PICKUP_EXECUTION ||--o{ PICKUP_EVIDENCE_LINK : evidenced_by
    EVIDENCE_CAPTURE ||--o{ PICKUP_EVIDENCE_LINK : supports

    RECEIVING_POINT ||--o{ HANDOVER_EVENT : receives
    RIDER_PROFILE ||--o{ HANDOVER_EVENT : performs
    HANDOVER_EVENT ||--|{ HANDOVER_EVENT_ITEM : contains
    PICKUP_EXECUTION ||--o{ HANDOVER_EVENT_ITEM : handed_over_by

    HANDOVER_EVENT ||--o{ HANDOVER_EVIDENCE_LINK : evidenced_by
    EVIDENCE_CAPTURE ||--o{ HANDOVER_EVIDENCE_LINK : supports
```

## Cross-cutting reliability tables

These are intentionally not shown in the business ER graph:

```text
IDEMPOTENCY_RECORD
INBOX_MESSAGE
OUTBOX_EVENT
```

They are reliability infrastructure rather than business aggregates.

See `IDEMPOTENCY.md`.
