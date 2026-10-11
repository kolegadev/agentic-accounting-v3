"""Contract F29 unit tests — orchestrator QUERY mode (deterministic answers).

The LLM only classifies a question into a ``query_type``; every number in
the answer payload comes from the report engine / COA / contacts /
transaction services and stays INTEGER PENCE.  Nothing is drafted, nothing
is posted, and the LLM never narrates a figure.

The DB is an AsyncMock satisfying the CoaService / ContactService /
TransactionService query patterns; the report engine is monkeypatched at
its orchestrator seam.  No real LLM, database or ledger is touched.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.constructors.facts import QUERY_TYPES, QueryFacts
from src.models.account import Account
from src.models.contact import Contact
from src.models.transaction import Posting, Transaction
from src.services.contact_service import ContactService
from src.services.orchestrator import build_system_prompt, orchestrate_message

NOW = datetime(2026, 10, 11, 12, 0, 0, tzinfo=UTC)
TODAY = datetime.now(UTC).date()

TB_ACCOUNTS = [
    {
        "code": "2220",
        "name": "Input VAT",
        "class": "Liability",
        "balance": 2000,
        "debit": 2000,
        "credit": 0,
    },
    {
        "code": "5000",
        "name": "Office Expenses",
        "class": "Expense",
        "balance": 10000,
        "debit": 10000,
        "credit": 0,
    },
    {
        "code": "2000",
        "name": "Trade Creditors",
        "class": "Liability",
        "balance": -12000,
        "debit": 0,
        "credit": 12000,
    },
]
TB_REPORT = {
    "report": "trial_balance",
    "as_at": date(2026, 10, 11),
    "accounts": TB_ACCOUNTS,
    "total_debits": 12000,
    "total_credits": 12000,
    "identity_ok": True,
}


def _account(code: str, name: str, category: str, type_: str) -> Account:
    return Account(
        id=uuid.uuid4(),
        code=code,
        name=name,
        category=category,
        type=type_,
        is_active=True,
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.fixture
def coa() -> dict[str, Account]:
    return {
        "1000": _account("1000", "Bank Current", "Asset", "Bank"),
        "1100": _account("1100", "Trade Debtors", "Asset", "CurrentAsset"),
        "2000": _account("2000", "Trade Creditors", "Liability", "CurrentLiability"),
        "2210": _account("2210", "Output VAT", "Liability", "CurrentLiability"),
        "2220": _account("2220", "Input VAT", "Liability", "CurrentLiability"),
        "4000": _account("4000", "Sales Revenue", "Revenue", "Revenue"),
        "5000": _account("5000", "Office Expenses", "Expense", "Expense"),
    }


def _result(*, scalar=None, scalars=None, count=None):
    """MagicMock mimicking an AsyncResult (incl. the count's scalar_one)."""
    m = MagicMock()
    m.scalar_one_or_none.return_value = scalar
    m.scalar_one.return_value = count
    m.scalars.return_value.all.return_value = scalars if scalars is not None else []
    return m


def _mock_db() -> AsyncMock:
    db = AsyncMock()
    db.added = []
    db.add = MagicMock(side_effect=db.added.append)
    return db


def _capturing_execute(results: list):
    """An execute() that records every statement and pops canned results."""
    captured: list[str] = []
    queue = list(results)

    async def _execute(stmt, *args, **kwargs):
        captured.append(str(stmt))
        return queue.pop(0)

    return _execute, captured


def _query_llm(payload: dict) -> AsyncMock:
    async def _transport(system_prompt: str, user_message: str) -> str:
        return json.dumps(payload)

    return _transport


def _query_payload(query_type: str, account_code: str | None = None) -> dict:
    facts: dict[str, object] = {"query_type": query_type}
    if account_code is not None:
        facts["account_code"] = account_code
    return {"skill_id": "query", "facts": facts}


def _draft_transaction() -> Transaction:
    tx_id = uuid.uuid4()
    tx = Transaction(
        id=tx_id,
        reference=None,
        description="Supplier invoice — pending draft",
        currency="GBP",
        status="draft",
        effective_date=date(2026, 10, 1),
        total_amount=12000,
        created_at=NOW,
        updated_at=NOW,
    )
    tx.postings = [
        Posting(
            id=uuid.uuid4(),
            transaction_id=tx_id,
            account_id=uuid.uuid4(),
            debit_amount=12000,
            credit_amount=0,
            created_at=NOW,
        )
    ]
    return tx


# ---------------------------------------------------------------------------
# Prompt + facts model
# ---------------------------------------------------------------------------


def test_build_system_prompt_includes_query_skill_and_rules() -> None:
    """The LLM must know the query skill exists, its query_types, and that
    it must never compute a number itself."""
    prompt = build_system_prompt()

    assert '"skill_id": "query"' in prompt
    assert '"query_type"' in prompt
    for query_type in ("trial_balance", "account_balance", "accounts", "contacts", "pending"):
        assert query_type in prompt
    assert "NEVER compute, estimate or state a number" in prompt
    # The 10 transaction skills are still advertised.
    assert '"skill_id": "supplier-invoice"' in prompt


def test_query_facts_model_shape() -> None:
    facts = QueryFacts.model_validate({"query_type": "account_balance", "account_code": "1000"})
    assert facts.query_type == "account_balance"
    assert facts.account_code == "1000"

    bare = QueryFacts.model_validate({"query_type": "trial_balance"})
    assert bare.account_code is None

    assert QUERY_TYPES == frozenset(
        {"trial_balance", "account_balance", "accounts", "contacts", "pending"}
    )


# ---------------------------------------------------------------------------
# trial_balance / account_balance — the report engine, entity-scoped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_trial_balance_answers_real_data_without_drafting(
    coa: dict[str, Account], monkeypatch
) -> None:
    db = _mock_db()
    db.execute.side_effect = [
        _result(scalars=[]),  # resolve_entity_scope — no entities (legacy)
        _result(scalars=list(coa.values())),  # list_accounts (prompt COA)
    ]
    tb = AsyncMock(return_value=TB_REPORT)
    monkeypatch.setattr("src.services.orchestrator.generate_tb", tb)

    result = await orchestrate_message(
        db, "what's my trial balance?", llm=_query_llm(_query_payload("trial_balance"))
    )

    assert result.status == "answer"
    assert result.query_type == "trial_balance"
    assert result.error is None
    assert result.transaction_id is None
    assert result.data["trial_balance"] == TB_ACCOUNTS
    assert result.data["total_debits"] == 12000
    assert result.data["total_credits"] == 12000
    assert result.data["as_at"] == "2026-10-11"
    assert all(
        isinstance(row["debit"], int) and isinstance(row["credit"], int)
        for row in result.data["trial_balance"]
    )
    # Legacy scope: the default ledger, no entity, today's date.
    tb.assert_awaited_once()
    assert tb.call_args.args[1] == "main"
    assert tb.call_args.kwargs["as_at"] == TODAY
    assert tb.call_args.kwargs["entity_id"] is None
    # A question never drafts or posts anything.
    assert [o for o in db.added if isinstance(o, Transaction)] == []


