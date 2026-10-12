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

import logging
import uuid
from datetime import date
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.config.database import get_db
from src.services.coa_service import CoaService
from src.services.entity_service import (
    AmbiguousEntityError,
    EntityNotFoundError,
    EntityService,
    get_ledger_for_entity,
    require_active_entity,
    resolve_entity_scope,
)
from src.services.formance_client import formance_client
from src.services.formance_reports import (
    ReportEngineError,
    generate_ap_aging,
    generate_ar_aging,
    generate_bs,
    generate_cash_flow,
    generate_funds,
    generate_pl,
    generate_tb,
    verify_against_postgres,
)
from src.services.transaction_service import TransactionService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/reports/formance", tags=["Reports — Formance"])

# Flow reports read period flows; balance reports read balances as at a date.
FLOW_TYPES = ("pl", "cash-flow", "funds")
BALANCE_TYPES = ("balance-sheet", "trial-balance")
# Contract F37 — aging reports are LEDGER-DERIVED (the F9 engine's
# generate_ar_aging / generate_ap_aging); as_at only, like balance types.
AGING_TYPES = ("aged-receivables", "aged-payables")
# Contract F39 — the Chart of Accounts report joins the Postgres COA with
# Formance balances and a per-account "used" flag (not engine-derived).
COA_TYPES = ("chart-of-accounts",)
VALID_REPORT_TYPES = FLOW_TYPES + BALANCE_TYPES + AGING_TYPES + COA_TYPES

NO_POSTGRES_NOTE = "no Postgres equivalent"

AGING_CROSS_CHECK_NOTE = (
    "aging is ledger-derived (the same posted ledger the Balance Sheet "
    "reads) and reconciles to the trial-balance balance by construction — "
    "there is no separate Postgres equivalent to diff against"
)

COA_CROSS_CHECK_NOTE = (
    "CoA rows are the Postgres source of record; balances are read from "
    "the Formance ledger."
)


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


def _fiscal_year_start(fiscal_year_start: str, today: date) -> date:
    """Most recent occurrence of the entity's fiscal-year-start (MM-DD)
    on or before ``today``; falls back to the UK 6-April convention."""
    try:
        month, day = (int(part) for part in fiscal_year_start.split("-"))
        candidate = date(today.year, month, day)
        if candidate > today:
            candidate = date(today.year - 1, month, day)
        return candidate
    except (ValueError, AttributeError):
        candidate = date(today.year, 4, 6)
        return candidate if candidate <= today else date(today.year - 1, 4, 6)


async def _entity_fiscal_start(
    db: AsyncSession, scope_id: Optional[uuid.UUID], end: date
) -> Optional[date]:
    """Fiscal-year start for a window ending ``end`` (Contract F23b/F24).

    The resolved entity's ``fiscal_year_start`` (UK 6-April fallback when
    the entity has none); ``None`` when no entity scope resolves.  Lookup
    failures degrade to the UK default — best-effort, like
    ``get_ledger_for_entity`` — so a pre-F11 schema or an unreachable
    entities table never breaks a report request.
    """
    if scope_id is None:
        return None
    fiscal_year_start = "04-06"
    try:
        entity = await EntityService.get_entity(db, scope_id)
    except Exception as exc:  # noqa: BLE001 — resolution is best-effort
        logger.warning(
            "_entity_fiscal_start: entity lookup failed (%s); "
            "using the UK 6-April default",
            exc,
        )
        entity = None
    if entity is not None and entity.fiscal_year_start:
        fiscal_year_start = entity.fiscal_year_start
    return _fiscal_year_start(fiscal_year_start, end)


async def _run_aging_report(
    db: AsyncSession,
    report_type: str,
    scope_id: Optional[uuid.UUID],
    ledger: str,
    as_at: Optional[date],
) -> JSONResponse:
    """Contract F37 — ledger-derived aging from the F9 engine.

    The aging is computed from the SAME posted ledger the Balance Sheet
    reads (posted + reversed Postgres transactions, entity-scoped) and the
    engine asserts its FIFO total equals the absolute trial-balance
    balance for 1100 / 2000 — a mismatch raises
    :class:`ReportEngineError` (422 ``{type: report_engine_error}``), never
    a quietly wrong report.

    Contract F42: each item carries ``contact_name`` (the counterparty
    from the Postgres ``Transaction.contact_id`` join; ``None`` when
    absent) — the route forwards the engine's items unchanged.
    """
    as_of = as_at or date.today()
    generator = (
        generate_ar_aging
        if report_type == "aged-receivables"
        else generate_ap_aging
    )
    try:
        aging = await generator(db, ledger, as_at=as_of, entity_id=scope_id)
    except ReportEngineError as exc:
        return JSONResponse(
            status_code=422,
            content={"type": "report_engine_error", "message": str(exc)},
        )

    return JSONResponse(
        status_code=200,
        content={
            "report_type": report_type,
            "as_at": as_of.isoformat(),
            "report": jsonable_encoder(
                {
                    "items": aging["items"],
                    "buckets": aging["buckets"],
                    "total_outstanding_pence": aging["total_outstanding_pence"],
                    "ledger_balance": aging["ledger_balance"],
                    "reconciled": aging["reconciled"],
                    "note": aging["note"],
                }
            ),
            "cross_check": {
                "ok": None,
                "diffs": [],
                "note": AGING_CROSS_CHECK_NOTE,
            },
        },
    )


