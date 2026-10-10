"""FastAPI router for Entity + initial-ledger setup (Contract F11/F12)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.config.database import get_db
from src.middleware.auth import get_current_user, require_role
from src.models.user import User
from src.services.coa_service import CoaServiceError
from src.services.entity_service import EntityService, EntityServiceError
from src.services.setup_service import (
    SetupError,
    get_entity_status,
    setup_entity,
)
from src.validators.entity import (
    EntityCreateWithSetup,
    EntityResponse,
    EntitySetupResponse,
    EntityStatus,
    SetupRequest,
    SetupResult,
)

router = APIRouter(prefix="/api/v1/entities", tags=["Entities"])


async def _run_setup(
    db: AsyncSession,
    entity_id: uuid.UUID,
    setup: SetupRequest,
) -> SetupResult | JSONResponse:
    """Execute setup_entity and translate service errors into HTTP errors.

    CoaServiceError (incl. TemplateNotFoundError) → 404 {type:"coa_error"};
    SetupError → 409 {type:"setup_error"}; EntityServiceError → its status
    code (404 as today).  No uncaught escape.
    """
    try:
        return await setup_entity(
            db,
            entity_id,
            setup.template_name,
            bank_accounts=setup.bank_accounts,
            opening_balances=setup.opening_balances,
        )
    except CoaServiceError as exc:
        return JSONResponse(
            status_code=404,
            content={"type": "coa_error", "message": exc.message},
        )
    except SetupError as exc:
        return JSONResponse(
            status_code=409,
            content={"type": "setup_error", "message": exc.message},
        )
    except EntityServiceError as exc:
        return JSONResponse(
            status_code=exc.status_code,
            content={"type": "entity_error", "message": exc.message},
        )


# ---------------------------------------------------------------------------
# POST / — Create entity (optionally with immediate setup)
# ---------------------------------------------------------------------------
@router.post(
    "/",
    response_model=EntitySetupResponse,
    summary="Create an accounting entity",
    status_code=status.HTTP_201_CREATED,
    responses={
        400: {"description": "Entity name is required"},
        409: {"description": "Duplicate entity name or already set up"},
        404: {"description": "COA template or account not found"},
    },
)
async def create_entity(
    data: EntityCreateWithSetup,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> EntitySetupResponse | JSONResponse:
    """Create the entity (created_by = current user) and, when `setup` is
    provided, run the initial-ledger setup immediately."""
    try:
        entity = await EntityService.create_entity(
            db, data, created_by=current_user.id
        )
    except EntityServiceError as exc:
        if exc.error_type is not None:
            return JSONResponse(
                status_code=exc.status_code,
                content={"type": exc.error_type, "message": exc.message},
            )
        raise HTTPException(status_code=exc.status_code, detail=exc.message)

    setup_result: SetupResult | None = None
    if data.setup is not None:
        setup_result = await _run_setup(db, entity.id, data.setup)
        if isinstance(setup_result, JSONResponse):
            # The entity row was committed by create_entity above, before
            # setup ran. On setup failure it remains created — a documented,
            # acceptable consequence: the client receives the mapped error
            # (404/409) and can re-run setup via POST /{entity_id}/setup.
            return setup_result

    return EntitySetupResponse(
        entity=EntityResponse.model_validate(entity),
        setup=setup_result,
    )


# ---------------------------------------------------------------------------
# GET / — List entities
# ---------------------------------------------------------------------------
@router.get(
    "/",
    response_model=list[EntityResponse],
    summary="List all entities",
    status_code=status.HTTP_200_OK,
)
async def list_entities(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[EntityResponse]:
    """Return all accounting entities, newest first."""
    entities = await EntityService.list_entities(db)
    return [EntityResponse.model_validate(e) for e in entities]


# ---------------------------------------------------------------------------
# GET /{entity_id} — Get entity by ID
# ---------------------------------------------------------------------------
@router.get(
    "/{entity_id}",
    response_model=EntityResponse,
    summary="Get entity by ID",
    status_code=status.HTTP_200_OK,
    responses={404: {"description": "Entity not found"}},
)
async def get_entity_route(
    entity_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> EntityResponse:
    """Return a single entity by its UUID."""
    entity = await EntityService.get_entity(db, entity_id)
    if entity is None:
        raise HTTPException(status_code=404, detail=f"Entity '{entity_id}' not found")
    return EntityResponse.model_validate(entity)


# ---------------------------------------------------------------------------
# POST /{entity_id}/setup — Run the initial-ledger setup
# ---------------------------------------------------------------------------
@router.post(
    "/{entity_id}/setup",
    response_model=SetupResult,
    summary="Set up the initial ledger for an entity",
    status_code=status.HTTP_200_OK,
    responses={
        404: {"description": "Entity not found"},
        409: {"description": "Entity already set up"},
        403: {"description": "Insufficient role"},
    },
)
async def setup_entity_route(
    entity_id: uuid.UUID,
    data: SetupRequest,
    current_user: User = Depends(require_role("owner", "admin")),
    db: AsyncSession = Depends(get_db),
) -> SetupResult:
    """Load COA, create bank accounts, ensure the ledger, draft opening balances."""
    return await _run_setup(db, entity_id, data)


# ---------------------------------------------------------------------------
# GET /{entity_id}/status — Setup status snapshot
# ---------------------------------------------------------------------------
@router.get(
    "/{entity_id}/status",
    response_model=EntityStatus,
    summary="Get entity setup status",
    status_code=status.HTTP_200_OK,
    responses={404: {"description": "Entity not found"}},
)
async def entity_status_route(
    entity_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> EntityStatus:
    """COA/bank/draft counts, ledger name, Formance reachability and parity."""
    try:
        return await get_entity_status(db, entity_id)
    except EntityServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
