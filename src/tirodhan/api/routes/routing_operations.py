from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tirodhan.api.dependencies import get_session_factory, require_role
from tirodhan.db.values import utc_now
from tirodhan.modules.identity.service import ROLE_MANAGER, AuthenticatedPrincipal
from tirodhan.modules.reliability.primitives import IdempotencyKeyConflictError
from tirodhan.modules.routing_operations.commands import (
    OperationalInputError,
    ResourceCommand,
    execute_resource_command,
)
from tirodhan.modules.routing_operations.service import read_shadow_run, request_shadow_run
from tirodhan.modules.routing_operations.snapshot import ShadowConfiguration

router = APIRouter(prefix="/v2/manager/routing", tags=["routing review"])
Principal = Annotated[AuthenticatedPrincipal, Depends(require_role(ROLE_MANAGER))]
Factory = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]


@router.post("/resources")
async def resource_command(
    command: ResourceCommand, request: Request, principal: Principal, factory: Factory
) -> dict[str, UUID]:
    ttl = request.app.state.settings.command_idempotency_ttl_seconds
    if ttl is None or ttl <= 0:
        raise HTTPException(503, "Command idempotency is not configured")
    try:
        result = await execute_resource_command(
            factory,
            manager_id=principal.user_id,
            command=command,
            expires_at=utc_now() + timedelta(seconds=ttl),
        )
    except (OperationalInputError, IdempotencyKeyConflictError, IntegrityError) as error:
        code = str(error) if isinstance(error, OperationalInputError) else "RESOURCE_CONFLICT"
        raise HTTPException(409, code) from None
    return {"resource_id": result}


@router.post("/shadow-runs/{client_command_id}")
async def create_run(
    client_command_id: UUID, config: ShadowConfiguration, principal: Principal, factory: Factory
) -> dict[str, UUID]:
    try:
        run = await request_shadow_run(
            factory,
            manager_id=principal.user_id,
            client_command_id=client_command_id,
            config=config,
        )
    except OperationalInputError as error:
        raise HTTPException(409, str(error)) from None
    return {"run_id": run}


@router.get("/shadow-runs/{run_id}")
async def read_run(run_id: UUID, principal: Principal, factory: Factory) -> dict[str, Any]:
    try:
        run = await read_shadow_run(factory, manager_id=principal.user_id, run_id=run_id)
    except OperationalInputError:
        raise HTTPException(404, "RUN_UNAVAILABLE") from None
    # No coordinates, manifests, raw matrices or command input in responses.
    return {
        "run_id": run.run_id,
        "status": run.status,
        "attempt": run.attempt,
        "failure_code": run.failure_code,
        "results": run.results,
        "authoritative": False,
    }
