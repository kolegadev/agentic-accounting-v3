"""REST routes for Contract F10 — Formance-derived reports (thin wrappers).

Exposes the five F9 report engine outputs over GET, each with the
Postgres cross-check result alongside.  No business logic lives here:
param validation only, then straight calls into the F9 engine.

Engine identity failures surface as ReportEngineError → 422 {type,
message}; a broken report is never returned (the engine raises first).

Contract F19: an optional ``entity_id`` query param scopes the report.
Resolution: explicit id → its ledger + COA scope; else the single active
entity → its ledger + scope; else NULL + ``"main"`` (legacy).  More than
one active entity without an explicit id is a 409
{type: ambiguous_entity} — never a silent ``"main"`` report.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.config.database import get_db
from src.services.entity_service import (
    AmbiguousEntityError,
    EntityNotFoundError,
    get_ledger_for_entity,
    require_active_entity,
    resolve_entity_scope,
)
from src.services.formance_reports import (
    ReportEngineError,
    generate_bs,
    generate_cash_flow,
    generate_funds,
    generate_pl,
    generate_tb,
    verify_against_postgres,
)

router = APIRouter(prefix="/api/v1/reports/formance", tags=["Reports — Formance"])

# Flow reports read period flows; balance reports read balances as at a date.
FLOW_TYPES = ("pl", "cash-flow", "funds")
BALANCE_TYPES = ("balance-sheet", "trial-balance")
VALID_REPORT_TYPES = FLOW_TYPES + BALANCE_TYPES

NO_POSTGRES_NOTE = "no Postgres equivalent"


async def _resolve_scope(
    db: AsyncSession,
    entity_id: Optional[uuid.UUID],
) -> tuple[Optional[uuid.UUID], str]:
    """(entity scope, ledger) per the F19 rule; ambiguous raises.

    Contract F20: an explicit ``entity_id`` must exist AND be active —
    else :class:`EntityNotFoundError` (the route answers 404
    {type: entity_not_found}) instead of silently degrading to 'main'.
    """
    if entity_id is not None:
        await require_active_entity(db, entity_id)
    scope_id = await resolve_entity_scope(db, entity_id)
    if scope_id is None:
        return None, "main"
    return scope_id, await get_ledger_for_entity(db, scope_id)


# ---------------------------------------------------------------------------
# GET /{report_type} — run the engine + Postgres cross-check
# ---------------------------------------------------------------------------


@router.get(
    "/{report_type}",
    summary="Run a Formance-derived report with the Postgres cross-check",
    status_code=200,
    responses={
        422: {
            "description": (
                "Invalid report type, missing period/as-at params, or "
                "engine identity error: {type, message}"
            )
        },
    },
)
async def formance_report(
    report_type: str,
    period_start: Optional[date] = Query(
        None, description="Inclusive period start (flow reports: required)"
    ),
    period_end: Optional[date] = Query(
        None, description="Inclusive period end (flow reports: required)"
    ),
    as_at: Optional[date] = Query(
        None, description="Balance date (balance reports: defaults to today)"
    ),
    entity_id: Optional[uuid.UUID] = Query(
        None, description="Entity scope (required when several are active)"
    ),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Run one of the five F9 Formance reports and cross-check against Postgres.

    pl / cash-flow / funds require period_start + period_end;
    balance-sheet / trial-balance take as_at (default: today).
    P&L/BS/TB additionally run verify_against_postgres and include the
    diff list; cash-flow/funds have no Postgres equivalent.
    """
    if report_type not in VALID_REPORT_TYPES:
        return JSONResponse(
            status_code=422,
            content={
                "type": "invalid_report_type",
                "message": (
                    f"unknown report type {report_type!r}; valid: "
                    + ", ".join(VALID_REPORT_TYPES)
                ),
            },
        )

    if report_type in FLOW_TYPES and (period_start is None or period_end is None):
        return JSONResponse(
            status_code=422,
            content={
                "type": "missing_period_params",
                "message": (
                    f"report type {report_type!r} requires period_start and "
                    "period_end query params"
                ),
            },
        )

    # Resolve the entity scope once per request (Contract F19 + F20):
    # explicit id (validated) > single active entity > NULL legacy;
    # ambiguity is a 409, an unknown explicit id a 404 — never a silent
    # 'main'.
    try:
        scope_id, ledger = await _resolve_scope(db, entity_id)
    except AmbiguousEntityError as exc:
        return JSONResponse(
            status_code=409,
            content={
                "type": exc.error_type or "ambiguous_entity",
                "message": exc.message,
            },
        )
    except EntityNotFoundError as exc:
        return JSONResponse(
            status_code=404,
            content={
                "type": exc.error_type or "entity_not_found",
                "message": exc.message,
            },
        )

    if report_type == "pl":
        generator, kwargs = generate_pl, {
            "period_start": period_start,
            "period_end": period_end,
        }
    elif report_type == "cash-flow":
        generator, kwargs = generate_cash_flow, {
            "period_start": period_start,
            "period_end": period_end,
        }
    elif report_type == "funds":
        generator, kwargs = generate_funds, {
            "period_start": period_start,
            "period_end": period_end,
        }
    elif report_type == "balance-sheet":
        generator, kwargs = generate_bs, {"as_at": as_at or date.today()}
    else:
        generator, kwargs = generate_tb, {"as_at": as_at or date.today()}

    try:
        report = await generator(db, ledger, entity_id=scope_id, **kwargs)
    except ReportEngineError as exc:
        return JSONResponse(
            status_code=422,
            content={"type": "report_engine_error", "message": str(exc)},
        )

    if report_type == "pl" or report_type in BALANCE_TYPES:
        diffs = await verify_against_postgres(
            db, report, report_type, entity_id=scope_id
        )
        cross_check = {"diffs": diffs, "ok": not bool(diffs)}
    else:
        cross_check = {
            "diffs": [],
            "ok": None,
            "note": NO_POSTGRES_NOTE,
        }

    return JSONResponse(
        status_code=200,
        content={
            "report_type": report_type,
            # The engine embeds date objects (period / as_at); encode them
            # to ISO strings so the payload is plain JSON.
            "report": jsonable_encoder(report),
            "identities": {"identity_ok": bool(report.get("identity_ok"))},
            "cross_check": cross_check,
        },
    )
