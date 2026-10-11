"""Contract F9 end-to-end — the mini scenario through the F6 orchestrator.

Seeds a MINI business scenario through the REAL F6 orchestrator + F4
constructors (capital injection via opening-balances, a sales invoice with
VAT, a supplier invoice with VAT, a depreciation journal) plus a loan
drawdown through TransactionService, against a real async ORM on in-memory
SQLite (the ``test_transaction_orm_reversal`` DDL + ``gen_random_uuid``
shim pattern). A scripted fake Formance records every mirror and serves the
recorded mirrors back through ``list_transactions`` / ``get_balances``.

Asserts:
  (a) all five reports satisfy their identities with the exact expected
      numbers computed from the seeded postings;
  (b) P&L/BS/TB ``verify_against_postgres`` returns an empty diff
      (Formance-derived == Postgres-derived exactly);
  (c) sabotaging one mirror amount still satisfies the balance-sheet
      identity (the F24 profit-for-period line absorbs the drift) and
      produces non-empty P&L/BS/TB cross-check diffs.

Scenario numbers (integer pence, profit designed to zero so the balance
sheet identity holds identically with or without the F24 profit line):
  A. opening-balances: Dr 1000 2,000,000; Dr 1320 400,000; Cr 3000 2,400,000
  B. sales-invoice:      Dr 1100 120,000; Cr 4000 100,000; Cr 2210 20,000
  C. supplier-invoice:   Dr 5000 90,000; Dr 2220 18,000; Cr 2000 108,000
  D. depreciation:       Dr 6500 10,000; Cr 1360 10,000
  E. loan drawdown:      Dr 1000 500,000; Cr 2100 500,000

P&L: revenue 100,000; direct costs 90,000; depreciation 10,000; profit 0.
BS: assets 3,010,000 (bank 2,500,000 + debtors 120,000 + equipment 400,000
    − accumulated depreciation 10,000) == liabilities 610,000 (creditors
    108,000 + VAT net 2,000 + loan 500,000) + equity 2,400,000.
TB: debits == credits == 3,138,000.
CF: operating 0 + investing −400,000 + financing 2,900,000 == Δbank 2,500,000.
Funds: sources 2,910,000 == applications 2,910,000 (fixed assets 400,000 +
    working capital 2,510,000).
"""

from __future__ import annotations

import copy
import json
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.config.database import Base
from src.models.account import Account
from src.routes.formance_reports import formance_report as formance_report_route
from src.services.coa_service import CoaService
from src.services.formance_reports import (
    generate_bs,
    generate_cash_flow,
    generate_funds,
    generate_pl,
    generate_tb,
    verify_against_postgres,
)
from src.services.orchestrator import approve_entry, orchestrate_message
from src.services.transaction_service import TransactionService
from src.validators.transaction import PostingCreate, TransactionCreate

GBP = "GBP/2"

START = date(2026, 1, 1)
END = max(date(2026, 12, 31), date.today())

_SQLITE_SCHEMA = [
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
]

COA_SEED = [
    ("1000", "Bank Current Account", "Asset", "Bank"),
    ("1100", "Trade Debtors", "Asset", "CurrentAsset"),
    ("1320", "Computer Equipment", "Asset", "FixedAsset"),
    ("1360", "Accumulated Depreciation", "Asset", "FixedAsset"),
    ("2000", "Trade Creditors", "Liability", "CurrentLiability"),
    ("2040", "Corporation Tax Payable", "Liability", "CurrentLiability"),
    ("2100", "Bank Loan", "Liability", "LongTermLiability"),
    ("2210", "Output VAT", "Liability", "CurrentLiability"),
    ("2220", "Input VAT", "Liability", "CurrentLiability"),
    ("3000", "Share Capital", "Equity", "Equity"),
    ("3020", "Retained Earnings", "Equity", "Equity"),
    ("4000", "Sales - Standard Rated", "Revenue", "Revenue"),
    ("5000", "Cost of Sales", "Expense", "DirectCost"),
    ("6000", "Rent & Rates", "Expense", "Expense"),
    ("6500", "Depreciation", "Expense", "Expense"),
]

OPENING_RAW = (
    '{"skill_id": "opening-balances", "facts": {'
    '"opening_date": "2026-01-01", "assets": ['
    '{"code": "1000", "name": "Bank Current Account", "balance_pence": 2000000}, '
    '{"code": "1320", "name": "Computer Equipment", "balance_pence": 400000}], '
    '"liabilities": [], "equity_account_code": "3000"}}'
)

