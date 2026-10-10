# /Users/johnpellew/agentic-accounting-v3/api/src/services/orchestrator.py

"""Contract F6 — Orchestrator (LLM classify → constructor → preview → approve → post).

The production pipeline: the LLM ONLY classifies the transaction type and
extracts facts; the F4 deterministic constructor builds the entry; the user
previews and approves; only then does the entry post and mirror to Formance
— followed by an F2 parity check.  The LLM has no path to construct money
math.

Nothing posts without :func:`approve_entry` — no code path may post inside
:func:`orchestrate_message`.  All money stays integer pence until F1
converts to Decimal strings at the mirror boundary.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx

from src.constructors import CONSTRUCTORS, ConstructorError, resolve_from_coa
from src.constructors.facts import FACTS_MODELS, parse_facts
from src.services.coa_service import CoaService
from src.services.entity_service import (
    AmbiguousEntityError,
    get_ledger_for_entity,
    resolve_entity_scope,
)
from src.services.ledger_parity import ParityReport, verify_parity
from src.services.transaction_service import (
    TransactionService,
    TransactionServiceError,
)
from src.validators.transaction import PostingCreate, TransactionCreate

logger = logging.getLogger(__name__)

__all__ = [
    "LLMError",
    "EntityScopeError",
    "OrchestrateResult",
    "PostResult",
    "PreviewLeg",
    "RejectResult",
    "approve_entry",
    "build_system_prompt",
    "call_llm",
    "orchestrate_message",
    "reject_entry",
]


# ---------------------------------------------------------------------------
# LLM configuration (env vars — same convention as llm_router)
# ---------------------------------------------------------------------------
LLM_API_URL: str = os.getenv("LLM_API_URL", "http://localhost:11434/v1/chat/completions")
LLM_API_KEY: str = os.getenv("LLM_API_KEY", "")
LLM_MODEL: str = os.getenv("LLM_MODEL", "deepseek-v4-flash")
LLM_TIMEOUT: float = float(os.getenv("LLM_TIMEOUT", "15.0"))
LLM_MAX_TOKENS: int = int(os.getenv("LLM_MAX_TOKENS", "2048"))

# Human-readable skill names (from each SKILL.md front-matter) for the prompt.
SKILL_DESCRIPTIONS: dict[str, str] = {
    "bank-transfer": "Transfer between own bank accounts",
    "customer-receipt": "Receive an AR balance into bank",
    "depreciation": "Monthly straight-line depreciation",
    "journal-accrual": "Accrued expense / provision journal",
    "opening-balances": "Initial capital, assets, liabilities",
    "payroll": "Net pay + PAYE/NIC liabilities",
    "prepayment": "Prepaid expense & deferred income",
    "sales-invoice": "Sale on credit (with & without VAT variants)",
    "supplier-invoice": "Purchase on credit (with & without VAT variants)",
    "supplier-payment": "Pay an AP balance from bank",
}

# Injectability seam: tests patch ``src.services.orchestrator.verify_parity``.

LLMTransport = Callable[[str, str], Awaitable[str]]


class LLMError(Exception):
    """LLM transport failed or returned an unparseable response."""


class EntityScopeError(TransactionServiceError):
    """A draft is stamped with a different entity than the request scope.

    Contract F20: the reject route carries a resolved ``entity_id``; a
    draft owned by a DIFFERENT entity (or the NULL legacy scope) is
    invisible within that scope, so the refusal is a 404
    (EntityNotFoundError-style) with ``error_type="entity_mismatch"`` —
    never a cross-entity discard.
    """

    def __init__(
        self,
        transaction_id: uuid.UUID,
        draft_entity_id: uuid.UUID | None,
        scope_entity_id: uuid.UUID,
    ) -> None:
        super().__init__(
            f"Transaction '{transaction_id}' belongs to entity "
            f"'{draft_entity_id}', not the request scope '{scope_entity_id}'",
            status_code=404,
        )
        self.error_type = "entity_mismatch"


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PreviewLeg:
    """One previewed leg of the draft entry — account code + name resolved."""

    code: str
    name: str
    account_class: str
    debit_pence: int
    credit_pence: int
    description: str


@dataclass
class OrchestrateResult:
    """Outcome of one orchestration pass.

    ``status`` is either ``"preview"`` (a draft transaction was created and
    awaits approval) or ``"error"`` (a structured error dict is set —
    never a raw traceback).
    """

    status: str
    transaction_id: uuid.UUID | None = None
    skill_id: str | None = None
    legs: list[PreviewLeg] | None = None
    total_pence: int | None = None
    formance_preview: list[dict[str, str]] | None = None
    error: dict[str, str] | None = None

    @classmethod
    def preview(
        cls,
        *,
        transaction_id: uuid.UUID,
        skill_id: str,
        legs: list[PreviewLeg],
        total_pence: int,
        formance_preview: list[dict[str, str]],
    ) -> OrchestrateResult:
        return cls(
            status="preview",
            transaction_id=transaction_id,
            skill_id=skill_id,
            legs=legs,
            total_pence=total_pence,
            formance_preview=formance_preview,
        )

    @classmethod
    def failure(cls, error_type: str, message: str) -> OrchestrateResult:
        return cls(status="error", error={"type": error_type, "message": message})


@dataclass
class PostResult:
    """Outcome of approving a draft: posted transaction + parity report.

    ``parity_ok`` may be False while the transaction is already posted —
    parity flags drift, it does not roll back.
    """

    transaction: Any
    parity_ok: bool
    parity_report: ParityReport


@dataclass
class RejectResult:
    """Outcome of rejecting a draft — it has been discarded."""

    transaction_id: uuid.UUID
    status: str = "discarded"


# ---------------------------------------------------------------------------
# LLM prompt builder
# ---------------------------------------------------------------------------


def build_system_prompt(history: list[dict[str, Any]] | None = None) -> str:
    """System prompt listing all 10 skills with their Facts JSON schemas.

    Schemas come from the F4 pydantic models via ``.model_json_schema()``;
    rules enforce integer pence, ISO dates, the vat_rate enum, and the
    closest-skill-or-clarification fallback.
    """
    skill_snippets: list[dict[str, Any]] = []
    for skill_id, model in FACTS_MODELS.items():
        skill_snippets.append(
            {
                "skill_id": skill_id,
                "description": SKILL_DESCRIPTIONS.get(skill_id, skill_id),
                "facts_schema": model.model_json_schema(),
            }
        )
    skills_json = json.dumps(skill_snippets, indent=2)

    context_lines: list[str] = []
    for entry in (history or [])[-10:]:
        role = entry.get("role", "unknown")
        content = entry.get("content", "")
        if isinstance(content, str) and len(content) > 300:
            content = content[:300] + "..."
        context_lines.append(f"[{role}] {content}")
    context = "\n".join(context_lines) if context_lines else "(no prior conversation)"

    return (
        "You are the transaction orchestrator for Agentic Accounting, a "
        "double-entry accounting system for UK small businesses.\n\n"
        "Your ONLY job is to classify the user's message into ONE of the "
        "transaction skills below and extract the facts that skill's schema "
        "requires. You NEVER construct journal entries, compute debit/credit "
        "legs, choose which account is debited or credited, or post anything "
        "— a deterministic constructor does that after you return facts.\n\n"
        "## Skills and their Facts JSON schemas\n\n"
        f"{skills_json}\n\n"
        "## Rules\n"
        "1. Respond with a single JSON object and nothing else:\n"
        '   {"skill_id": "<one of the skill ids above>", "facts": {<matching schema>}}\n'
        "2. ALL money amounts are INTEGER PENCE. £50.00 -> 5000. Never use "
        "floats or decimals for money.\n"
        "3. Dates are ISO 8601 strings (YYYY-MM-DD).\n"
        '4. vat_rate must be one of the enum values in the schema: '
        '"20%", "5%", "0%", "exempt".\n'
        "5. Account codes go ONLY into the facts fields that carry account "
        "codes (e.g. bank_account_code, expense_account_code). Use the codes "
        "the user mentions; the constructor resolves them against the chart "
        "of accounts.\n"
        "6. Choose the CLOSEST matching skill. Never invent a skill id.\n"
        '7. If the message is ambiguous or you cannot extract the required '
        'facts, respond with {"skill_id": null, "clarification": "<what you '
        'need to know>"}.\n\n'
        "## Conversation history\n\n"
        f"{context}"
    )


# ---------------------------------------------------------------------------
# LLM transport
# ---------------------------------------------------------------------------


async def call_llm(system_prompt: str, user_message: str) -> str:
    """Call the OpenAI-compatible LLM endpoint and return the raw content.

    Raises :class:`LLMError` when the endpoint is unreachable or the
    response shape is unexpected — never a raw httpx exception.
    """
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if LLM_API_KEY:
        headers["Authorization"] = f"Bearer {LLM_API_KEY}"

    payload = {
        "model": LLM_MODEL,
        "temperature": 0,
        "max_tokens": LLM_MAX_TOKENS,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
    }

    try:
        async with httpx.AsyncClient(timeout=LLM_TIMEOUT) as client:
            response = await client.post(LLM_API_URL, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
            content = data["choices"][0]["message"]["content"]
            return str(content).strip()
    except httpx.HTTPError as exc:
        logger.error("Orchestrator LLM API call failed: %s", exc)
        raise LLMError(f"LLM API error: {exc}") from exc
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        logger.error("Unexpected LLM response format: %s", exc)
        raise LLMError(f"LLM returned an unexpected response format: {exc}") from exc


def _parse_llm_json(raw: str) -> dict[str, Any]:
    """Parse the LLM's JSON output, stripping fences and common artifacts."""
    text = raw.strip()

    # Strip markdown code fences
    if text.startswith("```"):
        text = text[text.index("\n") + 1:] if "\n" in text else text[3:]
    if text.endswith("```"):
        text = text[: text.rindex("```")].strip()

    # Remove trailing commas (common LLM artifact)
    text = text.replace(",}", "}").replace(",]", "]")

    # Strip leading garbage before the first {
    brace_pos = text.find("{")
    if brace_pos > 0:
        text = text[brace_pos:]

    try:
        result = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMError(f"LLM response is not valid JSON: {exc}") from exc
    if not isinstance(result, dict):
        raise LLMError(f"LLM response is not a JSON object: {raw[:200]!r}")
    return result


