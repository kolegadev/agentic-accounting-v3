# /Users/johnpellew/agentic-accounting-v3/api/tests/integration/test_orchestrator_e2e.py

"""Contract F6 end-to-end tests — orchestrator over a real async ORM.

Drives the REAL constructors, TransactionService and CoaService against a
real SQLAlchemy async session on an in-memory SQLite database (the mapped
models are PostgreSQL-native, so a hand-written SQLite schema is used with a
``gen_random_uuid`` shim — same pattern as ``test_transaction_orm_reversal.py``).
A fake LLM returns canned extraction JSON; a scripted fake Formance records
every ``create_transaction`` mirror and serves those records back to the
parity verifier.

Round-trips covered: supplier invoice (VAT) and customer receipt — preview
shows the correct legs, approve posts + mirrors and parity reports ok=True;
the reject path leaves no posted rows and parity stays ok; dropping a
recorded mirror makes parity flag the drift.
"""

from __future__ import annotations

import copy
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import event, select, text, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.config.database import Base
from src.models.account import Account
from src.models.entity import Entity
from src.models.transaction import Transaction
from src.services.orchestrator import (
    approve_entry,
    orchestrate_message,
    reject_entry,
)
from src.services.transaction_service import (
    TransactionNotFoundError,
    TransactionService,
)

GBP = "GBP/2"