@pytest.mark.asyncio
async def test_query_trial_balance_is_entity_scoped(
    coa: dict[str, Account], monkeypatch
) -> None:
    entity_id = uuid.uuid4()
    entity = MagicMock()
    entity.id = entity_id
    entity.ledger_name = "entity-ledger"
    db = _mock_db()
    db.execute.side_effect = [
        _result(scalars=list(coa.values())),  # list_accounts (prompt COA)
        _result(scalar=entity),  # get_ledger_for_entity
    ]
    tb = AsyncMock(return_value=TB_REPORT)
    monkeypatch.setattr("src.services.orchestrator.generate_tb", tb)

    result = await orchestrate_message(
        db,
        "trial balance please",
        llm=_query_llm(_query_payload("trial_balance")),
        entity_id=entity_id,
    )

    assert result.status == "answer"
    assert tb.call_args.args[1] == "entity-ledger"
    assert tb.call_args.kwargs["entity_id"] == entity_id


@pytest.mark.asyncio
async def test_query_account_balance_filters_one_row(
    coa: dict[str, Account], monkeypatch
) -> None:
    db = _mock_db()
    db.execute.side_effect = [
        _result(scalars=[]),  # resolve_entity_scope — no entities (legacy)
        _result(scalars=list(coa.values())),  # list_accounts (prompt COA)
    ]
    monkeypatch.setattr(
        "src.services.orchestrator.generate_tb", AsyncMock(return_value=TB_REPORT)
    )

    result = await orchestrate_message(
        db,
        "what's the balance on account 5000?",
        llm=_query_llm(_query_payload("account_balance", "5000")),
    )

    assert result.status == "answer"
    assert result.query_type == "account_balance"
    assert result.data["account"]["code"] == "5000"
    assert result.data["trial_balance"] == [TB_ACCOUNTS[1]]
    assert result.data["total_debits"] == 10000
    assert result.data["total_credits"] == 0
    assert result.data["account"]["debit"] == 10000  # integer pence