SALES_RAW = (
    '{"skill_id": "sales-invoice", "facts": {'
    '"customer_name": "Acme Corp", "invoice_ref": "INV-2001", '
    '"invoice_date": "2026-10-01", "lines": ['
    '{"description": "Consulting services", "net_pence": 100000, '
    '"vat_rate": "20%", "revenue_account_code": "4000"}]}}'
)

SUPPLIER_RAW = (
    '{"skill_id": "supplier-invoice", "facts": {'
    '"supplier_name": "Beta Office Ltd", "invoice_ref": "INV-3001", '
    '"invoice_date": "2026-10-02", "lines": ['
    '{"description": "Stock purchases", "net_pence": 90000, '
    '"vat_rate": "20%", "expense_account_code": "5000"}]}}'
)

# Zero-rated variant — no VAT legs, so the BS cross-check stays fully
# comparable (no engine-only 1130 reclassification) for the F24 test.
SUPPLIER_ZERO_VAT_RAW = (
    '{"skill_id": "supplier-invoice", "facts": {'
    '"supplier_name": "Beta Office Ltd", "invoice_ref": "INV-3002", '
    '"invoice_date": "2026-10-02", "lines": ['
    '{"description": "Stock purchases", "net_pence": 90000, '
    '"vat_rate": "0%", "expense_account_code": "5000"}]}}'
)

DEPRECIATION_RAW = (
    '{"skill_id": "depreciation", "facts": {'
    '"asset_account_code": "1320", "cost_pence": 400000, '
    '"residual_pence": 0, "useful_life_months": 40, '
    '"period_end_date": "2026-10-31", "method": "straight_line"}}'
)


class FakeFormance:
    """Scripted Formance ledger: records mirrors, serves them back.

    Balances are DERIVED from the recorded postings, so sabotaging a mirror
    amount propagates to every balance-based report automatically.
    """

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
            t
            for t in self.transactions
            if t["metadata"]["transaction_id"] == str(tx_id)
        )


def make_fake_llm(responses: dict[str, str]):
    async def transport(system_prompt: str, user_message: str) -> str:
        lowered = user_message.lower()
        for key, raw in responses.items():
            if key.lower() in lowered:
                return raw
        return '{"skill_id": null, "clarification": "unrecognised message"}'

    return transport


