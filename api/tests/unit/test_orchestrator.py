# /Users/johnpellew/agentic-accounting-v3/api/tests/unit/test_orchestrator.py

"""Contract F6 unit tests — orchestrator pipeline with mocked DB + LLM + Formance.

The LLM transport is an injected async callable; the DB is an AsyncMock
satisfying the CoaService/TransactionService query patterns; the Formance
mirror call and the parity verifier are patched at their module seams.
Nothing here touches a real LLM, database, or ledger.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.constructors import ConstructorError
from src.models.account import Account
from src.models.transaction import Posting, Transaction
from src.services.ledger_parity import ParityReport
from src.services.orchestrator import (
    LLMError,
    approve_entry,
    build_system_prompt,
    orchestrate_message,
    reject_entry,
)
from src.services.transaction_service import (
    TransactionNotDraftError,
    TransactionNotFoundError,
    TransactionService,
)

NOW = datetime(2026, 10, 10, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
        "1001": _account("1001", "Deposit Account", "Asset", "Bank"),
        "1100": _account("1100", "Trade Debtors", "Asset", "CurrentAsset"),
        "2000": _account("2000", "Trade Creditors", "Liability", "CurrentLiability"),
        "2030": _account("2030", "PAYE/NI Payable", "Liability", "CurrentLiability"),
        "2210": _account("2210", "Output VAT", "Liability", "CurrentLiability"),
        "2220": _account("2220", "Input VAT", "Liability", "CurrentLiability"),
        "4000": _account("4000", "Sales Revenue", "Revenue", "Revenue"),
        "5000": _account("5000", "Office Expenses", "Expense", "Expense"),
        "5010": _account("5010", "Materials", "Expense", "DirectCost"),
        "6600": _account("6600", "Staff Wages", "Expense", "Expense"),
        "6610": _account("6610", "Employer NI", "Expense", "Expense"),
        "6630": _account("6630", "Pension Contributions", "Expense", "Expense"),
    }


@pytest.fixture
def mock_db(coa: dict[str, Account]) -> AsyncMock:
    """AsyncMock DB that assigns ids on flush and tracks added objects."""
    db = AsyncMock()
    db.added: list = []
    db.flush_calls = 0

    def _add(obj) -> None:
        db.added.append(obj)

    async def _flush() -> None:
        db.flush_calls += 1
        for obj in db.added:
            if isinstance(obj, Transaction) and obj.id is None:
                obj.id = uuid.uuid4()

    db.add = _add
    db.flush = _flush
    db.delete = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    return db


def _result(*, scalar=None, scalars=None):
    """MagicMock mimicking an AsyncResult for one execute call."""
    m = MagicMock()
    m.scalar_one_or_none.return_value = scalar
    m.scalars.return_value.all.return_value = scalars if scalars is not None else []
    return m


async def _llm_returning(raw: str):
    async def transport(system_prompt: str, user_message: str) -> str:
        return raw

    return transport


def _draft_transaction() -> Transaction:
    tx_id = uuid.uuid4()
    tx = Transaction(
        id=tx_id,
        reference=None,
        description="Supplier invoice — test",
        currency="GBP",
        status="draft",
        effective_date=date(2026, 10, 1),
        total_amount=12000,
        created_at=NOW,
        updated_at=NOW,
    )
    bank_id = uuid.uuid4()
    ap_id = uuid.uuid4()
    tx.postings = [
        Posting(
            id=uuid.uuid4(),
            transaction_id=tx_id,
            account_id=ap_id,
            debit_amount=12000,
            credit_amount=0,
            created_at=NOW,
        ),
        Posting(
            id=uuid.uuid4(),
            transaction_id=tx_id,
            account_id=bank_id,
            debit_amount=0,
            credit_amount=12000,
            created_at=NOW,
        ),
    ]
    return tx


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------


def test_build_system_prompt_lists_all_10_skills_with_schemas() -> None:
    prompt = build_system_prompt([{"role": "user", "content": "hi"}])

    for skill_id in (
        "bank-transfer",
        "customer-receipt",
        "depreciation",
        "journal-accrual",
        "opening-balances",
        "payroll",
        "prepayment",
        "sales-invoice",
        "supplier-invoice",
        "supplier-payment",
    ):
        assert f'"skill_id": "{skill_id}"' in prompt
    assert '"facts_schema"' in prompt
    assert "properties" in prompt  # JSON schema content
    assert "INTEGER PENCE" in prompt
    assert "YYYY-MM-DD" in prompt
    assert '"20%", "5%", "0%", "exempt"' in prompt
    assert '"skill_id": null, "clarification"' in prompt
    assert "[user] hi" in prompt


# ---------------------------------------------------------------------------
# orchestrate_message — happy paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orchestrate_supplier_invoice_vat_preview(
    mock_db: AsyncMock, coa: dict[str, Account]
) -> None:
    llm = await _llm_returning(
        '{"skill_id": "supplier-invoice", "facts": {'
        '"supplier_name": "Acme Office Supplies", "invoice_ref": "INV-1001", '
        '"invoice_date": "2026-10-01", "lines": ['
        '{"description": "Printer paper", "net_pence": 10000, '
        '"vat_rate": "20%", "expense_account_code": "5000"}]}}'
    )
    mock_db.execute.side_effect = [
        _result(scalars=[]),  # resolve_entity_scope — no entities (legacy)
        _result(scalars=list(coa.values())),  # list_accounts
        _result(scalar=coa["5000"]),  # get_account_by_code
        _result(scalar=coa["2220"]),
        _result(scalar=coa["2000"]),
        _result(scalar=None),  # idempotency check
        _result(scalars=[coa["5000"], coa["2220"], coa["2000"]]),  # validate accounts
    ]

    result = await orchestrate_message(mock_db, "Supplier invoice INV-1001 for £120", llm=llm)

    assert result.status == "preview"
    assert result.skill_id == "supplier-invoice"
    assert result.transaction_id is not None
    assert result.total_pence == 12000
    assert [(l.code, l.debit_pence, l.credit_pence) for l in result.legs] == [
        ("5000", 10000, 0),
        ("2220", 2000, 0),
        ("2000", 0, 12000),
    ]
    assert result.legs[0].name == "Office Expenses"
    assert result.legs[2].name == "Trade Creditors"
    assert result.formance_preview == [
        {"source": "2000", "destination": "5000", "amount": "100.00", "asset": "GBP/2"},
        {"source": "2000", "destination": "2220", "amount": "20.00", "asset": "GBP/2"},
    ]
    # Draft only — nothing posted, no mirroring.
    drafts = [o for o in mock_db.added if isinstance(o, Transaction)]
    assert len(drafts) == 1
    assert drafts[0].status == "draft"
    assert drafts[0].effective_date == date(2026, 10, 1)
    assert drafts[0].reference is None
    mock_db.commit.assert_called_once()


@pytest.mark.asyncio
async def test_orchestrate_bank_transfer_preview(
    mock_db: AsyncMock, coa: dict[str, Account]
) -> None:
    llm = await _llm_returning(
        '{"skill_id": "bank-transfer", "facts": {'
        '"source_account_code": "1000", "destination_account_code": "1001", '
        '"amount_pence": 50000, "transfer_date": "2026-10-02", '
        '"reference": "sweep"}}'
    )
    mock_db.execute.side_effect = [
        _result(scalars=[]),  # resolve_entity_scope — no entities (legacy)
        _result(scalars=list(coa.values())),
        _result(scalar=coa["1001"]),
        _result(scalar=coa["1000"]),
        _result(scalar=None),
        _result(scalars=[coa["1001"], coa["1000"]]),
    ]

    result = await orchestrate_message(mock_db, "Move £500 from 1000 to 1001", llm=llm)

    assert result.status == "preview"
    assert result.skill_id == "bank-transfer"
    assert result.total_pence == 50000
    assert [(l.code, l.debit_pence, l.credit_pence) for l in result.legs] == [
        ("1001", 50000, 0),
        ("1000", 0, 50000),
    ]
    assert result.formance_preview == [
        {"source": "1000", "destination": "1001", "amount": "500.00", "asset": "GBP/2"}
    ]


@pytest.mark.asyncio
async def test_orchestrate_payroll_preview(
    mock_db: AsyncMock, coa: dict[str, Account]
) -> None:
    llm = await _llm_returning(
        '{"skill_id": "payroll", "facts": {'
        '"period_end_date": "2026-09-30", "pay_date": "2026-09-30", '
        '"gross_pence": 300000, "paye_pence": 40000, "employee_nic_pence": 15000, '
        '"employer_nic_pence": 14000, "bank_account_code": "1000"}}'
    )
    mock_db.execute.side_effect = [
        _result(scalars=[]),  # resolve_entity_scope — no entities (legacy)
        _result(scalars=list(coa.values())),
        _result(scalar=coa["6600"]),
        _result(scalar=coa["1000"]),
        _result(scalar=coa["2030"]),
        _result(scalar=coa["2030"]),
        _result(scalar=None),
        _result(scalars=[coa["6600"], coa["1000"], coa["2030"]]),
    ]

    result = await orchestrate_message(mock_db, "Payroll: gross £3000, PAYE £400", llm=llm)

    assert result.status == "preview"
    assert result.skill_id == "payroll"
    assert result.total_pence == 300000
    assert [(l.code, l.debit_pence, l.credit_pence) for l in result.legs] == [
        ("6600", 300000, 0),
        ("1000", 0, 245000),
        ("2030", 0, 40000),
        ("2030", 0, 15000),
    ]
    assert result.formance_preview == [
        {"source": "1000", "destination": "6600", "amount": "2450.00", "asset": "GBP/2"},
        {"source": "2030", "destination": "6600", "amount": "400.00", "asset": "GBP/2"},
        {"source": "2030", "destination": "6600", "amount": "150.00", "asset": "GBP/2"},
    ]
    drafts = [o for o in mock_db.added if isinstance(o, Transaction)]
    assert drafts[0].effective_date == date(2026, 9, 30)


# ---------------------------------------------------------------------------
# orchestrate_message — error paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orchestrate_invalid_facts_structured_error(mock_db: AsyncMock) -> None:
    llm = await _llm_returning(
        '{"skill_id": "supplier-invoice", "facts": {'
        '"supplier_name": "Acme", "invoice_ref": "INV-1", "invoice_date": "2026-10-01", '
        '"lines": [{"description": "x", "net_pence": 1000, '
        '"vat_rate": "25%", "expense_account_code": "5000"}]}}'
    )

    result = await orchestrate_message(mock_db, "invoice", llm=llm)

    assert result.status == "error"
    assert result.transaction_id is None
    assert result.legs is None
    assert result.error["type"] == "invalid_facts"
    assert "vat_rate" in result.error["message"]
    mock_db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_orchestrate_unknown_skill_error(mock_db: AsyncMock) -> None:
    llm = await _llm_returning('{"skill_id": "magic-wand", "facts": {}}')

    result = await orchestrate_message(mock_db, "wave wand", llm=llm)

    assert result.status == "error"
    assert result.error["type"] == "unknown_skill"
    assert "magic-wand" in result.error["message"]
    mock_db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_orchestrate_llm_unreachable_structured_error(mock_db: AsyncMock) -> None:
    async def broken_llm(system_prompt: str, user_message: str) -> str:
        raise LLMError("LLM API error: connection refused")

    result = await orchestrate_message(mock_db, "hi", llm=broken_llm)

    assert result.status == "error"
    assert result.error["type"] == "llm_unreachable"
    assert "connection refused" in result.error["message"]
    mock_db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_orchestrate_llm_transport_raising_raw_exception(
    mock_db: AsyncMock,
) -> None:
    async def broken_llm(system_prompt: str, user_message: str) -> str:
        raise httpx.ConnectError("no route to host")

    result = await orchestrate_message(mock_db, "hi", llm=broken_llm)

    assert result.status == "error"
    assert result.error["type"] == "llm_error"
    assert "ConnectError" in result.error["message"]


@pytest.mark.asyncio
async def test_orchestrate_llm_bad_json_structured_error(mock_db: AsyncMock) -> None:
    llm = await _llm_returning("I think you want to record something, but I am unsure")

    result = await orchestrate_message(mock_db, "hi", llm=llm)

    assert result.status == "error"
    assert result.error["type"] == "invalid_response"
    assert "not valid JSON" in result.error["message"]
    mock_db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_orchestrate_clarification_error_preview_absent(
    mock_db: AsyncMock,
) -> None:
    llm = await _llm_returning(
        '{"skill_id": null, "clarification": "Which bank account should the money leave?"}'
    )

    result = await orchestrate_message(mock_db, "pay the supplier", llm=llm)

    assert result.status == "error"
    assert result.error["type"] == "clarification"
    assert "bank account" in result.error["message"]
    assert result.transaction_id is None
    assert result.legs is None
    mock_db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_orchestrate_constructor_error_structured(
    mock_db: AsyncMock, coa: dict[str, Account]
) -> None:
    # Expense account 9999 is not in the COA -> ConstructorError, not a crash.
    llm = await _llm_returning(
        '{"skill_id": "supplier-invoice", "facts": {'
        '"supplier_name": "Acme", "invoice_ref": "INV-2", "invoice_date": "2026-10-03", '
        '"lines": [{"description": "x", "net_pence": 1000, '
        '"vat_rate": "0%", "expense_account_code": "9999"}]}}'
    )
    mock_db.execute.side_effect = [
        _result(scalars=[]),  # resolve_entity_scope — no entities (legacy)
        _result(scalars=list(coa.values())),
    ]

    result = await orchestrate_message(mock_db, "invoice", llm=llm)

    assert result.status == "error"
    assert result.error["type"] == "constructor_error"
    assert "9999" in result.error["message"]
    assert not [o for o in mock_db.added if isinstance(o, Transaction)]


@pytest.mark.asyncio
async def test_orchestrate_no_skill_id_nor_clarification(
    mock_db: AsyncMock,
) -> None:
    llm = await _llm_returning('{"facts": {}}')

    result = await orchestrate_message(mock_db, "hi", llm=llm)

    assert result.status == "error"
    assert result.error["type"] == "invalid_response"


# ---------------------------------------------------------------------------
# approve_entry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approve_posts_mirrors_and_runs_parity(
    mock_db: AsyncMock, monkeypatch
) -> None:
    draft = _draft_transaction()
    mock_db.execute.side_effect = [
        _result(scalar=draft),  # post_transaction load
        _result(scalar=None),  # _generate_je_reference
        _result(scalars=[]),  # _post_to_formance account-code lookup
    ]
    mock_create = AsyncMock(return_value={"txid": 1})
    parity = ParityReport(ok=True, summary="Ledger parity OK: 1 transactions verified")
    monkeypatch.setenv("FORNANCE_LEDGER_URL", "http://formance:3068")

    with (
        patch(
            "src.services.transaction_service.formance_client.create_transaction",
            mock_create,
        ),
        patch(
            "src.services.orchestrator.verify_parity",
            AsyncMock(return_value=parity),
        ) as mock_parity,
        patch(
            "src.services.orchestrator.get_active_ledger",
            AsyncMock(return_value="main"),
        ),
    ):
        result = await approve_entry(mock_db, draft.id)

    assert result.transaction.status == "posted"
    assert result.transaction.reference == "JE-2026-0001"
    assert result.parity_ok is True
    assert result.parity_report is parity
    mock_create.assert_called_once()
    assert mock_create.call_args.kwargs["reference"] == "JE-2026-0001"
    mock_parity.assert_awaited_once()


@pytest.mark.asyncio
async def test_approve_parity_drift_flagged_but_result_returned(
    mock_db: AsyncMock, monkeypatch
) -> None:
    draft = _draft_transaction()
    mock_db.execute.side_effect = [
        _result(scalar=draft),
        _result(scalar=None),
        _result(scalars=[]),
    ]
    drift = ParityReport(
        ok=False,
        missing_in_formance=[
            {
                "kind": "missing_mirror",
                "transaction_id": str(draft.id),
                "reference": None,
                "expected": str(draft.id),
                "actual": None,
            }
        ],
        summary="Ledger parity FAILED: 1 missing",
    )
    monkeypatch.setenv("FORNANCE_LEDGER_URL", "http://formance:3068")
    mock_create = AsyncMock(return_value={"txid": 1})

    with (
        patch(
            "src.services.transaction_service.formance_client.create_transaction",
            mock_create,
        ),
        patch(
            "src.services.orchestrator.verify_parity",
            AsyncMock(return_value=drift),
        ),
        patch(
            "src.services.orchestrator.get_active_ledger",
            AsyncMock(return_value="main"),
        ),
    ):
        result = await approve_entry(mock_db, draft.id)

    # Posting already happened — parity flags drift, it does not roll back.
    assert result.transaction.status == "posted"
    assert result.parity_ok is False
    assert len(result.parity_report.missing_in_formance) == 1
    mock_create.assert_called_once()


@pytest.mark.asyncio
async def test_approve_non_draft_raises(
    mock_db: AsyncMock, monkeypatch
) -> None:
    posted = _draft_transaction()
    posted.status = "posted"
    mock_db.execute.return_value = _result(scalar=posted)
    monkeypatch.setenv("FORNANCE_LEDGER_URL", "http://formance:3068")

    with (
        patch(
            "src.services.transaction_service.formance_client.create_transaction",
            AsyncMock(),
        ),
        patch("src.services.orchestrator.verify_parity", AsyncMock()) as mock_parity,
        pytest.raises(TransactionNotDraftError),
    ):
        await approve_entry(mock_db, posted.id)

    mock_parity.assert_not_called()


@pytest.mark.asyncio
async def test_approve_missing_transaction_raises(mock_db: AsyncMock) -> None:
    mock_db.execute.return_value = _result(scalar=None)

    with pytest.raises(TransactionNotFoundError):
        await approve_entry(mock_db, uuid.uuid4())


# ---------------------------------------------------------------------------
# reject_entry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reject_discards_draft(mock_db: AsyncMock) -> None:
    draft = _draft_transaction()
    mock_db.execute.return_value = _result(scalar=draft)

    result = await reject_entry(mock_db, draft.id)

    assert result.transaction_id == draft.id
    assert result.status == "discarded"
    mock_db.delete.assert_called_once_with(draft)
    mock_db.commit.assert_called_once()


@pytest.mark.asyncio
async def test_reject_posted_raises_never_deleted(mock_db: AsyncMock) -> None:
    posted = _draft_transaction()
    posted.status = "posted"
    mock_db.execute.return_value = _result(scalar=posted)

    with pytest.raises(TransactionNotDraftError):
        await reject_entry(mock_db, posted.id)

    mock_db.delete.assert_not_called()
    mock_db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_reject_missing_transaction_raises(mock_db: AsyncMock) -> None:
    mock_db.execute.return_value = _result(scalar=None)

    with pytest.raises(TransactionNotFoundError):
        await reject_entry(mock_db, uuid.uuid4())

    mock_db.delete.assert_not_called()


# ---------------------------------------------------------------------------
# ConstructorError stays a single, catchable exception type
# ---------------------------------------------------------------------------


def test_constructor_error_is_still_an_exception() -> None:
    assert issubclass(ConstructorError, Exception)


def test_discard_draft_is_on_transaction_service() -> None:
    assert callable(TransactionService.discard_draft)