@pytest.mark.asyncio
async def test_query_account_balance_unknown_code_is_structured_error(
    coa: dict[str, Account], monkeypatch
) -> None:
    db = _mock_db()
    db.execute.side_effect = [
        _result(scalars=[]),  # resolve_entity_scope — no entities (legacy)
        _result(scalars=list(coa.values())),  # list_accounts (prompt COA)
        _result(scalar=None),  # get_account_by_code fallback — no such code
    ]
    monkeypatch.setattr(
        "src.services.orchestrator.generate_tb", AsyncMock(return_value=TB_REPORT)
    )

    result = await orchestrate_message(
        db,
        "balance on 9999?",
        llm=_query_llm(_query_payload("account_balance", "9999")),
    )

    assert result.status == "error"
    assert result.error["type"] == "account_not_found"
    assert "9999" in result.error["message"]
    assert result.data is None
    assert [o for o in db.added if isinstance(o, Transaction)] == []


@pytest.mark.asyncio
async def test_query_account_balance_zero_row_for_posted_nothing_account(
    coa: dict[str, Account], monkeypatch
) -> None:
    """A COA account with no postings is NOT on the TB; the answer is a
    zero row, not a misleading account_not_found (verifier fix-list)."""
    db = _mock_db()
    db.execute.side_effect = [
        _result(scalars=[]),  # resolve_entity_scope — no entities (legacy)
        _result(scalars=list(coa.values())),  # list_accounts (prompt COA)
        _result(scalar=coa["1000"]),  # get_account_by_code fallback — exists
    ]
    monkeypatch.setattr(
        "src.services.orchestrator.generate_tb", AsyncMock(return_value=TB_REPORT)
    )

    result = await orchestrate_message(
        db,
        "balance on 1000?",
        llm=_query_llm(_query_payload("account_balance", "1000")),
    )

    assert result.status == "answer"
    row = result.data["account"]
    assert row["code"] == "1000"
    assert row["name"] == "Bank Current"
    assert row["balance"] == 0
    assert row["debit"] == 0
    assert row["credit"] == 0


# ---------------------------------------------------------------------------
# accounts / contacts / pending — service summaries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_accounts_summarizes_the_entity_coa(
    coa: dict[str, Account],
) -> None:
    db = _mock_db()
    db.execute.side_effect = [
        _result(scalars=[]),  # resolve_entity_scope — no entities (legacy)
        _result(scalars=list(coa.values())),  # list_accounts (prompt COA)
        _result(scalars=list(coa.values())),  # list_accounts (the answer)
    ]

    result = await orchestrate_message(
        db, "list my accounts", llm=_query_llm(_query_payload("accounts"))
    )

    assert result.status == "answer"
    assert result.query_type == "accounts"
    assert result.data["count"] == len(coa)
    assert result.data["accounts"][0] == {
        "code": "1000",
        "name": "Bank Current",
        "category": "Asset",
        "type": "Bank",
    }


@pytest.mark.asyncio
async def test_query_contacts_lists_global_contacts(
    coa: dict[str, Account],
) -> None:
    contact = Contact(
        id=uuid.uuid4(),
        type="customer",
        name="Acme Corp",
        company=None,
        email="info@acme.com",
        phone=None,
        address_line1=None,
        address_line2=None,
        city=None,
        postcode=None,
        country="GB",
        vat_number=None,
        payment_terms="Net 30",
        default_gl_account_id=None,
        currency="GBP",
        status="active",
        total_invoiced=0,
        total_paid=0,
        total_owing=0,
        created_at=NOW,
        updated_at=NOW,
    )
    db = _mock_db()
    db.execute.side_effect = [
        _result(scalars=[]),  # resolve_entity_scope — no entities (legacy)
        _result(scalars=list(coa.values())),  # list_accounts (prompt COA)
        _result(count=1),  # list_contacts count
        _result(scalars=[contact]),  # list_contacts fetch
    ]

    result = await orchestrate_message(
        db, "who are my contacts?", llm=_query_llm(_query_payload("contacts"))
    )

    assert result.status == "answer"
    assert result.query_type == "contacts"
    assert result.data["count"] == 1
    assert result.data["contacts"] == [
        {
            "id": str(contact.id),
            "name": "Acme Corp",
            "type": "customer",
            "email": "info@acme.com",
        }
    ]


@pytest.mark.asyncio
async def test_query_pending_lists_drafts_in_pence_and_is_entity_scoped() -> None:
    entity_id = uuid.uuid4()
    draft = _draft_transaction()
    db = _mock_db()
    execute, captured = _capturing_execute(
        [
            _result(scalars=[]),  # list_accounts (prompt COA)
            _result(count=1),  # list_transactions count
            _result(scalars=[draft]),  # list_transactions fetch
        ]
    )
    db.execute = execute

    result = await orchestrate_message(
        db,
        "what's pending?",
        llm=_query_llm(_query_payload("pending")),
        entity_id=entity_id,
    )

    assert result.status == "answer"
    assert result.query_type == "pending"
    assert result.data["count"] == 1
    assert result.data["pending"] == [
        {
            "transaction_id": str(draft.id),
            "reference": None,
            "description": "Supplier invoice — pending draft",
            "effective_date": "2026-10-01",
            "total_amount": 12000,  # integer pence
            "postings": 1,
        }
    ]
    # Scoped: only this entity's drafts are listed.
    assert "transactions.entity_id = :entity_id" in captured[1]
    assert "transactions.status = :status_1" in captured[1]