@pytest.fixture
async def ctx(monkeypatch):
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

    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    db = session_factory()

    now = datetime.now(UTC)
    db.add_all(
        [
            Account(
                id=uuid.uuid4(),
                code=code,
                name=name,
                category=category,
                type=type_,
                is_active=True,
                created_at=now,
                updated_at=now,
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
        patch("src.services.formance_reports.formance_client", fake),
    ):
        yield {"db": db, "fake": fake}

    await db.close()
    await engine.dispose()


async def _seed_scenario(ctx) -> dict:
    """Seed the mini scenario through the F6 orchestrator and approve each."""
    db, fake = ctx["db"], ctx["fake"]
    llm = make_fake_llm(
        {
            "opening balances": OPENING_RAW,
            "sales invoice INV-2001": SALES_RAW,
            "supplier invoice INV-3001": SUPPLIER_RAW,
            "depreciation": DEPRECIATION_RAW,
        }
    )

    opening = await orchestrate_message(
        db, "Opening balances as at 2026-01-01", llm=llm
    )
    assert opening.status == "preview", opening.error
    post = await approve_entry(db, opening.transaction_id)
    assert post.parity_ok is True

    sales = await orchestrate_message(
        db, "sales invoice INV-2001 to Acme Corp", llm=llm
    )
    assert sales.status == "preview", sales.error
    post = await approve_entry(db, sales.transaction_id)
    assert post.parity_ok is True

    supplier = await orchestrate_message(
        db, "supplier invoice INV-3001 from Beta Office Ltd", llm=llm
    )
    assert supplier.status == "preview", supplier.error
    post = await approve_entry(db, supplier.transaction_id)
    assert post.parity_ok is True

    depreciation = await orchestrate_message(
        db, "depreciation for the month on the computer equipment", llm=llm
    )
    assert depreciation.status == "preview", depreciation.error
    post = await approve_entry(db, depreciation.transaction_id)
    assert post.parity_ok is True

    # Loan drawdown — no dedicated constructor skill; direct TransactionService.
    bank = await CoaService.get_account_by_code(db, "1000")
    loan = await CoaService.get_account_by_code(db, "2100")
    assert bank is not None and loan is not None
    drawdown = await TransactionService.create_transaction(
        db,
        TransactionCreate(
            description="Bank loan drawdown",
            effective_date=date(2026, 3, 1),
            idempotency_key=uuid.uuid4(),
            postings=[
                PostingCreate(account_id=bank.id, debit_amount=500000),
                PostingCreate(account_id=loan.id, credit_amount=500000),
            ],
        ),
    )
    posted = await TransactionService.post_transaction(db, drawdown.id)
    assert posted.status == "posted"
    assert posted.reference == "JE-2026-0005"

    return {"sales": sales}


async def _all_reports(ctx):
    db = ctx["db"]
    return {
        "pl": await generate_pl(db, "main", START, END),
        "bs": await generate_bs(db, "main", as_at=END),
        "tb": await generate_tb(db, "main", as_at=END),
        "cf": await generate_cash_flow(db, "main", START, END),
        "funds": await generate_funds(db, "main", START, END),
    }


# ---------------------------------------------------------------------------
# (a) all five reports satisfy their identities with the expected numbers
# ---------------------------------------------------------------------------


async def test_mini_scenario_all_five_identities_hold(ctx) -> None:
    await _seed_scenario(ctx)
    reports = await _all_reports(ctx)
    for name, report in reports.items():
        assert report["identity_ok"] is True, f"{name} identity failed"


async def test_mini_scenario_pl_matches_expected_numbers(ctx) -> None:
    await _seed_scenario(ctx)
    pl = await generate_pl(ctx["db"], "main", START, END)
    assert pl["totals"] == {
        "revenue": 100000,
        "direct_costs": 90000,
        "expenses": 10000,
        "corporation_tax": 0,
    }
    assert pl["subtotals"] == {
        "gross_profit": 10000,
        "operating_profit": 0,
        "profit_before_tax": 0,
    }
    assert pl["profit"] == 0
    by_code = {
        line["code"]: line["amount"]
        for section in pl["sections"].values()
        for line in section
    }
    assert by_code["4000"] == 100000
    assert by_code["5000"] == 90000
    assert by_code["6500"] == 10000
    assert by_code["6000"] == 0


async def test_mini_scenario_bs_matches_expected_numbers(ctx) -> None:
    await _seed_scenario(ctx)
    bs = await generate_bs(ctx["db"], "main", as_at=END)
    assert bs["totals"] == {
        "assets": 3010000,
        "liabilities": 610000,
        "equity": 2400000,
        "liabilities_and_equity": 3010000,
    }
    sections = bs["sections"]
    bank = {line["code"]: line["amount"] for line in sections["bank"]}
    assert bank["1000"] == 2500000
    current = {line["code"]: line["amount"] for line in sections["current_assets"]}
    assert current["1100"] == 120000
    fixed = {line["code"]: line["amount"] for line in sections["fixed_assets"]}
    assert fixed["1320"] == 400000
    contra = {
        line["code"]: line["amount"]
        for line in sections["accumulated_depreciation"]
    }
    assert contra["1360"] == -10000
    liab = {
        line["code"]: line["amount"] for line in sections["current_liabilities"]
    }
    assert liab["2000"] == 108000
    assert liab["VAT"] == 2000
    assert sections["long_term_liabilities"][0]["code"] == "2100"
    assert sections["long_term_liabilities"][0]["amount"] == 500000
    equity = {line["code"]: line["amount"] for line in sections["equity"]}
    assert equity["3000"] == 2400000
    assert equity["3020"] == 0
    # F24: the profit-for-period line is present; the scenario profit is
    # designed to zero so it adds nothing to the identity.
    assert equity["profit_for_period"] == 0
    assert bs["vat_net"] == 2000


async def test_mini_scenario_tb_matches_expected_numbers(ctx) -> None:
    await _seed_scenario(ctx)
    tb = await generate_tb(ctx["db"], "main", as_at=END)
    assert tb["total_debits"] == 3138000
    assert tb["total_credits"] == 3138000
    rows = {row["code"]: row for row in tb["accounts"]}
    assert rows["1000"]["debit"] == 2500000
    assert rows["1360"]["credit"] == 10000
    assert rows["3000"]["credit"] == 2400000
    assert rows["4000"]["credit"] == 100000
    assert rows["2220"]["debit"] == 18000
    assert rows["2100"]["credit"] == 500000


async def test_mini_scenario_cash_flow_matches_expected_numbers(ctx) -> None:
    await _seed_scenario(ctx)
    cf = await generate_cash_flow(ctx["db"], "main", START, END)
    assert cf["subtotals"] == {
        "operating": 0,
        "investing": -400000,
        "financing": 2900000,
    }
    assert cf["reconciliation"] == {
        "net_change_in_cash": 2500000,
        "bank_opening": 0,
        "bank_closing": 2500000,
    }
    operating = cf["sections"]["operating"]
    assert operating[0]["amount"] == 0  # profit
    assert operating[1]["amount"] == 10000  # depreciation add-back
    by_code = {line["code"]: line["amount"] for line in operating[2:]}
    assert by_code["1100"] == -120000
    assert by_code["2000"] == 108000
    assert by_code["VAT"] == 2000
    assert cf["sections"]["investing"][0]["amount"] == -400000
    financing = {
        line["code"]: line["amount"] for line in cf["sections"]["financing"]
    }
    assert financing["2100"] == 500000
    assert financing["3000"] == 2400000


async def test_mini_scenario_funds_matches_expected_numbers(ctx) -> None:
    await _seed_scenario(ctx)
    funds = await generate_funds(ctx["db"], "main", START, END)
    assert funds["totals"] == {"sources": 2910000, "applications": 2910000}
    sources = {line["code"]: line["amount"] for line in funds["sources"]}
    assert sources["PROFIT"] == 0
    assert sources["6500"] == 10000
    assert sources["2100"] == 500000
    assert sources["3000"] == 2400000
    applications = {
        line["code"]: line["amount"] for line in funds["applications"]
    }
    assert applications["1300-1350"] == 400000
    assert applications["WC"] == 2510000
    assert funds["working_capital_split"] == {
        "current_assets": 2620000,
        "current_liabilities": 110000,
        "net": 2510000,
    }


async def test_mini_scenario_bs_current_profit_matches_pl(ctx) -> None:
    """Contract F24: seed capital + one posted zero-rated supplier invoice
    → the BS identity holds with profit_for_period == the P&L profit
    exactly, and verify_against_postgres stays empty (the BS cross-check
    compares section totals EXCLUDING the profit line — run_bs has no
    current-profit line)."""
    db, fake = ctx["db"], ctx["fake"]
    llm = make_fake_llm(
        {
            "opening balances": OPENING_RAW,
            "supplier invoice INV-3002": SUPPLIER_ZERO_VAT_RAW,
        }
    )

    opening = await orchestrate_message(
        db, "Opening balances as at 2026-01-01", llm=llm
    )
    assert opening.status == "preview", opening.error
    post = await approve_entry(db, opening.transaction_id)
    assert post.parity_ok is True

    supplier = await orchestrate_message(
        db, "supplier invoice INV-3002 from Beta Office Ltd", llm=llm
    )
    assert supplier.status == "preview", supplier.error
    post = await approve_entry(db, supplier.transaction_id)
    assert post.parity_ok is True

    pl = await generate_pl(db, "main", START, END)
    assert pl["profit"] == -90000

    bs = await generate_bs(db, "main", as_at=END)
    assert bs["identity_ok"] is True
    profit_line = next(
        line
        for line in bs["sections"]["equity"]
        if line["code"] == "profit_for_period"
    )
    assert profit_line["name"] == "Profit for the period"
    assert profit_line["amount"] == pl["profit"] == -90000
    assert bs["vat_net"] == 0
    assert bs["totals"] == {
        "assets": 2400000,
        "liabilities": 90000,
        "equity": 2310000,
        "liabilities_and_equity": 2400000,
    }
    # Empty diff: engine account-equity (2,400,000p, excluding the profit
    # line) == Postgres equity subtotal; the −90,000p profit line itself
    # has no Postgres equivalent and is not compared.
    assert await verify_against_postgres(db, bs, "bs") == []


# ---------------------------------------------------------------------------
# (b) cross-check against the Postgres ReportService — empty diff
# ---------------------------------------------------------------------------


async def test_verify_against_postgres_empty_for_pl_bs_tb(ctx) -> None:
    await _seed_scenario(ctx)
    db = ctx["db"]
    pl = await generate_pl(db, "main", START, END)
    assert await verify_against_postgres(db, pl, "pl") == []

    bs = await generate_bs(db, "main", as_at=END)
    assert await verify_against_postgres(db, bs, "bs") == []

    tb = await generate_tb(db, "main", as_at=END)
    assert await verify_against_postgres(db, tb, "tb") == []


# ---------------------------------------------------------------------------
# (c) sabotage — one mirror amount changed → identity violation + diffs
# ---------------------------------------------------------------------------


async def test_sabotage_mirror_amount_flags_identity_and_diff(ctx) -> None:
    seeded = await _seed_scenario(ctx)
    db, fake = ctx["db"], ctx["fake"]

    # Sabotage the sales mirror: £1,000.00 net sale becomes £1,001.00.
    sales_mirror = fake.mirror_for(str(seeded["sales"].transaction_id))
    assert sales_mirror["postings"][0]["source"] == "4000"
    sales_mirror["postings"][0]["amount"] = "1001.00"

    # Balance sheet: the F24 profit-for-period line absorbs the £10 drift
    # (revenue flows through profit), so the identity still holds — the
    # Postgres cross-check flags the drift on the asset side instead.
    bs = await generate_bs(db, "main", as_at=END)
    assert bs["identity_ok"] is True
    bs_profit = next(
        line
        for line in bs["sections"]["equity"]
        if line["code"] == "profit_for_period"
    )
    assert bs_profit["amount"] == 100
    bs_diffs = await verify_against_postgres(db, bs, "bs")
    assert bs_diffs
    bs_lines = {d["line"]: d for d in bs_diffs}
    assert bs_lines["current_assets"]["engine"] == 2620100
    assert bs_lines["current_assets"]["postgres"] == 2620000
    assert bs_lines["total_assets"]["engine"] == 3010100
    assert bs_lines["total_assets"]["postgres"] == 3010000

    # P&L still satisfies its own identity, but the cross-check flags drift.
    pl = await generate_pl(db, "main", START, END)
    assert pl["totals"]["revenue"] == 100100
    diffs = await verify_against_postgres(db, pl, "pl")
    assert diffs
    lines = {d["line"]: d for d in diffs}
    assert lines["revenue"]["engine"] == 100100
    assert lines["revenue"]["postgres"] == 100000
    assert lines["net_profit"]["delta"] == 100

    # Trial balance still balances (each posting is internally balanced) but
    # the per-account cross-check against Postgres is non-empty.
    tb = await generate_tb(db, "main", as_at=END)
    tb_diffs = await verify_against_postgres(db, tb, "tb")
    assert tb_diffs
    codes = {d["line"] for d in tb_diffs}
    assert "account 1100" in codes
    assert "account 4000" in codes


# ---------------------------------------------------------------------------
# Contract F30 — aged payables through the route (real Postgres aging +
# real entity-scoped engine TB).  The AR join needs contacts/invoices
# tables this fixture does not create (Base.metadata.create_all fails on
# SQLite and the fallback DDL has only four tables), so AR is covered by
# the route tests plus the AP/AR service tests in test_reports.py.
# ---------------------------------------------------------------------------


async def test_aging_payables_route_buckets_and_ledger_balance(ctx) -> None:
    await _seed_scenario(ctx)
    db, fake = ctx["db"], ctx["fake"]
    assert fake.transactions  # scenario really mirrored to the engine

    resp = await formance_report_route(
        report_type="aged-payables",
        as_at=END,
        entity_id=None,
        db=db,
    )

    assert resp.status_code == 200, resp.body
    body = json.loads(resp.body)
    assert body["report_type"] == "aged-payables"
    assert body["as_at"] == END.isoformat()
    report = body["report"]

    # Unmatched expense postings: supplier invoice Dr 5000 £900.00
    # (2026-10-02) + depreciation Dr 6500 £100.00 (2026-10-31) — both 61-90
    # days overdue against 2026-12-31.
    buckets = {b["bucket"]: b for b in report["buckets"]}
    assert buckets["61-90"] == {"bucket": "61-90", "count": 2, "total_pence": 100000}
    assert buckets["0-30"] == {"bucket": "0-30", "count": 0, "total_pence": 0}
    assert report["total_outstanding_pence"] == 100000
    assert {line["ref"] for line in report["lines"]} == {
        "JE-2026-0003",  # supplier invoice (NET 900.00 on 5000)
        "JE-2026-0004",  # depreciation (100.00 on 6500)
    }
    assert all(
        set(line) == {"contact_name", "ref", "due_date", "amount_pence", "days_overdue"}
        for line in report["lines"]
    )

    # Creditors 2000: engine TB signs credit balances negative — the line
    # matches the 2000 row of the trial balance exactly.
    tb = await generate_tb(db, "main", as_at=END)
    tb_2000 = next(row for row in tb["accounts"] if row["code"] == "2000")
    assert report["ledger_balance"] == {
        "code": "2000",
        "name": "Trade Creditors",
        "balance_pence": tb_2000["balance"],
    }
    assert report["ledger_balance"]["balance_pence"] == -108000
    assert report["note"]
    assert body["cross_check"] == {
        "ok": None,
        "diffs": [],
        "note": (
            "aging is invoice-derived (Postgres); ledger balance is "
            "Formance-derived — the difference is unpaid-draft timing, "
            "not a defect"
        ),
    }