# ---------------------------------------------------------------------------
# Fact helpers
# ---------------------------------------------------------------------------

_DATE_FACT_FIELDS = (
    "transfer_date",
    "receipt_date",
    "invoice_date",
    "payment_date",
    "pay_date",
    "period_end_date",
    "opening_date",
    "start_date",
    "end_date",
)


def _derive_effective_date(facts: Any) -> date | None:
    """Best-effort business date for the draft, from date-carrying facts."""
    for attr in _DATE_FACT_FIELDS:
        value = getattr(facts, attr, None)
        if isinstance(value, date):
            return value
    return None


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


async def orchestrate_message(
    db: Any,
    message: str,
    history: list[dict[str, Any]] | None = None,
    llm: LLMTransport | None = None,
    entity_id: uuid.UUID | None = None,
) -> OrchestrateResult:
    """Run the classify → construct → draft pipeline for one user message.

    Returns a preview result (draft created, nothing posted) or a structured
    error.  ``llm`` is the injectable transport: an async callable
    ``(system_prompt, user_message) -> raw text``; defaults to
    :func:`call_llm`.

    ``entity_id`` (Contract F19) is the entity scope: an explicit id wins;
    otherwise the single active entity; otherwise the legacy global scope
    (NULL).  The COA resolution and the stamped draft are entity-scoped;
    more than one active entity without an explicit id fails loud as a
    structured ``ambiguous_entity`` error (never a silent global draft).
    """
    transport: LLMTransport = llm or call_llm
    system_prompt = build_system_prompt(history)

    # ---- 1) LLM classify + extract (never constructs money math) ---------
    try:
        raw = await transport(system_prompt, message)
    except LLMError as exc:
        return OrchestrateResult.failure("llm_unreachable", str(exc))
    except Exception as exc:  # noqa: BLE001 — injected transports raise raw errors
        return OrchestrateResult.failure(
            "llm_error", f"LLM transport failed: {exc.__class__.__name__}: {exc}"
        )
    try:
        payload = _parse_llm_json(raw)
    except LLMError as exc:
        return OrchestrateResult.failure("invalid_response", str(exc))

    skill_id = payload.get("skill_id")
    if not skill_id:
        clarification = payload.get("clarification")
        if clarification:
            return OrchestrateResult.failure("clarification", str(clarification))
        return OrchestrateResult.failure(
            "invalid_response",
            "LLM returned neither a skill_id nor a clarification",
        )

    # ---- 2) parse_facts (pydantic, F4) — invalid -> structured error -----
    if skill_id not in CONSTRUCTORS:
        return OrchestrateResult.failure("unknown_skill", f"unknown skill id {skill_id!r}")
    facts_raw = payload.get("facts")
    if not isinstance(facts_raw, Mapping):
        return OrchestrateResult.failure(
            "invalid_response", "LLM response is missing a 'facts' object"
        )
    try:
        facts = parse_facts(skill_id, facts_raw)
    except ConstructorError as exc:
        return OrchestrateResult.failure("invalid_facts", str(exc))

    # ---- 2b) entity scope (Contract F19): explicit > single-active > NULL.
    #          More than one active entity without an explicit id is a
    #          structured error — never a silent global draft.
    try:
        scope_id: uuid.UUID | None = await resolve_entity_scope(db, entity_id)
    except AmbiguousEntityError as exc:
        return OrchestrateResult.failure("ambiguous_entity", exc.message)

    # ---- 3) constructor = CONSTRUCTORS[skill_id] (F4) ---------------------
    try:
        accounts = await CoaService.list_accounts(db, entity_id=scope_id)
        codes = resolve_from_coa(
            [{"code": a.code, "category": a.category, "type": a.type} for a in accounts]
        )
        entry = CONSTRUCTORS[skill_id](facts, codes)
    except ConstructorError as exc:
        return OrchestrateResult.failure("constructor_error", str(exc))

    # ---- 4) resolve account codes via the COA -> PostingCreate rows ------
    legs: list[PreviewLeg] = []
    postings: list[PostingCreate] = []
    for leg in entry.legs:
        account = await CoaService.get_account_by_code(
            db, leg.account, entity_id=scope_id
        )
        if account is None:
            return OrchestrateResult.failure(
                "constructor_error",
                f"account code {leg.account!r} is not in the chart of accounts",
            )
        postings.append(
            PostingCreate(
                account_id=account.id,
                debit_amount=leg.debit_amount,
                credit_amount=leg.credit_amount,
                description=leg.description,
            )
        )
        legs.append(
            PreviewLeg(
                code=leg.account,
                name=account.name,
                account_class=leg.account_class,
                debit_pence=leg.debit_amount,
                credit_pence=leg.credit_amount,
                description=leg.description,
            )
        )

    total_pence = sum(leg.debit_amount for leg in entry.legs)
    description = message.strip()[:300] or skill_id

    # ---- 5) draft via TransactionService.create_transaction --------------
    try:
        transaction = await TransactionService.create_transaction(
            db,
            TransactionCreate(
                description=description,
                effective_date=_derive_effective_date(facts),
                idempotency_key=uuid.uuid4(),
                postings=postings,
            ),
            entity_id=scope_id,
        )
    except TransactionServiceError as exc:
        return OrchestrateResult.failure("transaction_error", exc.message)
    except ValueError as exc:  # pydantic validation of TransactionCreate
        return OrchestrateResult.failure("transaction_error", str(exc))

    # ---- 6) PREVIEW (nothing posted) --------------------------------------
    return OrchestrateResult.preview(
        transaction_id=transaction.id,
        skill_id=skill_id,
        legs=legs,
        total_pence=total_pence,
        formance_preview=entry.formance_postings,
    )