async def _run_coa_report(
    db: AsyncSession,
    scope_id: Optional[uuid.UUID],
    ledger: str,
    as_at: Optional[date],
) -> JSONResponse:
    """Contract F39 — the Chart of Accounts report.

    The rows are the Postgres COA (active, entity-scoped, ordered by code —
    the source of record); ``balance``/``balances`` come from the Formance
    ledger (address == account code, the engine's mapping) and ``used`` is
    True when the account has ANY transactions (Postgres posted scope OR a
    Formance posting).  A Formance read failure degrades to empty balances
    and used=False — the report never crashes because the ledger is down
    (the same no-crash contract as the sibling reports' missing scope).
    """
    as_of = as_at or date.today()

    accounts = await CoaService.list_accounts(db, entity_id=scope_id)

    balance_by_address: dict[str, dict[str, str]] = {}
    try:
        raw_balances = await formance_client.get_balances(ledger)
    except Exception as exc:  # noqa: BLE001 — degrade, never crash
        logger.warning(
            "_run_coa_report: Formance balances for ledger '%s' failed (%s); "
            "serving the report without balances",
            ledger,
            exc,
        )
        raw_balances = {"data": []}
    for item in (raw_balances or {}).get("data") or []:
        if not isinstance(item, dict):
            continue
        address = str(item.get("address") or "")
        balances = item.get("balances")
        if isinstance(balances, dict):
            balance_by_address[address] = {
                str(asset): str(value) for asset, value in balances.items()
            }

    rows: list[dict[str, Any]] = []
    for account in accounts:
        balances = balance_by_address.get(account.code, {})
        # The engine's asset convention is "GBP/2" (ledger_parity's
        # DEFAULT_EXPECTED_ASSET); prefer it, fall back to a plain "GBP".
        balance = balances.get("GBP/2", balances.get("GBP"))

        used = False
        postgres = await TransactionService.list_for_account(
            db, account.code, entity_id=scope_id, limit=1
        )
        if postgres is not None and postgres.get("items"):
            used = True
        else:
            try:
                formance = await formance_client.list_account_transactions(
                    ledger, account.code, page_size=1
                )
            except Exception as exc:  # noqa: BLE001 — degrade, never crash
                logger.warning(
                    "_run_coa_report: Formance usage check for code '%s' "
                    "failed (%s); Postgres remains authoritative",
                    account.code,
                    exc,
                )
                formance = {"data": []}
            if formance.get("data"):
                used = True

        rows.append(
            {
                "id": str(account.id),
                "code": account.code,
                "name": account.name,
                "category": account.category,
                "type": account.type,
                "vat_rate": account.vat_rate,
                "balance": balance,
                "balances": balances,
                "used": used,
            }
        )

    return JSONResponse(
        status_code=200,
        content={
            "report_type": "chart-of-accounts",
            "as_at": as_of.isoformat(),
            "report": {"accounts": rows},
            "cross_check": {
                "ok": None,
                "diffs": [],
                "note": COA_CROSS_CHECK_NOTE,
            },
        },
    )


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
                "Invalid report type, missing period/as-at params, or engine "
                "identity error (including an aging that does not reconcile "
                "to the trial balance): {type, message}"
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
    """Run one of the eight Formance reports and cross-check against Postgres.

    pl / cash-flow / funds require period_start + period_end;
    balance-sheet / trial-balance / aged-receivables / aged-payables /
    chart-of-accounts take as_at (default: today).  The aging reports are ledger-derived (the F37
    engine generators) and reconcile to the trial-balance debtors /
    creditors balance by construction.
    The balance sheet additionally receives the entity's fiscal-year start
    as ``period_start`` (Contract F24, None = inception) for its
    profit-for-period line. P&L/BS/TB additionally run
    verify_against_postgres and include the diff list; cash-flow/funds
    have no Postgres equivalent.
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

    # Contract F37 — aged receivables/payables take as_at only (like the
    # balance types) and are served by the F9 engine's ledger-derived
    # aging generators; they never reach the engine dispatch below.
    if report_type in AGING_TYPES:
        return await _run_aging_report(db, report_type, scope_id, ledger, as_at)

    # Contract F39 — the Chart of Accounts report is served straight from
    # the Postgres COA + the Formance balances; it never reaches the
    # engine dispatch below.
    if report_type in COA_TYPES:
        return await _run_coa_report(db, scope_id, ledger, as_at)

    if report_type in FLOW_TYPES and (period_start is None or period_end is None):
        period_end = period_end or date.today()
        if period_start is None:
            period_start = await _entity_fiscal_start(db, scope_id, period_end)
            if period_start is None:
                # No entity scope: the legacy UK fiscal default.
                period_start = _fiscal_year_start("04-06", period_end)

    # Contract F24: the balance sheet's profit-for-period line covers the
    # current fiscal year — derive the same entity fiscal-window start and
    # pass it to the engine (None = inception when no entity resolves).
    # Trial balance passes nothing.
    bs_period_start: Optional[date] = None
    if report_type == "balance-sheet":
        bs_period_start = await _entity_fiscal_start(
            db, scope_id, as_at or date.today()
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
        generator, kwargs = generate_bs, {
            "as_at": as_at or date.today(),
            "period_start": bs_period_start,
        }
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
