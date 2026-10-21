"""Bounded manager views over immutable options and actual activation/custody facts."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .admission_models import AdmissionSourceHead
from .capacity_models import RiderReplacement
from .orchestration_models import CargoTransferIntent, OperatingRegion
from .replacement_models import ReplacementCandidate, ReplacementEvaluation


async def evaluation_history(
    session: AsyncSession,
    identity: UUID,
    *,
    after_id: UUID | None = None,
    limit: int = 50,
    evaluation_id: UUID | None = None,
    after_rank: int = 0,
) -> dict[str, Any]:
    request = await session.get(RiderReplacement, identity)
    if request is None:
        raise ValueError("REPLACEMENT_UNAVAILABLE")
    query = select(ReplacementEvaluation).where(
        ReplacementEvaluation.replacement_id.in_(
            select(RiderReplacement.replacement_id).where(
                RiderReplacement.incident_id == request.incident_id
            )
        )
        if request.incident_id
        else ReplacementEvaluation.replacement_id == identity
    )
    if after_id:
        query = query.where(ReplacementEvaluation.evaluation_id > after_id)
    if evaluation_id:
        query = query.where(ReplacementEvaluation.evaluation_id == evaluation_id)
    rows = list(
        await session.scalars(query.order_by(ReplacementEvaluation.evaluation_id).limit(limit + 1))
    )
    if evaluation_id and not rows:
        raise ValueError("EVALUATION_UNAVAILABLE")
    items = []
    for row in rows[:limit]:
        summary = {
            "evaluation_id": row.evaluation_id,
            "source_version": row.source_version,
            "region_version": row.region_version,
            "source_fingerprint": row.source_fingerprint.hex(),
            "created_at": row.created_at,
            "expires_at": row.expires_at,
            "search": row.search,
            "outcome": row.outcome,
            "is_latest": request.evaluation_id == row.evaluation_id,
        }
        if evaluation_id:
            choices = list(
                await session.scalars(
                    select(ReplacementCandidate)
                    .where(
                        ReplacementCandidate.evaluation_id == evaluation_id,
                        ReplacementCandidate.rank > after_rank,
                    )
                    .order_by(ReplacementCandidate.rank)
                    .limit(limit + 1)
                )
            )
            summary["candidates"] = [
                {"candidate_id": c.candidate_id, "rank": c.rank, "selectable": False, **c.summary}
                for c in choices[:limit]
            ]
            summary["next_rank"] = choices[limit - 1].rank if len(choices) > limit else None
        items.append(summary)
    return {
        "evaluations": items,
        "next_cursor": rows[limit - 1].evaluation_id if len(rows) > limit else None,
    }


async def read_replacement_options(
    session: AsyncSession,
    identity: UUID,
    now: datetime,
    *,
    after_rank: int = 0,
    limit: int = 50,
) -> dict[str, Any]:
    row = await session.get(RiderReplacement, identity)
    if row is None:
        raise ValueError("REPLACEMENT_UNAVAILABLE")
    evaluation = (
        await session.get(ReplacementEvaluation, row.evaluation_id) if row.evaluation_id else None
    )
    head = await session.get(AdmissionSourceHead, row.region_id)
    region = await session.get(OperatingRegion, row.region_id)
    from .replacement_commands import refreshed_pending_selection

    refreshed = await refreshed_pending_selection(session, row)
    fresh = bool(
        evaluation
        and head
        and region
        and now < evaluation.expires_at
        and evaluation.source_version == head.version
        and evaluation.region_version == region.version
        and (row.status == "RECOMMENDED" or refreshed)
    )
    choices = (
        list(
            await session.scalars(
                select(ReplacementCandidate)
                .where(
                    ReplacementCandidate.evaluation_id == row.evaluation_id,
                    ReplacementCandidate.rank > after_rank,
                )
                .order_by(ReplacementCandidate.rank)
                .limit(limit + 1)
            )
        )
        if evaluation
        else []
    )
    intent = (
        await session.get(CargoTransferIntent, row.transfer_intent_id)
        if row.transfer_intent_id
        else None
    )
    return {
        "replacement_id": identity,
        "request_id": row.request_id,
        "status": row.status,
        "version": row.version,
        "original_rider_id": row.original_rider_id,
        "requested_by": row.requested_by,
        "requested_at": row.requested_at,
        "reason": row.reason,
        "incident_id": row.incident_id,
        "recommended_shift_id": row.recommended_shift_id,
        "recommended_at": row.recommended_at,
        "source_version": row.source_version,
        "selected_candidate_id": row.selected_candidate_id,
        "selection_refresh_available": refreshed and fresh,
        "decided_by": row.decided_by,
        "decided_at": row.decided_at,
        "activation_id": row.activation_id,
        "activated_at": row.activated_at,
        "transfer_intent_id": row.transfer_intent_id,
        "custody_transferred": bool(intent and intent.resolution == "TRANSFERRED"),
        "authoritative": row.status == "COMPLETED" and row.activation_id is not None,
        "failure_code": row.failure_code,
        "activation_state": "ACTIVATED"
        if row.activation_id
        else "PENDING"
        if row.status == "APPROVED"
        else "NOT_AUTHORIZED",
        "evaluation": {
            "evaluation_id": evaluation.evaluation_id,
            "outcome": evaluation.outcome,
            "source_version": evaluation.source_version,
            "region_version": evaluation.region_version,
            "source_fingerprint": evaluation.source_fingerprint.hex(),
            "created_at": evaluation.created_at,
            "expires_at": evaluation.expires_at,
            "current": fresh,
            "search": evaluation.search,
        }
        if evaluation
        else None,
        "candidates": [
            {"candidate_id": c.candidate_id, "rank": c.rank, "selectable": fresh, **c.summary}
            for c in choices[:limit]
        ],
        "next_cursor": choices[limit - 1].rank if len(choices) > limit else None,
    }
