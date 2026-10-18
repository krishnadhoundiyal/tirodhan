"""Relational capacity certificates: immutable routes, temporal shared membership."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import timedelta
from typing import Any
from uuid import UUID

from pydantic import TypeAdapter
from sqlalchemy import delete, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from tirodhan.db.values import new_uuid7
from tirodhan.modules.planning.routing.certificates import RouteProof
from tirodhan.modules.planning.routing.contracts import ProposedRoute, RouteProposal, RoutingProblem

from .admission_models import (
    AdmissionAllocation,
    AdmissionObligation,
    AdmissionPlanRevision,
    AdmissionProofObligation,
    AdmissionRouteCertificate,
    AdmissionRouteMembership,
    AdmissionSourceNotice,
)
from .admission_queries import in_ids
from .orchestration_models import CapacityReservation
from .service import PROPOSAL_CODEC

FORMAT = "ADMISSION_CERTIFICATE_V2"
PROOF_CODEC = TypeAdapter(RouteProof)


def certificate_document(
    proposal: RouteProposal, revision: int, incremental: bool, safety_percent: int
) -> dict[str, Any]:
    return {
        "format": FORMAT,
        "region_version": revision,
        "template": PROPOSAL_CODEC.dump_python(
            replace(proposal, routes=(), unserved_stop_ids=()), mode="json"
        ),
        "incremental": incremental,
        "safety_percent": safety_percent,
    }


async def notice_stamp(session: AsyncSession, region_id: UUID) -> bytes:
    digest = hashlib.sha256()
    notices = await session.stream_scalars(
        select(AdmissionSourceNotice.notice_id)
        .where(AdmissionSourceNotice.region_id == region_id)
        .order_by(AdmissionSourceNotice.notice_id)
        .execution_options(yield_per=512)
    )
    async for identity in notices:
        digest.update(identity.bytes)
    return digest.digest()


async def load_certificate(
    session: AsyncSession, reservation: CapacityReservation
) -> RouteProposal:
    document = reservation.certificate
    if document is None:
        raise ValueError("PROJECTION_HOLD_HAS_NO_ROUTE_CERTIFICATE")
    if document.get("format") != FORMAT:
        return PROPOSAL_CODEC.validate_python(document["proposal"])
    rows = (
        await session.execute(
            select(AdmissionRouteMembership.vehicle_id, AdmissionRouteCertificate.proof)
            .join(
                AdmissionRouteCertificate,
                AdmissionRouteCertificate.certificate_id == AdmissionRouteMembership.certificate_id,
            )
            .where(
                AdmissionRouteMembership.region_id == reservation.region_id,
                AdmissionRouteMembership.from_revision <= reservation.region_version,
                or_(
                    AdmissionRouteMembership.to_revision.is_(None),
                    AdmissionRouteMembership.to_revision > reservation.region_version,
                ),
            )
            .order_by(AdmissionRouteMembership.vehicle_id)
        )
    ).all()
    template = PROPOSAL_CODEC.validate_python(document["template"])
    return replace(
        template,
        routes=tuple(
            ProposedRoute(vehicle, PROOF_CODEC.validate_python(proof["route"]).order)
            for vehicle, proof in rows
        ),
    )


async def current_proofs(session: AsyncSession, region_id: UUID) -> dict[UUID, RouteProof]:
    rows = (
        await session.execute(
            select(AdmissionRouteMembership.vehicle_id, AdmissionRouteCertificate.proof)
            .join(
                AdmissionRouteCertificate,
                AdmissionRouteCertificate.certificate_id == AdmissionRouteMembership.certificate_id,
            )
            .where(
                AdmissionRouteMembership.region_id == region_id,
                AdmissionRouteMembership.to_revision.is_(None),
            )
        )
    ).all()
    return {
        vehicle: PROOF_CODEC.validate_python(proof["route"])
        for vehicle, proof in rows
        if proof.get("validator_version") == 1
    }


async def persist_certificate(
    session: AsyncSession,
    reservation: CapacityReservation,
    problem: RoutingProblem,
    proposal: RouteProposal,
    proofs: dict[UUID, RouteProof],
    sources: dict[str, Any],
    stamp: bytes,
) -> None:
    region_id, revision = reservation.region_id, reservation.region_version
    session.add(
        AdmissionPlanRevision(
            region_id=region_id, revision=revision, format_version=FORMAT, source_stamp=stamp
        )
    )
    await session.flush()
    existing = list(
        await session.scalars(
            select(AdmissionObligation).where(AdmissionObligation.region_id == region_id)
        )
    )
    by_reservation = {row.reservation_id: row for row in existing if row.reservation_id}
    by_request = {row.request_id: row for row in existing if row.request_id}
    aliases: dict[UUID | None, UUID] = dict(
        (
            await session.execute(
                select(CapacityReservation.request_id, CapacityReservation.reservation_id).where(
                    CapacityReservation.region_id == region_id,
                    CapacityReservation.request_id.is_not(None),
                )
            )
        )
        .tuples()
        .all()
    )
    obligations: dict[UUID, AdmissionObligation] = {}
    for source in sources["requests"]:
        key = source["request_id"]
        reserved = key if source.get("hold") else aliases.get(key)
        booked = None if source.get("hold") else key
        obligation = (by_reservation.get(reserved) if reserved else None) or (
            by_request.get(booked) if booked else None
        )
        if obligation is None:
            obligation = AdmissionObligation(
                obligation_id=reserved or key,
                region_id=region_id,
                reservation_id=reserved,
                request_id=booked,
            )
            session.add(obligation)
        elif booked is not None:
            obligation.request_id = booked
        obligations[key] = obligation
    await session.flush()
    members = {
        row.vehicle_id: row
        for row in await session.scalars(
            select(AdmissionRouteMembership).where(
                AdmissionRouteMembership.region_id == region_id,
                AdmissionRouteMembership.to_revision.is_(None),
            )
        )
    }
    certificates = {
        row.certificate_id: row
        for row in await session.scalars(
            select(AdmissionRouteCertificate).where(
                in_ids(
                    AdmissionRouteCertificate.certificate_id,
                    [m.certificate_id for m in members.values()],
                )
            )
        )
    }
    stops = {s.stop_id: s for s in problem.stops}
    vehicles = {v.vehicle_id: v for v in problem.vehicles}
    allocations: list[dict[str, UUID]] = []
    for route in proposal.routes:
        proof = proofs[route.vehicle_id]
        member = members.pop(route.vehicle_id, None)
        previous = certificates.get(member.certificate_id) if member else None
        if previous and previous.fingerprint == proof.fingerprint:
            continue
        if member:
            member.to_revision = revision
            await session.flush([member])
        certificate_id = new_uuid7()
        session.add(
            AdmissionRouteCertificate(
                certificate_id=certificate_id,
                region_id=region_id,
                vehicle_id=route.vehicle_id,
                shift_id=vehicles[route.vehicle_id].operating_shift_id,
                fingerprint=proof.fingerprint,
                proof={
                    "validator_version": 1,
                    "route": PROOF_CODEC.dump_python(proof, mode="json"),
                },
                valid_until=problem.epoch + timedelta(seconds=proof.latest_entry),
            )
        )
        await session.flush()
        session.add(
            AdmissionRouteMembership(
                region_id=region_id,
                vehicle_id=route.vehicle_id,
                from_revision=revision,
                certificate_id=certificate_id,
            )
        )
        lots = {stops[s].lot_id for s in route.stop_ids}
        for lot in lots:
            obligation = obligations[lot]
            session.add(
                AdmissionProofObligation(
                    certificate_id=certificate_id, obligation_id=obligation.obligation_id
                )
            )
            allocations.append(
                dict(obligation_id=obligation.obligation_id, vehicle_id=route.vehicle_id)
            )
    for old in members.values():
        old.to_revision = revision
    if allocations:
        command = insert(AdmissionAllocation)
        await session.execute(
            command.on_conflict_do_update(
                index_elements=[AdmissionAllocation.obligation_id],
                set_={"vehicle_id": command.excluded.vehicle_id},
            ),
            allocations,
        )
    active_ids = {o.obligation_id for o in obligations.values()}
    removed = [o.obligation_id for o in existing if o.obligation_id not in active_ids]
    for offset in range(0, len(removed), 512):
        await session.execute(
            delete(AdmissionAllocation).where(
                in_ids(AdmissionAllocation.obligation_id, removed[offset : offset + 512])
            )
        )
    # Delete exactly this SERIALIZABLE snapshot's visible notices. New concurrent
    # transactions cannot lose notices, and sequence allocation order is irrelevant.
    await session.execute(
        delete(AdmissionSourceNotice).where(AdmissionSourceNotice.region_id == region_id)
    )