@pytest.mark.asyncio
async def test_query_missing_facts_object_is_structured_error(
    coa: dict[str, Account],
) -> None:
    db = _mock_db()
    db.execute.side_effect = [
        _result(scalars=[]),  # resolve_entity_scope — no entities (legacy)
        _result(scalars=list(coa.values())),  # list_accounts (prompt COA)
    ]

    result = await orchestrate_message(
        db, "trial balance", llm=_query_llm({"skill_id": "query"})
    )

    assert result.status == "error"
    assert result.error["type"] == "invalid_response"


# ---------------------------------------------------------------------------
# Structured query errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_unknown_type_is_invalid_query(coa: dict[str, Account]) -> None:
    db = _mock_db()
    db.execute.side_effect = [
        _result(scalars=[]),  # resolve_entity_scope — no entities (legacy)
        _result(scalars=list(coa.values())),  # list_accounts (prompt COA)
    ]

    result = await orchestrate_message(
        db, "what's the weather?", llm=_query_llm(_query_payload("weather"))
    )

    assert result.status == "error"
    assert result.error["type"] == "invalid_query"
    assert "weather" in result.error["message"]
    # The engine was never consulted.
    assert [o for o in db.added if isinstance(o, Transaction)] == []


@pytest.mark.asyncio
async def test_query_invalid_facts_is_structured_error(coa: dict[str, Account]) -> None:
    db = _mock_db()
    db.execute.side_effect = [
        _result(scalars=[]),  # resolve_entity_scope — no entities (legacy)
        _result(scalars=list(coa.values())),  # list_accounts (prompt COA)
    ]

    result = await orchestrate_message(
        db, "hmm", llm=_query_llm({"skill_id": "query", "facts": {"account_code": "1000"}})
    )

    assert result.status == "error"
    assert result.error["type"] == "invalid_facts"


@pytest.mark.asyncio
async def test_query_engine_failure_is_structured_error(
    coa: dict[str, Account], monkeypatch
) -> None:
    db = _mock_db()
    db.execute.side_effect = [
        _result(scalars=[]),  # resolve_entity_scope — no entities (legacy)
        _result(scalars=list(coa.values())),  # list_accounts (prompt COA)
    ]
    monkeypatch.setattr(
        "src.services.orchestrator.generate_tb",
        AsyncMock(side_effect=RuntimeError("formance unreachable")),
    )

    result = await orchestrate_message(
        db, "trial balance", llm=_query_llm(_query_payload("trial_balance"))
    )

    assert result.status == "error"
    assert result.error["type"] == "query_error"
    assert "formance unreachable" in result.error["message"]


# ---------------------------------------------------------------------------
# Query mode never hijacks the transaction path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transaction_message_still_routes_to_the_constructor(
    coa: dict[str, Account],
) -> None:
    db = _mock_db()
    db.execute.side_effect = [
        _result(scalars=[]),  # resolve_entity_scope — legacy
        _result(scalars=list(coa.values())),  # list_accounts
        _result(scalar=coa["5000"]),  # Dr expense
        _result(scalar=coa["2220"]),  # Dr input VAT
        _result(scalar=coa["2000"]),  # Cr trade creditors
        _result(scalar=None),  # idempotency check
        _result(scalars=[coa["5000"], coa["2220"], coa["2000"]]),
    ]
    contact = MagicMock()
    with patch.object(
        ContactService, "find_or_create", AsyncMock(return_value=(contact, True))
    ):
        result = await orchestrate_message(
            db,
            "Supplier invoice INV-1 for £120",
            llm=_query_llm(
                {
                    "skill_id": "supplier-invoice",
                    "facts": {
                        "supplier_name": "Acme",
                        "invoice_ref": "INV-1",
                        "invoice_date": "2026-10-01",
                        "lines": [
                            {
                                "description": "paper",
                                "net_pence": 10000,
                                "vat_rate": "20%",
                                "expense_account_code": "5000",
                            }
                        ],
                    },
                }
            ),
        )

    assert result.status == "preview"
    assert result.skill_id == "supplier-invoice"
    assert result.total_pence == 12000
    assert result.data is None  # not an answer


def test_query_skill_is_not_a_constructor() -> None:
    """The query skill must stay out of CONSTRUCTORS — it can never reach
    the constructor path."""
    from src.constructors import CONSTRUCTORS

    assert "query" not in CONSTRUCTORS
