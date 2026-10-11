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
from datetime import UTC, date, datetime, timezone
from typing import Any

import httpx
from pydantic import ValidationError

from src.constructors import CONSTRUCTORS, ConstructorError, resolve_from_coa
from src.constructors.facts import (
    FACTS_MODELS,
    QUERY_TYPES,
    QueryFacts,
    parse_facts,
)
from src.services.coa_service import CoaService
from src.services.contact_service import ContactService
from src.services.entity_service import (
    AmbiguousEntityError,
    get_ledger_for_entity,
    resolve_entity_scope,
)
from src.services.formance_reports import generate_tb
from src.services.ledger_parity import ParityReport, verify_parity
from src.services import llm_config as llm_cfg
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
    "query": (
        "Answer a QUESTION about the books (read-only, deterministic): trial "
        "balance, a single account balance, the account list, contacts, or "
        "pending drafts — never the LLM's own numbers"
    ),
}

# ---------------------------------------------------------------------------
# Contract F29 — query mode + contact auto-creation
# ---------------------------------------------------------------------------

#: The skill that answers questions instead of building a draft.  It is NOT
#: in ``CONSTRUCTORS``: it never reaches the constructor path.
QUERY_SKILL_ID = "query"

#: The facts field carrying the counterparty name, per contact-bearing
#: skill (the exact F4 field names: ``supplier_name`` / ``customer_name``).
CONTACT_NAME_FACT_FIELDS: dict[str, tuple[str, ...]] = {
    "supplier-invoice": ("supplier_name",),
    "supplier-payment": ("supplier_name",),
    "sales-invoice": ("customer_name",),
    "customer-receipt": ("customer_name",),
}

#: Contact type for auto-created counterparties, per skill family.
CONTACT_TYPE_BY_SKILL: dict[str, str] = {
    "supplier-invoice": "supplier",
    "supplier-payment": "supplier",
    "sales-invoice": "customer",
    "customer-receipt": "customer",
}

