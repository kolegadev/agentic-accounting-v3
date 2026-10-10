"""REST routes for the F6 orchestrator — thin wrappers, no business logic.

Exposes classify → construct → preview, plus approve / reject / pending.
The LLM is only invoked by the message endpoint; approve, reject and
pending never touch it.  Orchestrator structured errors map to 422 with
{type, message}; service exceptions map to 404 / 422.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from src.config.database import get_db
from src.services.orchestrator import (
    approve_entry,
    orchestrate_message,
    reject_entry,
)
from src.services.transaction_service import (
    TransactionNotDraftError,
    TransactionNotFoundError,
    TransactionService,
    UnbalancedTransactionError,
)

router = APIRouter(prefix="/api/v1/orchestrator", tags=["Orchestrator"])


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class MessageRequest(BaseModel):
    """Body for POST /message — classify + construct a draft preview."""

    message: str
    history: Optional[list[dict[str, Any]]] = None
    session_id: Optional[str] = None


class TransactionRef(BaseModel):
    """Body for POST /approve and /reject — the draft to act on."""

    transaction_id: uuid.UUID


# ---------------------------------------------------------------------------
# POST /message — preview (nothing posts)
# ---------------------------------------------------------------------------


@router.post(
    "/message",
    summary="Classify a message and construct a draft preview",
    status_code=200,
    responses={422: {"description": "Structured orchestrator error: {type, message}"}},
)
async def message(
    data: MessageRequest,
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Run classify → construct → draft. Nothing is posted.

    Returns 200 with status "preview" (draft awaiting approval) or
    "clarification" (the LLM needs more detail).  Hard errors map to
    422 with {type, message}.
    """
    result = await orchestrate_message(db, data.message, history=data.history)

    if result.status == "preview":
        return JSONResponse(
            status_code=200,
            content={
                "status": "preview",
                "preview": {
                    "transaction_id": str(result.transaction_id),
                    "skill_id": result.skill_id,
                    "legs": [
                        {
                            "code": leg.code,
                            "name": leg.name,
                            "account_class": leg.account_class,
                            "debit_pence": leg.debit_pence,
                            "credit_pence": leg.credit_pence,
                            "description": leg.description,
                        }
                        for leg in (result.legs or [])
                    ],
                    "total_pence": result.total_pence,
                    "formance_preview": result.formance_preview,
                },
            },
        )

    error: dict[str, str] = result.error or {
        "type": "invalid_response",
        "message": "Orchestrator returned no result",
    }
    if error.get("type") == "clarification":
        return JSONResponse(
            status_code=200,
            content={"status": "clarification", "error": error},
        )
    return JSONResponse(
        status_code=422,
        content={"type": error.get("type", "error"), "message": error.get("message", "")},
    )


# ---------------------------------------------------------------------------
# POST /approve — post the draft (mirrors + parity)
# ---------------------------------------------------------------------------


@router.post(
    "/approve",
    summary="Post an approved draft transaction",
    status_code=200,
    responses={
        404: {"description": "Transaction not found"},
        422: {"description": "Transaction not in draft, or unbalanced"},
    },
)
async def approve(
    data: TransactionRef,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Post the draft: assigns JE ref, mirrors to Formance, verifies parity."""
    try:
        result = await approve_entry(db, data.transaction_id)
    except TransactionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.message)
    except TransactionNotDraftError as exc:
        raise HTTPException(status_code=422, detail=exc.message)
    except UnbalancedTransactionError as exc:
        raise HTTPException(status_code=422, detail=exc.message)
    return {
        "status": "posted",
        "transaction": result.transaction,
        "parity_ok": result.parity_ok,
        "parity_report": asdict(result.parity_report),
    }


# ---------------------------------------------------------------------------
# POST /reject — discard the draft
# ---------------------------------------------------------------------------


@router.post(
    "/reject",
    summary="Discard a draft transaction",
    status_code=200,
    responses={
        404: {"description": "Transaction not found"},
        422: {"description": "Transaction not in draft"},
    },
)
async def reject(
    data: TransactionRef,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Discard the draft. Posted rows are append-only — never deleted."""
    try:
        result = await reject_entry(db, data.transaction_id)
    except TransactionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.message)
    except TransactionNotDraftError as exc:
        raise HTTPException(status_code=422, detail=exc.message)
    return {"status": result.status, "transaction_id": str(result.transaction_id)}


# ---------------------------------------------------------------------------
# GET /pending — drafts awaiting approval (newest first)
# ---------------------------------------------------------------------------


@router.get(
    "/pending",
    summary="List draft transactions awaiting approval",
    status_code=200,
)
async def pending(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Return drafts only (status='draft'), newest first, with legs."""
    transactions, total = await TransactionService.list_transactions(
        db, status="draft"
    )
    return {
        "transactions": [
            TransactionService._transaction_to_response(t) for t in transactions
        ],
        "total": total,
    }
