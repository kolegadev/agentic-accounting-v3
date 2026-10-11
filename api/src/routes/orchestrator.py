"""REST routes for the F6 orchestrator — thin wrappers, no business logic.

Exposes classify → construct → preview, plus approve / reject / pending.
The LLM is only invoked by the message endpoint; approve, reject and
pending never touch it.  Orchestrator structured errors map to 422 with
{type, message}; service exceptions map to 404 / 422.

Contract F19: every endpoint accepts an optional ``entity_id`` (the
message body for /message, a query param for approve/reject/pending, a
form field for the F38 multipart /document upload).
The scope resolves explicit > single-active > NULL legacy; more than one
active entity without an explicit id is a 409 {type: ambiguous_entity} —
never a silent global fallback.

Contract F38: POST /document takes a multipart PDF/JPG/PNG upload,
extracts its text (415 unsupported type, 413 over 10MB, 422 unreadable)
and previews a draft through the same pipeline as /message.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from src.config.database import get_db
from src.services.document_extract import (
    DocumentExtractError,
    extract_document_text,
)
from src.services.entity_service import (
    AmbiguousEntityError,
    EntityNotFoundError,
    require_active_entity,
    resolve_entity_scope,
)
from src.services.orchestrator import (
    EntityScopeError,
    OrchestrateResult,
    approve_entry,
    orchestrate_document,
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
    entity_id: Optional[uuid.UUID] = None


class TransactionRef(BaseModel):
    """Body for POST /approve and /reject — the draft to act on."""

    transaction_id: uuid.UUID


# ---------------------------------------------------------------------------
# Entity scope resolution (Contract F19 + F20)
# ---------------------------------------------------------------------------


async def _resolve_scope(
    db: AsyncSession,
    entity_id: Optional[uuid.UUID],
) -> Optional[uuid.UUID]:
    """Explicit > single-active > NULL legacy; ambiguous → the exception.

    Contract F20: an explicit ``entity_id`` must exist AND be active —
    else :class:`EntityNotFoundError` (the routes answer 404
    {type: entity_not_found}) instead of silently degrading to 'main'.
    """
    if entity_id is not None:
        await require_active_entity(db, entity_id)
    return await resolve_entity_scope(db, entity_id)


def _ambiguous_response(exc: AmbiguousEntityError) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={"type": exc.error_type or "ambiguous_entity", "message": exc.message},
    )


def _entity_not_found_response(exc: EntityNotFoundError) -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content={
            "type": exc.error_type or "entity_not_found",
            "message": exc.message,
        },
    )


def _preview_content(result: OrchestrateResult) -> dict[str, Any]:
    """The preview payload shared by /message and /document (F38)."""
    return {
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
        "contact": result.contact,
    }


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

    Returns 200 with status "preview" (draft awaiting approval),
    "clarification" (the LLM needs more detail), or "answer" (a query
    response).  Hard errors map to 422 with {type, message}.  An
    ambiguous entity scope maps to 409 {type: ambiguous_entity}.
    """
    try:
        scope_id = await _resolve_scope(db, data.entity_id)
    except AmbiguousEntityError as exc:
        return _ambiguous_response(exc)
    except EntityNotFoundError as exc:
        return _entity_not_found_response(exc)

    result = await orchestrate_message(
        db, data.message, history=data.history, entity_id=scope_id
    )

    if result.status == "preview":
        return JSONResponse(
            status_code=200,
            content={
                "status": "preview",
                "preview": _preview_content(result),
            },
        )

    if result.status == "answer":
        return JSONResponse(
            status_code=200,
            content={
                "status": "answer",
                "query_type": result.query_type,
                "data": result.data,
                "contact": result.contact,
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
# POST /document — upload a PDF/JPG/PNG, extract its text, preview a draft
# (Contract F38).  The document's TEXT (never an image) rides the SAME
# classify → construct pipeline as /message, so preview/approve is unchanged.
# ---------------------------------------------------------------------------

#: Accepted extensions / content types (Contract F38) — anything else is 415.
ALLOWED_DOCUMENT_EXTENSIONS = (".pdf", ".jpg", ".jpeg", ".png")
ALLOWED_DOCUMENT_CONTENT_TYPES = (
    "application/pdf",
    "image/jpeg",
    "image/jpg",
    "image/png",
)
#: Generic upload types are not a signal either way — the extension decides.
GENERIC_DOCUMENT_CONTENT_TYPES = ("", "application/octet-stream")

#: Hard upload limit (Contract F38) — larger uploads are 413.
MAX_DOCUMENT_BYTES = 10 * 1024 * 1024


def _document_type_supported(filename: str, content_type: str) -> bool:
    """True when the type is on the whitelist (Contract F38).

    Verifier fix-list: the EXTRACTOR dispatches on the extension, so a
    named file's extension must itself be whitelisted — the content type
    is consulted only for extension-less uploads.  (A ``.exe`` that
    claims ``image/png`` is refused here, not 422'd downstream.)
    """
    extension = os.path.splitext(filename or "")[1].lower()
    ctype = (content_type or "").split(";")[0].strip().lower()
    if extension:
        return extension in ALLOWED_DOCUMENT_EXTENSIONS
    if ctype in GENERIC_DOCUMENT_CONTENT_TYPES:
        return False
    return ctype in ALLOWED_DOCUMENT_CONTENT_TYPES


def _parse_history_form(history: str) -> tuple[list[dict[str, Any]] | None, JSONResponse | None]:
    """Parse the optional ``history`` form field (a JSON array of turns)."""
    try:
        parsed = json.loads(history)
    except ValueError:
        parsed = None
    if not isinstance(parsed, list):
        return None, JSONResponse(
            status_code=422,
            content={
                "type": "invalid_history",
                "message": (
                    "history must be a JSON array of {role, content} objects"
                ),
            },
        )
    return parsed, None


@router.post(
    "/document",
    summary="Extract the text from an uploaded document and preview a draft",
    status_code=200,
    responses={
        413: {"description": "Document over 10MB: {type: document_too_large}"},
        415: {"description": "Unsupported type: {type: unsupported_document_type}"},
        422: {"description": "Structured extraction/orchestrator error: {type, message}"},
    },
)
async def document(
    file: UploadFile = File(
        ..., description="Invoice/bill/payroll document (PDF, JPG, JPEG or PNG)"
    ),
    entity_id: uuid.UUID | None = Form(
        None, description="Entity scope (required when several are active)"
    ),
    history: str | None = Form(
        None, description="Conversation history as a JSON string"
    ),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Multipart upload → extract text → classify → construct → preview.

    Guards: unsupported type → 415, over :data:`MAX_DOCUMENT_BYTES` → 413,
    unreadable/OCR-missing → 422 {type, message}.  On success the response
    mirrors /message plus ``source_text`` and ``filename`` so the UI can
    show what was read.  Nothing is persisted and nothing posts here.
    """
    filename = file.filename or ""
    if not _document_type_supported(filename, file.content_type or ""):
        return JSONResponse(
            status_code=415,
            content={
                "type": "unsupported_document_type",
                "message": (
                    f"{filename or 'uploaded file'} is not a supported "
                    "document — upload a PDF, JPG, JPEG or PNG"
                ),
            },
        )

    data = await file.read()
    if len(data) > MAX_DOCUMENT_BYTES:
        return JSONResponse(
            status_code=413,
            content={
                "type": "document_too_large",
                "message": (
                    f"document is {len(data)} bytes — the limit is "
                    f"{MAX_DOCUMENT_BYTES} bytes (10MB)"
                ),
            },
        )

    parsed_history: list[dict[str, Any]] | None = None
    if history:
        parsed_history, history_error = _parse_history_form(history)
        if history_error is not None:
            return history_error

    try:
        scope_id = await _resolve_scope(db, entity_id)
    except AmbiguousEntityError as exc:
        return _ambiguous_response(exc)
    except EntityNotFoundError as exc:
        return _entity_not_found_response(exc)

    try:
        text = extract_document_text(filename, data)
    except DocumentExtractError as exc:
        return JSONResponse(
            status_code=422,
            content={"type": exc.type, "message": exc.message},
        )

    result = await orchestrate_document(
        db,
        text,
        filename=filename,
        entity_id=scope_id,
        history=parsed_history,
    )

    if result.status == "preview":
        return JSONResponse(
            status_code=200,
            content={
                "status": "preview",
                "preview": _preview_content(result),
                "source_text": result.source_text,
                "filename": filename,
            },
        )

    if result.status == "answer":
        return JSONResponse(
            status_code=200,
            content={
                "status": "answer",
                "query_type": result.query_type,
                "data": result.data,
                "contact": result.contact,
                "source_text": result.source_text,
                "filename": filename,
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
        409: {"description": "Ambiguous entity scope: {type: ambiguous_entity}"},
        422: {"description": "Transaction not in draft, or unbalanced"},
    },
)
async def approve(
    data: TransactionRef,
    entity_id: Optional[uuid.UUID] = Query(
        None, description="Entity scope (required when several are active)"
    ),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Post the draft: assigns JE ref, mirrors to Formance, verifies parity."""
    try:
        scope_id = await _resolve_scope(db, entity_id)
    except AmbiguousEntityError as exc:
        return _ambiguous_response(exc)
    except EntityNotFoundError as exc:
        return _entity_not_found_response(exc)
    try:
        result = await approve_entry(db, data.transaction_id, entity_id=scope_id)
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
    entity_id: Optional[uuid.UUID] = Query(
        None, description="Entity scope (required when several are active)"
    ),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Discard the draft. Posted rows are append-only — never deleted.

    The resolved entity scope passes through to ``reject_entry``: a draft
    stamped with a different entity is refused (404 entity_mismatch) —
    never a cross-entity discard (Contract F20).
    """
    try:
        scope_id = await _resolve_scope(db, entity_id)
    except AmbiguousEntityError as exc:
        return _ambiguous_response(exc)
    except EntityNotFoundError as exc:
        return _entity_not_found_response(exc)
    try:
        result = await reject_entry(db, data.transaction_id, entity_id=scope_id)
    except TransactionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.message)
    except TransactionNotDraftError as exc:
        raise HTTPException(status_code=422, detail=exc.message)
    except EntityScopeError as exc:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "type": exc.error_type or "entity_mismatch",
                "message": exc.message,
            },
        )
    return {"status": result.status, "transaction_id": str(result.transaction_id)}


# ---------------------------------------------------------------------------
# GET /pending — drafts awaiting approval (newest first)
# ---------------------------------------------------------------------------


@router.get(
    "/pending",
    summary="List draft transactions awaiting approval",
    status_code=200,
    responses={409: {"description": "Ambiguous entity scope: {type: ambiguous_entity}"}},
)
async def pending(
    entity_id: Optional[uuid.UUID] = Query(
        None, description="Entity scope (required when several are active)"
    ),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Return drafts only (status='draft'), newest first, with legs —
    scoped to the resolved entity (stamped rows) in multi-entity mode."""
    try:
        scope_id = await _resolve_scope(db, entity_id)
    except AmbiguousEntityError as exc:
        return _ambiguous_response(exc)
    except EntityNotFoundError as exc:
        return _entity_not_found_response(exc)
    transactions, total = await TransactionService.list_transactions(
        db, status="draft", entity_id=scope_id
    )
    return {
        "transactions": [
            TransactionService._transaction_to_response(t) for t in transactions
        ],
        "total": total,
    }
