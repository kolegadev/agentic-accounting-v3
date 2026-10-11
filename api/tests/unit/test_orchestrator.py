# /Users/johnpellew/agentic-accounting-v3/api/tests/unit/test_orchestrator.py

"""Contract F6 unit tests — orchestrator pipeline with mocked DB + LLM + Formance.

The LLM transport is an injected async callable; the DB is an AsyncMock
satisfying the CoaService/TransactionService query patterns; the Formance
mirror call and the parity verifier are patched at their module seams.
Nothing here touches a real LLM, database, or ledger.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.constructors import ConstructorError
from src.models.account import Account
from src.models.contact import Contact
from src.models.transaction import Posting, Transaction
from src.services.contact_service import ContactService
from src.services.ledger_parity import ParityReport
from src.services.orchestrator import (
    EntityScopeError,
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
from src.validators.contact import ContactResponse

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


def _contact_orm(name: str) -> Contact:
    """A fully-populated Contact ORM row (model_validate needs every field)."""
    return Contact(
        id=uuid.uuid4(),
        type="supplier",
        name=name,
        company=None,
        email=None,
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


def _contact_response(name: str) -> ContactResponse:
    return ContactResponse.model_validate(_contact_orm(name))


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


def test_build_system_prompt_lists_all_skills_with_schemas() -> None:
    prompt = build_system_prompt([{"role": "user", "content": "hi"}])

    for skill_id in (
        "bank-transfer",
        "card-purchase",
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


def test_build_system_prompt_grounds_in_todays_date() -> None:
    """Contract F23: rule 3 carries the REAL current date so the LLM never
    dates drafts 2024-01-01 (which pushed posted transactions outside the
    report period)."""
    prompt = build_system_prompt()
    match = re.search(r"Today is (20\d\d-\d\d-\d\d)", prompt)
    assert match is not None, f"no 'Today is <date>' in prompt: {prompt[-400:]}"
    assert match.group(1) == datetime.now(UTC).date().isoformat()


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
    supplier_contact = _contact_response("Acme Office Supplies")

    with patch.object(
        ContactService,
        "find_or_create",
        AsyncMock(return_value=(supplier_contact, True)),
    ) as mock_find_or_create:
        result = await orchestrate_message(
            mock_db, "Supplier invoice INV-1001 for £120", llm=llm
        )

    mock_find_or_create.assert_awaited_once()
    assert mock_find_or_create.call_args.args[1] == "Acme Office Supplies"
    assert mock_find_or_create.call_args.kwargs["type"] == "supplier"

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
    # Contract F29 — the supplier contact was resolved/created + stamped.
    assert result.contact is not None
    assert result.contact["name"] == "Acme Office Supplies"
    assert result.contact["created"] is True
    assert drafts[0].contact_id == uuid.UUID(result.contact["id"])
    mock_db.commit.assert_called_once()


@pytest.mark.asyncio
async def test_orchestrate_card_purchase_preview_with_supplier_contact(
    mock_db: AsyncMock, coa: dict[str, Account]
) -> None:
    """F36b: a card purchase previews Dr expense / Dr 2220 Input VAT /
    Cr bank AND auto-creates the supplier contact from ``supplier_name``."""
    llm = await _llm_returning(
        '{"skill_id": "card-purchase", "facts": {'
        '"purchase_date": "2026-10-04", "bank_account_code": "1000", '
        '"account_code": "5000", "net_pence": 10000, "vat_rate": "20%", '
        '"description": "Printer paper", '
        '"supplier_name": "Apple Store UK"}}'
    )
    mock_db.execute.side_effect = [
        _result(scalars=[]),  # resolve_entity_scope — no entities (legacy)
        _result(scalars=list(coa.values())),  # list_accounts
        _result(scalar=coa["5000"]),  # Dr expense/asset (net)
        _result(scalar=coa["2220"]),  # Dr 2220 Input VAT
        _result(scalar=coa["1000"]),  # Cr bank (gross)
        _result(scalar=None),  # idempotency check
        _result(scalars=[coa["5000"], coa["2220"], coa["1000"]]),
    ]
    supplier_contact = _contact_response("Apple Store UK")

    with patch.object(
        ContactService,
        "find_or_create",
        AsyncMock(return_value=(supplier_contact, True)),
    ) as mock_find_or_create:
        result = await orchestrate_message(
            mock_db,
            "I paid £100 plus VAT for printer paper with the company card",
            llm=llm,
        )

    assert result.status == "preview", result.error
    assert result.skill_id == "card-purchase"
    assert result.transaction_id is not None
    assert result.total_pence == 12000
    assert [(l.code, l.debit_pence, l.credit_pence) for l in result.legs] == [
        ("5000", 10000, 0),
        ("2220", 2000, 0),
        ("1000", 0, 12000),
    ]
    assert result.legs[0].name == "Office Expenses"
    assert result.legs[1].name == "Input VAT"
    assert result.legs[2].name == "Bank Current"
    assert result.formance_preview == [
        {"source": "1000", "destination": "5000", "amount": "100.00", "asset": "GBP/2"},
        {"source": "1000", "destination": "2220", "amount": "20.00", "asset": "GBP/2"},
    ]
    # Draft only — nothing posted, no mirroring.
    drafts = [o for o in mock_db.added if isinstance(o, Transaction)]
    assert len(drafts) == 1
    assert drafts[0].status == "draft"
    assert drafts[0].description.startswith("I paid £100")
    # Contract F29 — the card-purchase supplier contact was created + stamped.
    mock_find_or_create.assert_awaited_once()
    assert mock_find_or_create.call_args.args[1] == "Apple Store UK"
    assert mock_find_or_create.call_args.kwargs == {
        "email": None,
        "vat_number": None,
        "type": "supplier",
    }
    assert result.contact == {
        "id": str(supplier_contact.id),
        "name": "Apple Store UK",
        "created": True,
    }
    assert drafts[0].contact_id == supplier_contact.id
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
    # Contract F29 — no counterparty facts -> no contact work at all.
    assert result.contact is None
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
# orchestrate_message — contact auto-creation (Contract F29)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orchestrate_customer_receipt_creates_customer_contact(
    mock_db: AsyncMock, coa: dict[str, Account]
) -> None:
    """customer-receipt derives the counterparty from ``customer_name``,
    creates the contact and stamps it on the draft."""
    llm = await _llm_returning(
        '{"skill_id": "customer-receipt", "facts": {'
        '"customer_name": "Acme Corp", "receipt_date": "2026-10-05", '
        '"bank_account_code": "1000", "amount_pence": 15000, '
        '"invoice_refs": ["INV-001"], "allocated_pence": [15000]}}'
    )
    mock_db.execute.side_effect = [
        _result(scalars=[]),  # resolve_entity_scope — no entities (legacy)
        _result(scalars=list(coa.values())),  # list_accounts
        _result(scalar=coa["1000"]),  # Dr bank
        _result(scalar=coa["1100"]),  # Cr trade debtors
        _result(scalar=None),  # idempotency check
        _result(scalars=[coa["1000"], coa["1100"]]),  # validate accounts
    ]
    contact = _contact_response("Acme Corp")

    with patch.object(
        ContactService, "find_or_create", AsyncMock(return_value=(contact, True))
    ) as mock_find:
        result = await orchestrate_message(
            mock_db, "Customer Acme Corp paid £150 against INV-001", llm=llm
        )

    assert result.status == "preview"
    assert result.skill_id == "customer-receipt"
    mock_find.assert_awaited_once()
    assert mock_find.call_args.args[1] == "Acme Corp"
    assert mock_find.call_args.kwargs == {
        "email": None,
        "vat_number": None,
        "type": "customer",
    }
    assert result.contact == {
        "id": str(contact.id),
        "name": "Acme Corp",
        "created": True,
    }
    assert [(l.code, l.debit_pence, l.credit_pence) for l in result.legs] == [
        ("1000", 15000, 0),
        ("1100", 0, 15000),
    ]
    drafts = [o for o in mock_db.added if isinstance(o, Transaction)]
    assert drafts[0].contact_id == contact.id


@pytest.mark.asyncio
async def test_orchestrate_sales_invoice_reuses_existing_customer_contact(
    mock_db: AsyncMock, coa: dict[str, Account]
) -> None:
    """The REAL ``ContactService.find_or_create`` runs here: a case-
    insensitive name match reuses the existing contact (created=False) and
    the draft still carries its contact_id."""
    existing = _contact_orm("Acme Corp")
    llm = await _llm_returning(
        '{"skill_id": "sales-invoice", "facts": {'
        '"customer_name": "acme corp", "invoice_ref": "INV-2001", '
        '"invoice_date": "2026-10-01", "lines": [{"description": "Consulting", '
        '"net_pence": 10000, "vat_rate": "20%", "revenue_account_code": "4000"}]}}'
    )
    mock_db.execute.side_effect = [
        _result(scalars=[]),  # resolve_entity_scope
        _result(scalars=list(coa.values())),  # list_accounts
        _result(scalar=coa["1100"]),  # Dr trade debtors
        _result(scalar=coa["4000"]),  # Cr sales revenue
        _result(scalar=coa["2210"]),  # Cr output VAT
        _result(scalar=existing),  # find_or_create — name match
        _result(scalar=None),  # idempotency check
        _result(scalars=[coa["1100"], coa["4000"], coa["2210"]]),
    ]

    result = await orchestrate_message(
        mock_db, "Invoice Acme Corp £120 for consulting", llm=llm
    )

    assert result.status == "preview", result.error
    assert result.skill_id == "sales-invoice"
    assert result.contact == {
        "id": str(existing.id),
        "name": "Acme Corp",
        "created": False,
    }
    drafts = [o for o in mock_db.added if isinstance(o, Transaction)]
    assert len(drafts) == 1
    assert drafts[0].contact_id == existing.id


@pytest.mark.asyncio
async def test_orchestrate_supplier_payment_uses_supplier_name(
    mock_db: AsyncMock, coa: dict[str, Account]
) -> None:
    llm = await _llm_returning(
        '{"skill_id": "supplier-payment", "facts": {'
        '"supplier_name": "Beta Stationery", "payment_date": "2026-10-06", '
        '"bank_account_code": "1000", "amount_pence": 5000, '
        '"invoice_refs": ["INV-1002"], "allocated_pence": [5000]}}'
    )
    mock_db.execute.side_effect = [
        _result(scalars=[]),  # resolve_entity_scope
        _result(scalars=list(coa.values())),  # list_accounts
        _result(scalar=coa["2000"]),  # Dr trade creditors
        _result(scalar=coa["1000"]),  # Cr bank
        _result(scalar=None),  # idempotency check
        _result(scalars=[coa["2000"], coa["1000"]]),  # validate accounts
    ]
    contact = _contact_response("Beta Stationery")

    with patch.object(
        ContactService, "find_or_create", AsyncMock(return_value=(contact, True))
    ) as mock_find:
        result = await orchestrate_message(
            mock_db, "Pay Beta Stationery £50 from the bank", llm=llm
        )

    assert result.status == "preview"
    assert result.skill_id == "supplier-payment"
    mock_find.assert_awaited_once()
    assert mock_find.call_args.args[1] == "Beta Stationery"
    assert result.contact is not None
    assert result.contact["created"] is True
    drafts = [o for o in mock_db.added if isinstance(o, Transaction)]
    assert drafts[0].contact_id == contact.id


@pytest.mark.asyncio
async def test_orchestrate_contact_failure_degrades_to_draft_without_contact(
    mock_db: AsyncMock, coa: dict[str, Account], caplog
) -> None:
    """A contact-service failure NEVER blocks the draft: the entry is still
    created (without contact_id) and the drop is logged as a warning."""
    llm = await _llm_returning(
        '{"skill_id": "supplier-invoice", "facts": {'
        '"supplier_name": "Gamma Supplies", "invoice_ref": "INV-1003", '
        '"invoice_date": "2026-10-03", "lines": [{"description": "Paper", '
        '"net_pence": 10000, "vat_rate": "20%", "expense_account_code": "5000"}]}}'
    )
    mock_db.execute.side_effect = [
        _result(scalars=[]),  # resolve_entity_scope
        _result(scalars=list(coa.values())),  # list_accounts
        _result(scalar=coa["5000"]),
        _result(scalar=coa["2220"]),
        _result(scalar=coa["2000"]),
        _result(scalar=None),  # idempotency check
        _result(scalars=[coa["5000"], coa["2220"], coa["2000"]]),
    ]

    with (
        patch.object(
            ContactService,
            "find_or_create",
            AsyncMock(side_effect=RuntimeError("contacts table unavailable")),
        ),
        caplog.at_level(logging.WARNING, logger="src.services.orchestrator"),
    ):
        result = await orchestrate_message(
            mock_db, "Supplier invoice INV-1003 from Gamma Supplies", llm=llm
        )

    assert result.status == "preview"
    assert result.contact is None
    drafts = [o for o in mock_db.added if isinstance(o, Transaction)]
    assert len(drafts) == 1
    assert drafts[0].contact_id is None
    assert result.total_pence == 12000
    mock_db.rollback.assert_awaited()  # the aborted statement is cleared
    assert any(
        "Contact auto-creation failed" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_orchestrate_party_less_skill_never_touches_contacts(
    mock_db: AsyncMock, coa: dict[str, Account]
) -> None:
    """Skills without a counterparty fact (bank-transfer, payroll, …) do no
    contact work at all."""
    llm = await _llm_returning(
        '{"skill_id": "bank-transfer", "facts": {'
        '"source_account_code": "1000", "destination_account_code": "1001", '
        '"amount_pence": 50000, "transfer_date": "2026-10-02"}}'
    )
    mock_db.execute.side_effect = [
        _result(scalars=[]),  # resolve_entity_scope
        _result(scalars=list(coa.values())),  # list_accounts
        _result(scalar=coa["1001"]),
        _result(scalar=coa["1000"]),
        _result(scalar=None),  # idempotency check
        _result(scalars=[coa["1001"], coa["1000"]]),
    ]

    with patch.object(ContactService, "find_or_create", AsyncMock()) as mock_find:
        result = await orchestrate_message(
            mock_db, "Move £500 from 1000 to 1001", llm=llm
        )

    assert result.status == "preview"
    assert result.contact is None
    mock_find.assert_not_awaited()


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
    mock_db.execute.side_effect = [
        _result(scalars=[]),  # resolve_entity_scope — no entities (legacy)
        _result(scalars=[]),  # list_accounts — empty COA (LLM-error paths)
    ]
    result = await orchestrate_message(mock_db, "invoice", llm=llm)

    assert result.status == "error"
    assert result.transaction_id is None
    assert result.legs is None
    assert result.error["type"] == "invalid_facts"
    assert "vat_rate" in result.error["message"]
    assert mock_db.execute.await_count >= 2  # scope + COA resolved before the LLM


@pytest.mark.asyncio
async def test_orchestrate_unknown_skill_error(mock_db: AsyncMock) -> None:
    llm = await _llm_returning('{"skill_id": "magic-wand", "facts": {}}')
    mock_db.execute.side_effect = [
        _result(scalars=[]),  # resolve_entity_scope — no entities (legacy)
        _result(scalars=[]),  # list_accounts — empty COA (LLM-error paths)
    ]
    result = await orchestrate_message(mock_db, "wave wand", llm=llm)

    assert result.status == "error"
    assert result.error["type"] == "unknown_skill"
    assert "magic-wand" in result.error["message"]
    assert mock_db.execute.await_count >= 2


@pytest.mark.asyncio
async def test_orchestrate_llm_unreachable_structured_error(mock_db: AsyncMock) -> None:
    async def broken_llm(system_prompt: str, user_message: str) -> str:
        raise LLMError("LLM API error: connection refused")

    mock_db.execute.side_effect = [
        _result(scalars=[]),  # resolve_entity_scope — no entities (legacy)
        _result(scalars=[]),  # list_accounts — empty COA (LLM-error paths)
    ]
    result = await orchestrate_message(mock_db, "hi", llm=broken_llm)

    assert result.status == "error"
    assert result.error["type"] == "llm_unreachable"
    assert "connection refused" in result.error["message"]
    assert mock_db.execute.await_count >= 2


@pytest.mark.asyncio
async def test_orchestrate_llm_transport_raising_raw_exception(
    mock_db: AsyncMock,
) -> None:
    async def broken_llm(system_prompt: str, user_message: str) -> str:
        raise httpx.ConnectError("no route to host")

    mock_db.execute.side_effect = [
        _result(scalars=[]),  # resolve_entity_scope — no entities (legacy)
        _result(scalars=[]),  # list_accounts — empty COA (LLM-error paths)
    ]
    result = await orchestrate_message(mock_db, "hi", llm=broken_llm)

    assert result.status == "error"
    assert result.error["type"] == "llm_error"
    assert "ConnectError" in result.error["message"]


@pytest.mark.asyncio
async def test_orchestrate_llm_bad_json_structured_error(mock_db: AsyncMock) -> None:
    llm = await _llm_returning("I think you want to record something, but I am unsure")
    mock_db.execute.side_effect = [
        _result(scalars=[]),  # resolve_entity_scope — no entities (legacy)
        _result(scalars=[]),  # list_accounts — empty COA (LLM-error paths)
    ]
    result = await orchestrate_message(mock_db, "hi", llm=llm)

    assert result.status == "error"
    assert result.error["type"] == "invalid_response"
    assert "not valid JSON" in result.error["message"]
    assert mock_db.execute.await_count >= 2


@pytest.mark.asyncio
async def test_orchestrate_prose_then_json_retries_and_succeeds(
    mock_db: AsyncMock, coa: dict[str, Account]
) -> None:
    """deepseek-flash prose flake: the first classify returns prose, the
    one-bounded-retry classifies correctly on the second call."""
    calls = 0

    async def flaky_llm(system_prompt: str, message: str) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            return "Sure, I can help record that for you!"
        return (
            '{"skill_id": "bank-transfer", "facts": {'
            '"transfer_date": "2026-10-05", "source_account_code": "1000", '
            '"destination_account_code": "1001", "amount_pence": 50000}}'
        )

    mock_db.execute.side_effect = [
        _result(scalars=[]),  # resolve_entity_scope — no entities (legacy)
        _result(scalars=list(coa.values())),  # list_accounts
        _result(scalar=coa["1000"]),  # Dr bank
        _result(scalar=coa["1001"]),  # Cr bank
        _result(scalar=None),  # idempotency check
        _result(scalars=[coa["1000"], coa["1001"]]),  # validate accounts
    ]
    result = await orchestrate_message(
        mock_db, "transfer 500 between accounts", llm=flaky_llm
    )

    assert result.status == "preview"
    assert result.skill_id == "bank-transfer"
    assert calls == 2
    assert [(l.code, l.debit_pence, l.credit_pence) for l in result.legs] == [
        ("1001", 50000, 0),
        ("1000", 0, 50000),
    ]


@pytest.mark.asyncio
async def test_orchestrate_clarification_error_preview_absent(
    mock_db: AsyncMock,
) -> None:
    llm = await _llm_returning(
        '{"skill_id": null, "clarification": "Which bank account should the money leave?"}'
    )
    mock_db.execute.side_effect = [
        _result(scalars=[]),  # resolve_entity_scope — no entities (legacy)
        _result(scalars=[]),  # list_accounts — empty COA (LLM-error paths)
    ]
    result = await orchestrate_message(mock_db, "pay the supplier", llm=llm)

    assert result.status == "error"
    assert result.error["type"] == "clarification"
    assert "bank account" in result.error["message"]
    assert result.transaction_id is None
    assert result.legs is None
    assert mock_db.execute.await_count >= 2  # scope + COA resolved before the LLM


@pytest.mark.asyncio
async def test_orchestrate_no_skill_id_nor_clarification(
    mock_db: AsyncMock,
) -> None:
    llm = await _llm_returning("{}")
    mock_db.execute.side_effect = [
        _result(scalars=[]),  # resolve_entity_scope — no entities (legacy)
        _result(scalars=[]),  # list_accounts — empty COA (LLM-error paths)
    ]
    result = await orchestrate_message(mock_db, "hi", llm=llm)

    assert result.status == "error"
    assert result.error["type"] == "invalid_response"
    assert "neither a skill_id nor a clarification" in result.error["message"]


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
# reject_entry — entity ownership (Contract F20)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reject_entry_matching_entity_scope_discards(
    mock_db: AsyncMock,
) -> None:
    """An entity-scoped reject discards a draft stamped with the SAME
    entity — ownership passes, the discard proceeds."""
    entity_id = uuid.uuid4()
    draft = _draft_transaction()
    draft.entity_id = entity_id
    mock_db.execute.return_value = _result(scalar=draft)

    result = await reject_entry(mock_db, draft.id, entity_id=entity_id)

    assert result.transaction_id == draft.id
    assert result.status == "discarded"
    mock_db.delete.assert_called_once_with(draft)
    mock_db.commit.assert_called_once()


@pytest.mark.asyncio
async def test_reject_entry_cross_entity_refused(mock_db: AsyncMock) -> None:
    """A draft stamped with a DIFFERENT entity is refused with
    EntityScopeError (404) — never a cross-entity discard."""
    scope_a = uuid.uuid4()
    scope_b = uuid.uuid4()
    draft = _draft_transaction()
    draft.entity_id = scope_b
    mock_db.execute.return_value = _result(scalar=draft)

    with pytest.raises(EntityScopeError) as excinfo:
        await reject_entry(mock_db, draft.id, entity_id=scope_a)

    assert excinfo.value.status_code == 404
    assert excinfo.value.error_type == "entity_mismatch"
    assert str(scope_b) in excinfo.value.message
    mock_db.delete.assert_not_called()
    mock_db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_reject_entry_null_scoped_draft_refused_from_entity(
    mock_db: AsyncMock,
) -> None:
    """A NULL-scoped (legacy) draft is invisible within an entity scope —
    an entity-scoped reject refuses it (404) instead of discarding."""
    scope_a = uuid.uuid4()
    draft = _draft_transaction()
    draft.entity_id = None
    mock_db.execute.return_value = _result(scalar=draft)

    with pytest.raises(EntityScopeError) as excinfo:
        await reject_entry(mock_db, draft.id, entity_id=scope_a)

    assert excinfo.value.status_code == 404
    mock_db.delete.assert_not_called()


@pytest.mark.asyncio
async def test_reject_entry_null_scope_legacy_unchanged(
    mock_db: AsyncMock,
) -> None:
    """entity_id=None keeps the legacy discard path — no ownership query."""
    draft = _draft_transaction()
    draft.entity_id = uuid.uuid4()  # even a stamped draft discards unscoped
    mock_db.execute.return_value = _result(scalar=draft)

    result = await reject_entry(mock_db, draft.id)

    assert result.status == "discarded"
    assert mock_db.execute.call_count == 1  # only discard_draft's load
    mock_db.delete.assert_called_once_with(draft)


# ---------------------------------------------------------------------------
# ConstructorError stays a single, catchable exception type
# ---------------------------------------------------------------------------


def test_constructor_error_is_still_an_exception() -> None:
    assert issubclass(ConstructorError, Exception)


def test_discard_draft_is_on_transaction_service() -> None:
    assert callable(TransactionService.discard_draft)


def test_build_system_prompt_includes_coa_when_provided() -> None:
    """The prompt must ground account codes in the REAL chart of accounts."""
    coa = [
        {"code": "1000", "name": "Bank Current", "category": "Asset", "type": "Bank"},
        {"code": "5000", "name": "Office Equipment", "category": "Expense", "type": "Expense"},
    ]
    prompt = build_system_prompt(coa=coa)
    assert "## Chart of accounts" in prompt
    assert "1000 | Bank Current | Asset (Bank)" in prompt
    assert "5000 | Office Equipment | Expense (Expense)" in prompt
    assert "NEVER invent a" in prompt

    without = build_system_prompt()
    assert "## Chart of accounts" not in without
