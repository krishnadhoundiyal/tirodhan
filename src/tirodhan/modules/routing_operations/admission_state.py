"""Discardable regional read models; PostgreSQL remains the only authority.

An exact set of visible transactional notices fences each MVCC read. No sequence
watermark is used: allocating an identifier is not the same as committing a change.
"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID
from weakref import WeakKeyDictionary

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from .admission_models import AdmissionSourceNotice
from .admission_queries import in_ids
from .admission_roads import DirectedRoadCache
from .orchestration_models import CapacityReservation
from .snapshot import OperationalSnapshot, ShadowConfiguration, _read_sources

# Changes to these leaf tables can be reprojected by request/reservation identity.
# Every other change rebuilds the complete resource/custody dependency closure.
LEAVES = {"collection_request", "regional_capacity_reservation", "routing_manifest"}
REQUEST_INPUTS = {
    "appointment_consent",
    "cargo_transfer",
    "discharge_authorization",
    "physical_load_observation",
    "reattempts",
    "reattempt_authorizations",
}


@dataclass
class ReadState:
    version: int
    signature: str
    sources: dict[str, Any]
    expires_at: datetime | None
    charged_bytes: int


class AdmissionReadCache:
    def __init__(self, max_bytes: int = 64_000_000):
        self.max_bytes = max_bytes
        self.states: OrderedDict[UUID, ReadState] = OrderedDict()
        self.bytes = 0
        self.full_reads = self.delta_reads = self.hits = 0
        self.road_sources: OrderedDict[str, DirectedRoadCache] = OrderedDict()

    def roads(self, providers: dict[str, Any]) -> dict[str, DirectedRoadCache]:
        from tirodhan.modules.planning.routing.execution import RoutingAdapterError

        budget = min(4_000_000, 32_000_000 // max(1, len(providers)))
        if budget < 1024:
            raise RoutingAdapterError("UNKNOWN_PROFILE_MEMORY_BUDGET")
        for profile in set(self.road_sources) - set(providers):
            self.road_sources.pop(profile)
        result = {}
        for profile, source in providers.items():
            cache = self.road_sources.pop(profile, None)
            if cache is None or cache.source is not source or cache.max_bytes != budget:
                cache = DirectedRoadCache(source, max_bytes=budget)
            self.road_sources[profile] = result[profile] = cache
        return result

    async def capture(
        self, factory: async_sessionmaker[AsyncSession], config: ShadowConfiguration, version: int
    ) -> tuple[OperationalSnapshot, bytes]:
        assert config.region_id is not None
        # Read a reusable *superset*, then project each original 24-hour routing
        # horizon below. This does not alter appointment/discharge terms. The wider
        # MVCC read scope is internal; no >24h routing problem is constructed.
        anchor = (config.epoch + timedelta(hours=12)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        read_config = config.model_copy(
            update={
                "epoch": anchor - timedelta(hours=12),
                "horizon_end": anchor + timedelta(hours=36),
            }
        )
        signature = read_config.model_dump_json(exclude={"as_of", "request_ids"})
        cached = self.states.get(config.region_id)
        async with factory() as session:
            await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            notices = await session.stream(
                select(
                    AdmissionSourceNotice.notice_id,
                    AdmissionSourceNotice.source_table,
                    AdmissionSourceNotice.source_key,
                )
                .where(AdmissionSourceNotice.region_id == config.region_id)
                .order_by(AdmissionSourceNotice.notice_id)
                .execution_options(yield_per=512)
            )
            digest = hashlib.sha256()
            affected: set[UUID] = set()
            leaves_only = True
            async for notice in notices:
                digest.update(notice.notice_id.bytes)
                if notice.source_table not in LEAVES:
                    leaves_only = False
                if leaves_only:
                    affected.add(UUID(notice.source_key))
                    # Delta-query parameter/scratch budget. A full regional read
                    # replaces an overlarge delta; no obligation is discarded.
                    if len(affected) * 256 > 1_000_000:
                        leaves_only = False
                        affected.clear()
            stamp = digest.digest()
            reusable = (
                cached is not None
                and cached.version == version
                and cached.signature == signature
                and (
                    cached.expires_at is None
                    or config.as_of is not None
                    and config.as_of < cached.expires_at
                )
                and leaves_only
            )
            if reusable and not affected:
                assert cached is not None
                sources = cached.sources
                self.hits += 1
            elif reusable:
                assert cached is not None
                # Resolve both sides of binding even if only one side was changed.
                aliases = await session.execute(
                    select(
                        CapacityReservation.reservation_id,
                        CapacityReservation.request_id,
                    ).where(
                        CapacityReservation.region_id == config.region_id,
                        (in_ids(CapacityReservation.reservation_id, affected))
                        | (in_ids(CapacityReservation.request_id, affected)),
                    )
                )
                for reserved, booked in aliases:
                    affected.add(reserved)
                    if booked:
                        affected.add(booked)
                delta = await _read_sources(
                    session, read_config.model_copy(update={"request_ids": tuple(affected)})
                )
                sources = dict(delta)
                sources["requests"] = sorted(
                    [r for r in cached.sources["requests"] if r["request_id"] not in affected]
                    + delta["requests"],
                    key=lambda r: str(r["request_id"]),
                )
                sources["released"] = [
                    r for r in cached.sources["released"] if r["request_id"] not in affected
                ] + delta["released"]
                inputs = dict(delta["operational_inputs"])
                old = cached.sources["operational_inputs"]
                for key in REQUEST_INPUTS:
                    inputs[key] = [
                        r for r in old.get(key, []) if r["request_id"] not in affected
                    ] + inputs.get(key, [])
                inputs["region_cells"] = sorted(
                    set(map(tuple, old.get("region_cells", [])))
                    | set(map(tuple, inputs.get("region_cells", [])))
                )
                sources["operational_inputs"] = inputs
                required_points = {r["receiving_point_id"] for r in sources["requests"]}
                if required_points <= {p["receiving_point_id"] for p in sources["points"]}:
                    self.delta_reads += 1
                else:
                    sources = await _read_sources(session, read_config)
                    self.full_reads += 1
            else:
                sources = await _read_sources(session, read_config)
                self.full_reads += 1
            expiry = await session.scalar(
                select(func.min(CapacityReservation.expires_at)).where(
                    CapacityReservation.region_id == config.region_id,
                    CapacityReservation.expires_at > (config.as_of or config.epoch),
                )
            )
        self.put(config.region_id, version, signature, sources, expiry)
        return OperationalSnapshot(project_horizon(sources, config), stamp), stamp

    def put(
        self,
        region: UUID,
        version: int,
        signature: str,
        sources: dict[str, Any],
        expires_at: datetime | None,
    ) -> None:
        # Conservative charge covers decoded Python objects as well as the wire size.
        charge = 8 * len(json.dumps(sources, default=str, separators=(",", ":")).encode())
        previous = self.states.pop(region, None)
        if previous:
            self.bytes -= previous.charged_bytes
        if charge > self.max_bytes:
            return  # Cold reconstruction remains correct; no business rejection.
        while self.states and self.bytes + charge > self.max_bytes:
            _, removed = self.states.popitem(last=False)
            self.bytes -= removed.charged_bytes
        self.states[region] = ReadState(version, signature, sources, expires_at, charge)
        self.bytes += charge


def project_horizon(sources: dict[str, Any], config: ShadowConfiguration) -> dict[str, Any]:
    projected = dict(sources)
    vehicles = [
        v
        for v in sources["vehicles"]
        if v["ends_at"] > config.epoch and v["starts_at"] < config.horizon_end
    ]
    projected["vehicles"] = vehicles
    riders = {v["rider_id"] for v in vehicles}
    projected["requests"] = [
        r
        for r in sources["requests"]
        if (
            r["collector_id"] in riders
            if r["pickup_status"] == "COLLECTED"
            else r["owner_id"] in riders
            or (r["slot_start"] < config.horizon_end and r["slot_end"] >= config.epoch)
        )
    ]
    past = {v["vehicle_id"] for v in sources["vehicles"] if v["ends_at"] <= config.epoch}
    projected["orphaned_routes"] = sources["orphaned_routes"] + [
        r for r in sources["routes"] if r["vehicle_id"] in past
    ]
    active = {v["vehicle_id"] for v in vehicles}
    projected["routes"] = [r for r in sources["routes"] if r["vehicle_id"] in active]
    roots = {r["route_id"] for r in projected["routes"]}
    projected["tasks"] = [t for t in sources["tasks"] if t["route_id"] in roots]
    projected["released"] = [r for r in sources["released"] if r["vehicle_id"] in active]
    return projected


_READ_CACHES: WeakKeyDictionary[AsyncEngine, AdmissionReadCache] = WeakKeyDictionary()


def read_cache(factory: async_sessionmaker[AsyncSession]) -> AdmissionReadCache:
    engine = factory.kw["bind"]
    if engine not in _READ_CACHES:
        _READ_CACHES[engine] = AdmissionReadCache()
    return _READ_CACHES[engine]