_SQLITE_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS entities (
        id CHAR(36) PRIMARY KEY NOT NULL DEFAULT (gen_random_uuid()),
        name VARCHAR(255) NOT NULL UNIQUE,
        legal_type VARCHAR(30) NOT NULL DEFAULT 'limited_company',
        framework VARCHAR(20) NOT NULL DEFAULT 'uk-gaap',
        fiscal_year_start VARCHAR(5) NOT NULL DEFAULT '04-06',
        vat_registered BOOLEAN NOT NULL DEFAULT 0,
        vat_number VARCHAR(20),
        vat_scheme VARCHAR(10),
        currency VARCHAR(3) NOT NULL DEFAULT 'GBP',
        ledger_name VARCHAR(64) NOT NULL UNIQUE,
        setup_state VARCHAR(16) NOT NULL DEFAULT 'none',
        created_by CHAR(36) NOT NULL,
        is_active BOOLEAN NOT NULL DEFAULT 1,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS accounts (
        id CHAR(36) PRIMARY KEY NOT NULL DEFAULT (gen_random_uuid()),
        code VARCHAR(10) NOT NULL UNIQUE,
        name VARCHAR(255) NOT NULL,
        category VARCHAR(20) NOT NULL,
        type VARCHAR(30) NOT NULL,
        parent_id CHAR(36),
        entity_id CHAR(36),
        vat_rate VARCHAR(10),
        is_active BOOLEAN NOT NULL DEFAULT 1,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS transactions (
        id CHAR(36) PRIMARY KEY NOT NULL DEFAULT (gen_random_uuid()),
        reference VARCHAR(14) UNIQUE,
        description TEXT,
        contact_id CHAR(36),
        entity_id CHAR(36),
        total_amount INTEGER,
        currency VARCHAR(3) NOT NULL DEFAULT 'GBP',
        status VARCHAR(10) NOT NULL DEFAULT 'draft',
        effective_date DATE,
        idempotency_key CHAR(36) UNIQUE,
        recorded_at DATETIME,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS postings (
        id CHAR(36) PRIMARY KEY NOT NULL DEFAULT (gen_random_uuid()),
        transaction_id CHAR(36) NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
        account_id CHAR(36) NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
        debit_amount INTEGER NOT NULL DEFAULT 0,
        credit_amount INTEGER NOT NULL DEFAULT 0,
        description TEXT,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS vat_lines (
        id CHAR(36) PRIMARY KEY NOT NULL DEFAULT (gen_random_uuid()),
        posting_id CHAR(36) NOT NULL REFERENCES postings(id) ON DELETE CASCADE,
        vat_rate VARCHAR(10) NOT NULL,
        vat_amount INTEGER NOT NULL,
        net_amount INTEGER NOT NULL,
        vat_type VARCHAR(6) NOT NULL
    )
    """,
    # Contract F29 — contact auto-creation writes real contact rows.
    """
    CREATE TABLE IF NOT EXISTS contacts (
        id CHAR(36) PRIMARY KEY NOT NULL DEFAULT (gen_random_uuid()),
        type VARCHAR(10) NOT NULL,
        name VARCHAR(255) NOT NULL,
        company VARCHAR(255),
        email VARCHAR(255) UNIQUE,
        phone VARCHAR(50),
        address_line1 VARCHAR(255),
        address_line2 VARCHAR(255),
        city VARCHAR(100),
        postcode VARCHAR(10),
        country VARCHAR(2) NOT NULL DEFAULT 'GB',
        vat_number VARCHAR(20) UNIQUE,
        payment_terms VARCHAR(50) NOT NULL DEFAULT 'Net 30',
        default_gl_account_id CHAR(36),
        currency VARCHAR(3) NOT NULL DEFAULT 'GBP',
        status VARCHAR(10) NOT NULL DEFAULT 'active',
        total_invoiced INTEGER NOT NULL DEFAULT 0,
        total_paid INTEGER NOT NULL DEFAULT 0,
        total_owing INTEGER NOT NULL DEFAULT 0,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
]

COA_SEED = [
    ("1000", "Bank Current", "Asset", "Bank"),
    ("1100", "Trade Debtors", "Asset", "CurrentAsset"),
    ("2000", "Trade Creditors", "Liability", "CurrentLiability"),
    ("2030", "PAYE/NI Payable", "Liability", "CurrentLiability"),
    ("2210", "Output VAT", "Liability", "CurrentLiability"),
    ("2220", "Input VAT", "Liability", "CurrentLiability"),
    ("4000", "Sales Revenue", "Revenue", "Revenue"),
    ("5000", "Office Expenses", "Expense", "Expense"),
    ("5010", "Materials", "Expense", "DirectCost"),
]

SUPPLIER_RAW = (
    '{"skill_id": "supplier-invoice", "facts": {'
    '"supplier_name": "Acme Office Supplies", "invoice_ref": "INV-1001", '
    '"invoice_date": "2026-10-01", "due_date": "2026-10-31", "lines": ['
    '{"description": "Printer paper", "net_pence": 10000, '
    '"vat_rate": "20%", "expense_account_code": "5000"}]}}'
)

SUPPLIER_RAW_2 = (
    '{"skill_id": "supplier-invoice", "facts": {'
    '"supplier_name": "Beta Stationery", "invoice_ref": "INV-1002", '
    '"invoice_date": "2026-10-02", "lines": ['
    '{"description": "Folders", "net_pence": 5000, '
    '"vat_rate": "20%", "expense_account_code": "5010"}]}}'
)

RECEIPT_RAW = (
    '{"skill_id": "customer-receipt", "facts": {'
    '"customer_name": "Acme Corp", "receipt_date": "2026-10-05", '
    '"bank_account_code": "1000", "amount_pence": 15000, '
    '"invoice_refs": ["INV-001"], "allocated_pence": [15000]}}'
)

# Contract F29 — a sale to a customer that does not exist yet.
SALES_RAW = (
    '{"skill_id": "sales-invoice", "facts": {'
    '"customer_name": "Brand New Customer Ltd", "invoice_ref": "INV-9001", '
    '"invoice_date": "2026-10-07", "lines": ['
    '{"description": "Consulting", "net_pence": 20000, '
    '"vat_rate": "20%", "revenue_account_code": "4000"}]}}'
)


class FakeFormance:
    """Scripted Formance ledger: records mirrors, serves them back."""

    def __init__(self) -> None:
        self.transactions: list[dict] = []

    async def create_transaction(self, **kwargs) -> dict:
        txid = len(self.transactions) + 1
        self.transactions.append(
            {
                "txid": txid,
                "reference": kwargs.get("reference"),
                "timestamp": kwargs.get("timestamp"),
                "metadata": kwargs.get("metadata") or {},
                "postings": copy.deepcopy(kwargs.get("postings") or []),
            }
        )
        return {"txid": txid}

    async def list_transactions(self, ledger, page_size=None, after=None) -> dict:
        return {"cursor": {"hasMore": False}, "data": copy.deepcopy(self.transactions)}

    async def get_balances(self, ledger, address=None) -> dict:
        nets: dict[str, dict[str, Decimal]] = {}
        for tx in self.transactions:
            for posting in tx["postings"]:
                asset = posting["asset"]
                amount = Decimal(posting["amount"])
                src = nets.setdefault(posting["source"], {})
                src[asset] = src.get(asset, Decimal(0)) - amount
                dst = nets.setdefault(posting["destination"], {})
                dst[asset] = dst.get(asset, Decimal(0)) + amount
        return {
            "data": [
                {"address": addr, "balances": {a: str(v) for a, v in assets.items()}}
                for addr, assets in nets.items()
            ]
        }

    def mirror_for(self, tx_id: str) -> dict:
        return next(
            t for t in self.transactions
            if t["metadata"]["transaction_id"] == str(tx_id)
        )

    def drop_transaction(self, txid: int) -> None:
        self.transactions = [t for t in self.transactions if t["txid"] != txid]


def make_fake_llm(responses: dict[str, str]):
    """LLM transport returning the canned extraction whose key is in the message."""

    async def transport(system_prompt: str, user_message: str) -> str:
        lowered = user_message.lower()
        for key, raw in responses.items():
            if key.lower() in lowered:
                return raw
        return '{"skill_id": null, "clarification": "unrecognised message"}'

    return transport


@pytest.fixture
async def ctx(monkeypatch):
    """Real ORM + seeded COA + recorded Formance mirrors, all wired up."""
    monkeypatch.setenv("FORNANCE_LEDGER_URL", "http://formance:3068")
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)

    @event.listens_for(engine.sync_engine, "connect")
    def _register_gen_random_uuid(dbapi_conn, _record) -> None:
        dbapi_conn.create_function("gen_random_uuid", 0, lambda: uuid.uuid4().hex)

    async with engine.begin() as conn:
        try:
            await conn.run_sync(Base.metadata.create_all)
        except SQLAlchemyError:
            for ddl in _SQLITE_SCHEMA:
                await conn.execute(text(ddl))

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    db = session_factory()

    now = datetime.now(UTC)
    db.add_all(
        [
            Account(
                id=uuid.uuid4(), code=code, name=name,
                category=category, type=type_,
                is_active=True, created_at=now, updated_at=now,
            )
            for code, name, category, type_ in COA_SEED
        ]
    )
    await db.commit()

    fake = FakeFormance()
    parity_client = MagicMock()
    parity_client.list_transactions = AsyncMock(side_effect=fake.list_transactions)
    parity_client.get_balances = AsyncMock(side_effect=fake.get_balances)

    with (
        patch(
            "src.services.transaction_service.formance_client.create_transaction",
            AsyncMock(side_effect=fake.create_transaction),
        ),
        patch("src.services.ledger_parity.formance_client", parity_client),
        # Contract F29 — query mode reads the F8/F9 report engine.
        patch("src.services.formance_reports.formance_client", parity_client),
    ):
        yield {"db": db, "fake": fake, "session_factory": session_factory}

    await db.close()
    await engine.dispose()


async def _posted_count(db: AsyncSession) -> int:
    result = await db.execute(
        select(Transaction).where(Transaction.status != "draft")
    )
    return len(result.scalars().all())


# ---------------------------------------------------------------------------
# (a) supplier-invoice VAT round-trip
# ---------------------------------------------------------------------------


async def test_supplier_invoice_vat_roundtrip(ctx) -> None:
    db, fake = ctx["db"], ctx["fake"]
    llm = make_fake_llm({"supplier invoice INV-1001": SUPPLIER_RAW})

    preview = await orchestrate_message(
        db,
        "Received supplier invoice INV-1001 from Acme Office Supplies: "
        "£100 net plus VAT",
        llm=llm,
    )

    assert preview.status == "preview"
    assert preview.skill_id == "supplier-invoice"
    assert preview.total_pence == 12000
    assert [(l.code, l.debit_pence, l.credit_pence) for l in preview.legs] == [
        ("5000", 10000, 0),
        ("2220", 2000, 0),
        ("2000", 0, 12000),
    ]
    assert preview.legs[0].name == "Office Expenses"
    assert preview.legs[2].name == "Trade Creditors"
    assert preview.formance_preview == [
        {"source": "2000", "destination": "5000", "amount": "100.00", "asset": GBP},
        {"source": "2000", "destination": "2220", "amount": "20.00", "asset": GBP},
    ]

    # Nothing posted and nothing mirrored by the preview.
    assert await _posted_count(db) == 0
    assert fake.transactions == []

    post = await approve_entry(db, preview.transaction_id)

    assert post.transaction.status == "posted"
    assert post.transaction.reference == "JE-2026-0001"
    assert post.parity_ok is True
    assert post.parity_report.missing_in_formance == []
    assert post.parity_report.orphans_in_formance == []
    assert post.parity_report.leg_mismatches == []
    assert post.parity_report.balance_mismatches == []
    assert "OK" in post.parity_report.summary

    assert len(fake.transactions) == 1
    mirror = fake.transactions[0]
    assert mirror["reference"] == "JE-2026-0001"
    assert mirror["metadata"]["transaction_id"] == str(preview.transaction_id)
    assert mirror["postings"] == preview.formance_preview


# ---------------------------------------------------------------------------
# (b) customer-receipt round-trip
# ---------------------------------------------------------------------------


async def test_customer_receipt_roundtrip(ctx) -> None:
    db, fake = ctx["db"], ctx["fake"]
    llm = make_fake_llm({"customer": RECEIPT_RAW})

    preview = await orchestrate_message(
        db,
        "Customer Acme Corp paid £150 against invoice INV-001 into the "
        "current account",
        llm=llm,
    )
    assert preview.status == "preview", f"unexpected error: {preview.error!r}"
    assert preview.skill_id == "customer-receipt"
    assert preview.total_pence == 15000
    assert [(l.code, l.debit_pence, l.credit_pence) for l in preview.legs] == [
        ("1000", 15000, 0),
        ("1100", 0, 15000),
    ]
    assert preview.legs[0].name == "Bank Current"
    assert preview.legs[1].name == "Trade Debtors"
    assert preview.formance_preview == [
        {"source": "1100", "destination": "1000", "amount": "150.00", "asset": GBP}
    ]
    assert await _posted_count(db) == 0

    post = await approve_entry(db, preview.transaction_id)

    assert post.transaction.status == "posted"
    assert post.transaction.reference == "JE-2026-0001"
    assert post.parity_ok is True
    assert len(fake.transactions) == 1
    assert fake.transactions[0]["postings"] == preview.formance_preview


# ---------------------------------------------------------------------------
# (c) reject path — no posted rows, parity stays ok
# ---------------------------------------------------------------------------


async def test_reject_path_leaves_no_posted_rows_parity_ok(ctx) -> None:
    db, fake = ctx["db"], ctx["fake"]
    llm = make_fake_llm({"supplier invoice INV-1001": SUPPLIER_RAW})

    preview = await orchestrate_message(
        db, "supplier invoice INV-1001 for office supplies", llm=llm
    )
    assert preview.status == "preview"

    rejected = await reject_entry(db, preview.transaction_id)

    assert rejected.status == "discarded"
    assert rejected.transaction_id == preview.transaction_id
    assert fake.transactions == []

    rows = (await db.execute(select(Transaction))).scalars().all()
    assert rows == []
    assert await _posted_count(db) == 0

    # Parity over an empty ledger: ok=True.
    from src.services.ledger_parity import verify_parity

    report = await verify_parity(db)
    assert report.ok is True

    # The draft is gone — a second reject raises, deleting nothing.
    with pytest.raises(TransactionNotFoundError):
        await reject_entry(db, preview.transaction_id)


# ---------------------------------------------------------------------------
# (d) sabotage — a dropped mirror is flagged by the parity check
# ---------------------------------------------------------------------------


async def test_sabotage_drop_mirror_flags_parity_drift(ctx) -> None:
    db, fake = ctx["db"], ctx["fake"]
    llm = make_fake_llm(
        {
            "supplier invoice INV-1001": SUPPLIER_RAW,
            "customer": RECEIPT_RAW,
            "supplier invoice INV-1002": SUPPLIER_RAW_2,
        }
    )

    a = await orchestrate_message(db, "supplier invoice INV-1001", llm=llm)
    post_a = await approve_entry(db, a.transaction_id)
    assert post_a.parity_ok is True

    b = await orchestrate_message(db, "customer Acme Corp paid", llm=llm)
    post_b = await approve_entry(db, b.transaction_id)
    assert post_b.parity_ok is True

    # Sabotage: drop the mirror recorded for transaction A.
    fake.drop_transaction(fake.mirror_for(str(a.transaction_id))["txid"])
    assert len(fake.transactions) == 1

    # The next approve runs verify_parity and flags the drift — but the
    # posting itself already happened and is NOT rolled back.
    c = await orchestrate_message(db, "supplier invoice INV-1002 folders", llm=llm)
    post_c = await approve_entry(db, c.transaction_id)

    assert post_c.transaction.status == "posted"
    assert post_c.parity_ok is False
    missing = post_c.parity_report.missing_in_formance
    assert [m["transaction_id"] for m in missing] == [str(a.transaction_id)]
    assert missing[0]["kind"] == "missing_mirror"
    assert missing[0]["reference"] == "JE-2026-0001"
    assert post_c.parity_report.orphans_in_formance == []
    assert len(fake.transactions) == 2  # B and C mirrors remain


# ---------------------------------------------------------------------------
# (e) clarification — structured error, no rows created
# ---------------------------------------------------------------------------


async def test_clarification_creates_no_rows(ctx) -> None:
    db, fake = ctx["db"], ctx["fake"]
    llm = make_fake_llm({})  # nothing matches -> clarification

    result = await orchestrate_message(db, "do the thing", llm=llm)

    assert result.status == "error"
    assert result.error["type"] == "clarification"
    rows = (await db.execute(select(Transaction))).scalars().all()
    assert rows == []
    assert fake.transactions == []


# ---------------------------------------------------------------------------
# (f) F19 — entity-scoped orchestration round-trip
# ---------------------------------------------------------------------------


async def test_entity_scoped_orchestration_roundtrip(ctx) -> None:
    """An entity-scoped message drafts into the entity's scope (stamped row,
    scoped COA); approve mirrors to the ENTITY's own ledger via the tx row
    (never a global guess) and the scoped parity check passes."""
    db, fake = ctx["db"], ctx["fake"]
    entity = Entity(
        id=uuid.uuid4(),
        name="Acme Scoped Ltd",
        legal_type="limited_company",
        framework="uk-gaap",
        fiscal_year_start="04-06",
        vat_registered=False,
        currency="GBP",
        ledger_name="entity-scoped",
        setup_state="complete",
        created_by=uuid.uuid4(),
        is_active=True,
    )
    db.add(entity)
    await db.commit()
    # Stamp the seeded COA with the entity (an entity's COA is its own rows).
    await db.execute(update(Account).values(entity_id=entity.id))
    await db.commit()

    from src.services.formance_client import formance_client

    ledgers: list[str] = []

    async def recording_create(**kwargs) -> dict:
        ledgers.append(kwargs.get("ledger"))
        return await fake.create_transaction(**kwargs)

    llm = make_fake_llm({"supplier invoice INV-1001": SUPPLIER_RAW})
    with patch.object(
        formance_client,
        "create_transaction",
        AsyncMock(side_effect=recording_create),
    ):
        preview = await orchestrate_message(
            db,
            "supplier invoice INV-1001 from Acme Office Supplies",
            llm=llm,
            entity_id=entity.id,
        )
        assert preview.status == "preview", preview.error

        draft_row = await TransactionService.get_transaction(
            db, preview.transaction_id
        )
        assert draft_row is not None
        assert draft_row.entity_id == entity.id

        post = await approve_entry(db, preview.transaction_id)

    assert post.transaction.status == "posted"
    assert post.parity_ok is True, post.parity_report.summary
    assert ledgers == ["entity-scoped"]
    assert fake.transactions[-1]["reference"] == "JE-2026-0001"


# ---------------------------------------------------------------------------
# (g) F29 — a sale to a NEW customer auto-creates the contact
# ---------------------------------------------------------------------------


async def test_sales_invoice_to_new_customer_creates_contact(ctx) -> None:
    """Contract F29: invoicing a customer the books have never seen creates
    the contact (visible via ContactService.list_contacts), stamps it on
    the draft, and the approved entry still posts + balances."""
    from src.services.contact_service import ContactService

    db, fake = ctx["db"], ctx["fake"]

    before, total_before = await ContactService.list_contacts(db)
    assert before == [] and total_before == 0

    llm = make_fake_llm({"invoice INV-9001": SALES_RAW})
    preview = await orchestrate_message(
        db,
        "Send invoice INV-9001 to Brand New Customer Ltd for £200 plus VAT",
        llm=llm,
    )

    assert preview.status == "preview", preview.error
    assert preview.skill_id == "sales-invoice"
    assert preview.total_pence == 24000
    assert preview.contact is not None
    assert preview.contact["name"] == "Brand New Customer Ltd"
    assert preview.contact["created"] is True

    # The contact is REAL (a row) and listed by the contacts service.
    contacts, total = await ContactService.list_contacts(db)
    assert total == 1
    assert [c.name for c in contacts] == ["Brand New Customer Ltd"]
    assert str(contacts[0].id) == preview.contact["id"]

    # ... and the DRAFT carries it.
    draft = await TransactionService.get_transaction(db, preview.transaction_id)
    assert draft is not None
    assert str(draft.contact_id) == preview.contact["id"]

    # Idempotent: the same customer on a second invoice is NOT re-created.
    second_llm = make_fake_llm({"invoice INV-9002": SALES_RAW.replace("INV-9001", "INV-9002")})
    second = await orchestrate_message(
        db, "Send invoice INV-9002 to Brand New Customer Ltd", llm=second_llm
    )
    assert second.status == "preview", second.error
    assert second.contact["created"] is False
    assert second.contact["id"] == preview.contact["id"]
    contacts_after, total_after = await ContactService.list_contacts(db)
    assert len(contacts_after) == 1
    assert total_after == 1

    # Approve -> posted, mirrored, parity ok (postings untouched by F29).
    post = await approve_entry(db, preview.transaction_id)
    assert post.transaction.status == "posted"
    assert post.parity_ok is True, post.parity_report.summary
    assert fake.transactions[-1]["metadata"]["transaction_id"] == str(
        preview.transaction_id
    )
    assert fake.transactions[-1]["postings"] == preview.formance_preview


# ---------------------------------------------------------------------------
# (h) F29 — query mode answers with the REAL ledger data
# ---------------------------------------------------------------------------


async def test_query_trial_balance_and_account_match_the_real_ledger(ctx) -> None:
    """A QUESTION (not a transaction) is classified as the query skill and
    answered deterministically: the trial balance totals equal the posted
    entry exactly, and so does the single-account balance.  Nothing is
    drafted, nothing is posted."""
    from src.models.transaction import Transaction as TxRow

    db, fake = ctx["db"], ctx["fake"]

    # Seed ONE real posted entry: £100 net + £20 VAT supplier invoice.
    llm = make_fake_llm({"supplier invoice INV-1001": SUPPLIER_RAW})
    preview = await orchestrate_message(db, "supplier invoice INV-1001", llm=llm)
    assert preview.status == "preview", preview.error
    post = await approve_entry(db, preview.transaction_id)
    assert post.parity_ok is True

    query_llm = make_fake_llm(
        {
            "trial balance": (
                '{"skill_id": "query", "facts": {"query_type": "trial_balance"}}'
            ),
            "balance on 5000": (
                '{"skill_id": "query", "facts": {"query_type": "account_balance", '
                '"account_code": "5000"}}'
            ),
        }
    )

    tb = await orchestrate_message(db, "what's my trial balance?", llm=query_llm)

    assert tb.status == "answer", tb.error
    assert tb.query_type == "trial_balance"
    assert tb.transaction_id is None  # a question never drafts
    assert tb.data["total_debits"] == 12000
    assert tb.data["total_credits"] == 12000
    by_code = {row["code"]: row for row in tb.data["trial_balance"]}
    assert by_code["5000"]["debit"] == 10000
    assert by_code["2220"]["debit"] == 2000
    assert by_code["2000"]["credit"] == 12000
    assert all(
        isinstance(row["debit"], int) and isinstance(row["credit"], int)
        for row in tb.data["trial_balance"]
    )

    one = await orchestrate_message(db, "what's the balance on 5000?", llm=query_llm)

    assert one.status == "answer", one.error
    assert one.query_type == "account_balance"
    assert one.data["account"]["code"] == "5000"
    assert one.data["account"]["debit"] == 10000

    # The query created no extra rows.
    rows = (await db.execute(select(TxRow))).scalars().all()
    assert len(rows) == 1
    assert fake.transactions and len(fake.transactions) == 1


async def test_query_unknown_type_is_a_structured_error(ctx) -> None:
    """An unsupported query_type degrades to the structured invalid_query
    error — no data, no draft."""
    from src.models.transaction import Transaction as TxRow

    db = ctx["db"]
    llm = make_fake_llm(
        {"report": '{"skill_id": "query", "facts": {"query_type": "stock_price"}}'}
    )

    result = await orchestrate_message(db, "give me a report on stock price", llm=llm)

    assert result.status == "error"
    assert result.error["type"] == "invalid_query"
    assert result.data is None
    rows = (await db.execute(select(TxRow))).scalars().all()
    assert rows == []