#: Optional contact detail fields, consulted in order (none of the current
#: F4 models carry them, but a future facts model may).
CONTACT_EMAIL_FACT_FIELDS: tuple[str, ...] = (
    "customer_email",
    "supplier_email",
    "email",
)
CONTACT_VAT_FACT_FIELDS: tuple[str, ...] = (
    "customer_vat_number",
    "supplier_vat_number",
    "vat_number",
)

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

    ``status`` is ``"preview"`` (a draft transaction was created and awaits
    approval), ``"answer"`` (Contract F29 — a read-only query answered from
    the real ledger: ``query_type`` + integer-pence ``data``, never LLM
    narration), or ``"error"`` (a structured error dict is set — never a
    raw traceback).
    """

    status: str
    transaction_id: uuid.UUID | None = None
    skill_id: str | None = None
    legs: list[PreviewLeg] | None = None
    total_pence: int | None = None
    formance_preview: list[dict[str, str]] | None = None
    # Contract F29 — {id, name, created} for the auto-created/found contact.
    contact: dict[str, Any] | None = None
    # Contract F29 — the deterministic query answer (status == "answer").
    query_type: str | None = None
    data: dict[str, Any] | None = None
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
        contact: dict[str, Any] | None = None,
    ) -> OrchestrateResult:
        return cls(
            status="preview",
            transaction_id=transaction_id,
            skill_id=skill_id,
            legs=legs,
            total_pence=total_pence,
            formance_preview=formance_preview,
            contact=contact,
        )

    @classmethod
    def answer(cls, query_type: str, data: dict[str, Any]) -> OrchestrateResult:
        """A deterministic, read-only answer (Contract F29) — all amounts in
        the payload are integer pence; the UI formats them."""
        return cls(status="answer", query_type=query_type, data=data)

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


def build_system_prompt(
    history: list[dict[str, Any]] | None = None,
    coa: list[dict[str, Any]] | None = None,
) -> str:
    """System prompt listing all 10 skills with their Facts JSON schemas.

    Schemas come from the F4 pydantic models via ``.model_json_schema()``;
    rules enforce integer pence, ISO dates, the vat_rate enum, and the
    closest-skill-or-clarification fallback.  When ``coa`` rows are
    provided (code/name/category/type), they are listed as the ONLY valid
    account codes so the LLM never hallucinates one.
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
    # Contract F29: the read-only query skill rides alongside the 10
    # transaction skills — same envelope, deterministic answer.
    skill_snippets.append(
        {
            "skill_id": QUERY_SKILL_ID,
            "description": SKILL_DESCRIPTIONS[QUERY_SKILL_ID],
            "facts_schema": QueryFacts.model_json_schema(),
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

    coa_section = ""
    if coa:
        lines = [
            f"- {r.get('code')} | {r.get('name')} | {r.get('category')} ({r.get('type')})"
            for r in coa
        ]
        coa_section = (
            "## Chart of accounts (the ONLY valid account codes)\n\n"
            + "\n".join(lines)
            + "\n\n"
        )

    # Contract F23: ground the prompt in the REAL current date so the LLM
    # never hallucinates a stale date (drafts were being dated 2024-01-01).
    today = datetime.now(timezone.utc).date()

    return (
        "You are the transaction orchestrator for Agentic Accounting, a "
        "double-entry accounting system for UK small businesses.\n\n"
        "Your ONLY job is to classify the user's message into ONE of the "
        "skills below — a transaction skill (record an entry) or the query "
        "skill (answer a question about the books) — and extract the facts "
        "that skill's schema requires. You NEVER construct journal entries, "
        "compute debit/credit legs, choose which account is debited or "
        "credited, post anything, or compute/narrate any balance or total "
        "— a deterministic layer does that after you return facts.\n\n"
        "## Skills and their Facts JSON schemas\n\n"
        f"{skills_json}\n\n"
        f"{coa_section}"
        "## Rules\n"
        "1. Respond with a single JSON object and nothing else:\n"
        '   {"skill_id": "<one of the skill ids above>", "facts": {<matching schema>}}\n'
        "2. ALL money amounts are INTEGER PENCE. £50.00 -> 5000. Never use "
        "floats or decimals for money.\n"
        f"3. Dates are ISO 8601 (YYYY-MM-DD). Today is {today}.\n"
        '4. vat_rate must be one of the enum values in the schema: '
        '"20%", "5%", "0%", "exempt".\n'
        "5. Account codes go ONLY into the facts fields that carry account "
        "codes (e.g. bank_account_code, expense_account_code). They MUST "
        "come from the Chart of accounts section above — NEVER invent a "
        "code. If no listed account matches the message (e.g. buying a "
        "laptop when only an Office Equipment expense account exists), pick "
        "the CLOSEST listed account for that field.\n"
        "6. Choose the CLOSEST matching skill. Never invent a skill id.\n"
        '7. If the message is ambiguous or you cannot extract the required '
        'facts, respond with {"skill_id": null, "clarification": "<what you '
        'need to know>"}.\n'
        "8. If the user asks a QUESTION about their books (a balance, the "
        "trial balance, their accounts, contacts, or pending drafts) instead "
        "of asking you to record a transaction, choose the query skill: "
        '{"skill_id": "query", "facts": {"query_type": "<one of '
        'trial_balance, account_balance, accounts, contacts, pending>",'
        ' "account_code": "<4-digit code, only for account_balance>"}}. Use '
        "account_balance ONLY when the question names one specific account "
        "(or a code, e.g. from a clicked table row); otherwise use "
        "trial_balance. NEVER compute, estimate or state a number or a "
        "balance yourself — the deterministic layer answers with the real "
        "ledger data.\n\n"
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
    api_key = llm_cfg.get_api_key()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload = {
        "model": llm_cfg.get_model(),
        "temperature": 0,
        "max_tokens": LLM_MAX_TOKENS,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
    }

    try:
        async with httpx.AsyncClient(timeout=LLM_TIMEOUT) as client:
            response = await client.post(llm_cfg.get_api_url(), json=payload, headers=headers)
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


def _derive_contact_identity(
    skill_id: str, facts: Any
) -> tuple[str | None, str | None, str | None]:
    """(name, email, vat_number) for the contact-bearing skills.

    Contract F29: the counterparty name comes from the skill's own fact
    field — ``supplier_name`` for supplier-invoice/supplier-payment,
    ``customer_name`` for sales-invoice/customer-receipt (the exact F4
    field names).  Email/VAT pass through when a facts model carries them.
    """
    fields = CONTACT_NAME_FACT_FIELDS.get(skill_id)
    if not fields:
        return None, None, None
    name: str | None = None
    for field in fields:
        value = getattr(facts, field, None)
        if isinstance(value, str) and value.strip():
            name = value.strip()
            break
    if not name:
        return None, None, None

    def _first(candidates: tuple[str, ...]) -> str | None:
        for field in candidates:
            value = getattr(facts, field, None)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    return name, _first(CONTACT_EMAIL_FACT_FIELDS), _first(CONTACT_VAT_FACT_FIELDS)


async def _resolve_contact(
    db: Any, skill_id: str, facts: Any
) -> tuple[Any, bool] | None:
    """Find-or-create the counterparty contact — never blocks the draft.

    Returns ``(contact, created)`` or ``None`` (skill without a party name,
    or a contact-service failure).  A failure is logged as a warning and
    degrades to a draft WITHOUT a contact: the drop is best-effort, the
    transaction is not negotiable.
    """
    name, email, vat_number = _derive_contact_identity(skill_id, facts)
    if not name:
        return None
    contact_type = CONTACT_TYPE_BY_SKILL.get(skill_id, "supplier")
    try:
        return await ContactService.find_or_create(
            db, name, email=email, vat_number=vat_number, type=contact_type
        )
    except Exception as exc:  # noqa: BLE001 — degrade, never block the draft
        logger.warning(
            "Contact auto-creation failed for %r (%s: %s) — continuing "
            "without a contact",
            name,
            exc.__class__.__name__,
            exc,
        )
        try:
            # A failed statement aborts the surrounding transaction on
            # Postgres; clear it so the draft still commits.
            await db.rollback()
        except Exception:  # best-effort recovery only
            logger.warning("Contact auto-creation rollback failed", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Query mode (Contract F29) — deterministic, read-only answers
# ---------------------------------------------------------------------------


async def _answer_trial_balance(
    db: Any,
    scope_id: uuid.UUID | None,
    facts: QueryFacts,
    query_type: str,
) -> OrchestrateResult:
    """Trial balance (and the single-account variant) from the report engine.

    Amounts stay INTEGER PENCE in the payload — the UI formats them.
    """
    ledger = await get_ledger_for_entity(db, scope_id)
    report = await generate_tb(
        db,
        ledger,
        as_at=datetime.now(UTC).date(),
        entity_id=scope_id,
    )
    rows = list(report.get("accounts") or [])
    as_at = report.get("as_at")
    as_at_value = as_at.isoformat() if isinstance(as_at, date) else None

    if query_type == "account_balance":
        code = (facts.account_code or "").strip()
        row = next((r for r in rows if r.get("code") == code), None)
        if row is None and code:
            # Not on the TB yet — the account exists but has no postings:
            # answer a zero balance instead of a misleading error.
            account = await CoaService.get_account_by_code(
                db, code, entity_id=scope_id
            )
            if account is not None:
                row = {
                    "code": account.code,
                    "name": account.name,
                    "class": f"{account.category} ({account.type})",
                    "balance": 0,
                    "debit": 0,
                    "credit": 0,
                }
        if row is None:
            return OrchestrateResult.failure(
                "account_not_found",
                f"account code {code!r} is not on the trial balance"
                if code
                else "account_balance needs an account_code",
            )
        return OrchestrateResult.answer(
            "account_balance",
            {
                "account": row,
                "trial_balance": [row],
                "total_debits": row.get("debit", 0),
                "total_credits": row.get("credit", 0),
                "as_at": as_at_value,
            },
        )

    return OrchestrateResult.answer(
        "trial_balance",
        {
            "trial_balance": rows,
            "total_debits": report.get("total_debits", 0),
            "total_credits": report.get("total_credits", 0),
            "as_at": as_at_value,
        },
    )


async def _answer_query(
    db: Any,
    facts_raw: Any,
    scope_id: uuid.UUID | None,
) -> OrchestrateResult:
    """Answer a read-only question with DETERMINISTIC data (Contract F29).

    The LLM only classified the question into a ``query_type``; every
    figure below comes from the report engine / COA / contacts /
    transaction services, in INTEGER PENCE.  Nothing is drafted or posted.
    """
    if not isinstance(facts_raw, Mapping):
        return OrchestrateResult.failure(
            "invalid_response", "LLM response is missing a 'facts' object"
        )
    try:
        facts = QueryFacts.model_validate(dict(facts_raw))
    except ValidationError as exc:
        return OrchestrateResult.failure(
            "invalid_facts", f"invalid query facts: {exc}"
        )

    query_type = facts.query_type.strip() if isinstance(facts.query_type, str) else ""
    if query_type not in QUERY_TYPES:
        return OrchestrateResult.failure(
            "invalid_query",
            f"unknown query_type {facts.query_type!r} — expected one of "
            + ", ".join(sorted(QUERY_TYPES)),
        )

    try:
        if query_type in ("trial_balance", "account_balance"):
            return await _answer_trial_balance(db, scope_id, facts, query_type)

        if query_type == "accounts":
            accounts = await CoaService.list_accounts(db, entity_id=scope_id)
            return OrchestrateResult.answer(
                "accounts",
                {
                    "accounts": [
                        {
                            "code": a.code,
                            "name": a.name,
                            "category": a.category,
                            "type": a.type,
                        }
                        for a in accounts
                    ],
                    "count": len(accounts),
                },
            )

        if query_type == "contacts":
            contacts, total = await ContactService.list_contacts(db)
            return OrchestrateResult.answer(
                "contacts",
                {
                    "contacts": [
                        {
                            "id": str(c.id),
                            "name": c.name,
                            "type": c.type,
                            "email": c.email,
                        }
                        for c in contacts
                    ],
                    "count": total,
                },
            )

        # pending — drafts awaiting approval, entity-scoped, pence intact
        transactions, total = await TransactionService.list_transactions(
            db, status="draft", entity_id=scope_id
        )
        return OrchestrateResult.answer(
            "pending",
            {
                "pending": [
                    {
                        "transaction_id": str(t.id),
                        "reference": t.reference,
                        "description": t.description,
                        "effective_date": (
                            t.effective_date.isoformat()
                            if isinstance(t.effective_date, date)
                            else None
                        ),
                        "total_amount": t.total_amount,
                        "postings": len(t.postings or []),
                    }
                    for t in transactions
                ],
                "count": total,
            },
        )
    except Exception as exc:  # noqa: BLE001 — a query never raises to the caller
        logger.error(
            "Orchestrator %s query failed: %s: %s",
            query_type,
            exc.__class__.__name__,
            exc,
        )
        return OrchestrateResult.failure(
            "query_error", f"{exc.__class__.__name__}: {exc}"
        )


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

    Returns a preview result (draft created, nothing posted), a
    deterministic query answer (Contract F29: ``status == "answer"`` with
    integer-pence ``data`` — the LLM never narrates a number), or a
    structured error.  ``llm`` is the injectable transport: an async
    callable ``(system_prompt, user_message) -> raw text``; defaults to
    :func:`call_llm`.

    Contact-bearing skills (supplier-invoice, supplier-payment,
    sales-invoice, customer-receipt) resolve-or-create their counterparty
    contact and stamp it on the draft (Contract F29) — a contact failure
    degrades to a draft without a contact, never a blocked transaction.

    ``entity_id`` (Contract F19) is the entity scope: an explicit id wins;
    otherwise the single active entity; otherwise the legacy global scope
    (NULL).  The COA resolution and the stamped draft are entity-scoped;
    more than one active entity without an explicit id fails loud as a
    structured ``ambiguous_entity`` error (never a silent global draft).
    """
    transport: LLMTransport = llm or call_llm

    # ---- 0) entity scope + COA first, so the prompt is grounded in the
    #         REAL chart of accounts (the LLM never hallucinates a code).
    try:
        scope_id: uuid.UUID | None = await resolve_entity_scope(db, entity_id)
    except AmbiguousEntityError as exc:
        return OrchestrateResult.failure("ambiguous_entity", exc.message)
    accounts = await CoaService.list_accounts(db, entity_id=scope_id)
    coa_rows = [
        {"code": a.code, "name": a.name, "category": a.category, "type": a.type}
        for a in accounts
    ]
    system_prompt = build_system_prompt(history, coa=coa_rows)

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
    except LLMError as first_exc:
        # deepseek-flash occasionally answers in prose instead of JSON —
        # one bounded retry with an explicit JSON-only nudge (Contract F35).
        retry_prompt = (
            system_prompt
            + "\n\nIMPORTANT: respond with NOTHING but the JSON object "
            "(no prose, no markdown)."
        )
        try:
            raw = await transport(retry_prompt, message)
            payload = _parse_llm_json(raw)
        except LLMError as exc:
            return OrchestrateResult.failure(
                "invalid_response", f"{first_exc} (retry: {exc})"
            )
        except Exception as exc:  # noqa: BLE001 — injected transports
            return OrchestrateResult.failure(
                "llm_error",
                f"LLM retry failed: {exc.__class__.__name__}: {exc}",
            )

    skill_id = payload.get("skill_id")
    if not skill_id:
        clarification = payload.get("clarification")
        if clarification:
            return OrchestrateResult.failure("clarification", str(clarification))
        return OrchestrateResult.failure(
            "invalid_response",
            "LLM returned neither a skill_id nor a clarification",
        )

    # ---- 1b) QUERY mode (Contract F29) — handled BEFORE the constructor
    #          path: a question is answered from the real data, never
    #          drafted, and never computed by the LLM.
    if skill_id == QUERY_SKILL_ID:
        return await _answer_query(db, payload.get("facts"), scope_id)

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

    # ---- 3) constructor = CONSTRUCTORS[skill_id] (F4) ---------------------
    try:
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

    # ---- 5) contact auto-creation (Contract F29) — best-effort: the
    #         counterparty is resolved/created and stamped on the draft,
    #         and a failure degrades to a draft WITHOUT a contact.
    resolved_contact = await _resolve_contact(db, skill_id, facts)
    contact_payload: dict[str, Any] | None = None
    contact_id: uuid.UUID | None = None
    if resolved_contact is not None:
        contact_obj, contact_created = resolved_contact
        candidate_id = getattr(contact_obj, "id", None)
        if isinstance(candidate_id, uuid.UUID):
            contact_id = candidate_id
        contact_payload = {
            "id": str(candidate_id),
            "name": getattr(contact_obj, "name", None),
            "created": bool(contact_created),
        }

    # ---- 6) draft via TransactionService.create_transaction --------------
    try:
        transaction = await TransactionService.create_transaction(
            db,
            TransactionCreate(
                description=description,
                contact_id=contact_id,
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

    # ---- 7) PREVIEW (nothing posted) --------------------------------------
    return OrchestrateResult.preview(
        transaction_id=transaction.id,
        skill_id=skill_id,
        legs=legs,
        total_pence=total_pence,
        formance_preview=entry.formance_postings,
        contact=contact_payload,
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