async def approve_entry(
    db: Any,
    transaction_id: uuid.UUID,
    ledger: str | None = None,
    entity_id: uuid.UUID | None = None,
) -> PostResult:
    """Post a draft: assigns JE ref, mirrors to Formance, verifies parity.

    Ledger resolution NEVER consults ``get_active_ledger``: the Formance
    mirror follows the TRANSACTION's own ``entity_id`` (F18's per-row
    resolution inside ``post_transaction``), and the post-approve
    ``verify_parity`` runs on that same ledger WITH the transaction's
    entity scope — an explicit ``entity_id`` argument overrides the parity
    scope (the routes pass their resolved scope), otherwise the tx row
    decides.

    Raises TransactionNotFoundError / TransactionNotDraftError /
    UnbalancedTransactionError from the service layer.  If parity reports a
    drift the result is still returned with ``parity_ok=False`` — posting
    already happened and parity flags drift, it does not roll back.
    """
    transaction = await TransactionService.post_transaction(
        db, transaction_id, ledger=ledger
    )
    resolved_ledger = ledger or await get_ledger_for_entity(
        db, transaction.entity_id
    )
    parity_scope: uuid.UUID | None = (
        entity_id if entity_id is not None else transaction.entity_id
    )
    report: ParityReport = await verify_parity(
        db, resolved_ledger, entity_id=parity_scope
    )
    return PostResult(
        transaction=TransactionService._transaction_to_response(transaction),
        parity_ok=report.ok,
        parity_report=report,
    )


async def reject_entry(
    db: Any,
    transaction_id: uuid.UUID,
    entity_id: uuid.UUID | None = None,
) -> RejectResult:
    """Discard a draft. Posted/reversed rows are append-only — never deleted.

    ``entity_id`` (Contract F20) scopes the discard: a draft stamped with
    a DIFFERENT entity — including the NULL legacy scope — is refused with
    :class:`EntityScopeError` (404).  ``None`` keeps the legacy unscoped
    discard.

    Raises TransactionNotFoundError / TransactionNotDraftError from the
    service layer.
    """
    if entity_id is not None:
        draft = await TransactionService.get_transaction(db, transaction_id)
        if draft is None:
            raise TransactionNotFoundError(transaction_id)
        if draft.entity_id != entity_id:
            raise EntityScopeError(transaction_id, draft.entity_id, entity_id)
    await TransactionService.discard_draft(db, transaction_id)
    return RejectResult(transaction_id=transaction_id)
