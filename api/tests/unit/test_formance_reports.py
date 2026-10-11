"""Unit tests for the Formance-derived report engine (Contract F9).

Scripted fake Formance (balances + cursor-paged transactions) over a real
in-memory SQLite COA: every report computed correctly from a known ledger
state, identities enforced (violation fixtures raise ReportEngineError),
unknown codes raise, £0.01 exactness survives, posted/reversed scope
filtering, pagination followed, sign conventions for credit accounts, and
the P&L/BS/TB cross-check against the Postgres ReportService.

The static binding tests parse the F8 SKILL.md Construction rules and
assert the engine's line-item code/class tables match — the catalog stays
the spec. The binding is per-GROUP (not per-report unions): moving a code
between line-item groups (e.g. 6310 overheads -> direct_costs) fails the
suite. The P&L assembly guard is tested by misassembling a section and
asserting ReportEngineError.
"""

from __future__ import annotations

import copy
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.config.database import Base
from src.models.account import Account
from src.models.transaction import Posting, Transaction
from src.services import formance_reports as fr
from src.services.formance_reports import (
    LINE_ITEM_CLASSES,
    LINE_ITEM_CODES,
    ReportEngineError,
    build_code_class_map,
    generate_bs,
    generate_cash_flow,
    generate_funds,
    generate_pl,
    generate_tb,
    verify_against_postgres,
)

GBP = "GBP/2"
REPO_ROOT = Path(__file__).resolve().parents[3]
REPORTS_DIR = REPO_ROOT / "skills" / "catalog" / "reports"
COA_DIR = REPO_ROOT / "api" / "src" / "coa_templates"

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


def _standard_coa_rows() -> list[tuple[str, str, str, str]]:
    """Build a COA covering every engine line-item code, from the tables."""
    rows: dict[str, tuple[str, str]] = {}

    def add(codes: tuple[str, ...], category: str, type_: str) -> None:
        for code in codes:
            rows.setdefault(code, (category, type_))

    pl = LINE_ITEM_CODES["profit-and-loss"]
    bs = LINE_ITEM_CODES["balance-sheet"]
    add(pl["revenue"], "Revenue", "Revenue")
    add(pl["direct_costs"], "Expense", "DirectCost")
    add(pl["overheads"], "Expense", "Expense")
    add(pl["staff_costs"], "Expense", "Expense")
    add(pl["depreciation"], "Expense", "Expense")
    add(pl["finance_costs"], "Expense", "Expense")
    add(pl["corporation_tax"], "Liability", "CurrentLiability")
    add(bs["bank"], "Asset", "Bank")
    add(bs["current_assets"], "Asset", "CurrentAsset")
    add(bs["fixed_assets"], "Asset", "FixedAsset")
    add(bs["accumulated_depreciation"], "Asset", "FixedAsset")
    add(bs["current_liabilities"], "Liability", "CurrentLiability")
    add(bs["vat"], "Liability", "CurrentLiability")
    add(bs["long_term_liabilities"], "Liability", "LongTermLiability")
    add(bs["equity"], "Equity", "Equity")
    add(bs["contra_equity"], "Equity", "Equity")
    return [
        (code, f"Account {code}", category, type_)
        for code, (category, type_) in sorted(rows.items())
    ]


async def _make_db(
    coa_rows: list[tuple[str, str, str, str]] | None = None,
) -> tuple[Any, AsyncSession]:
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
    for code, name, category, type_ in (coa_rows or _standard_coa_rows()):
        db.add(
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
        )
    await db.commit()
    return engine, db


class ScriptedFormance:
    """Scripted fake Formance: cursor-paged transactions + fixed balances."""

    def __init__(self, transactions=None, balances=None, page_size=None) -> None:
        self.transactions = list(transactions or [])
        self.balances = dict(balances or {})
        self.page_size = page_size
        self.list_calls: list[dict] = []
        self.balance_calls: list[dict] = []

    async def list_transactions(self, ledger, page_size=None, after=None):
        self.list_calls.append(
            {"ledger": ledger, "page_size": page_size, "after": after}
        )
        size = (
            self.page_size
            if self.page_size is not None
            else (page_size or len(self.transactions))
        )
        txs = sorted(self.transactions, key=lambda t: t.get("txid", 0))
        if after is not None:
            txs = [t for t in txs if t.get("txid", 0) > int(after)]
        batch = txs[:size]
        rest = txs[size:]
        if rest:
            cursor = {"hasMore": True, "next": str(batch[-1]["txid"])}
        else:
            cursor = {"hasMore": False}
        return {"cursor": cursor, "data": copy.deepcopy(batch)}

    async def get_balances(self, ledger, address=None):
        self.balance_calls.append({"ledger": ledger, "address": address})
        return {
            "data": [
                {"address": addr, "balances": {"GBP/2": str(value)}}
                for addr, value in self.balances.items()
            ]
        }


@dataclass
class World:
    db: AsyncSession
    engine: Any
    fake: ScriptedFormance
    ids: dict[str, uuid.UUID]
    period_start: date
    period_end: date

    async def close(self) -> None:
        await self.db.close()
        await self.engine.dispose()

    def id(self, key: str) -> str:
        return str(self.ids[key])


async def make_world(
    monkeypatch,
    *,
    entries=None,
    balances=None,
    coa_rows=None,
    page_size=None,
    period_start=date(2026, 1, 1),
    period_end=date(2026, 12, 31),
) -> World:
    """Real SQLite COA/scope + scripted Formance, patched into the engine.

    ``entries``: list of (key, status, timestamp, postings, metadata) where
    metadata is a dict or None (None strips the metadata key entirely).
    """
    engine, db = await _make_db(coa_rows)
    fake = ScriptedFormance(balances=balances, page_size=page_size)
    ids: dict[str, uuid.UUID] = {}
    now = datetime.now(UTC)
    for i, (key, status, timestamp, postings, metadata) in enumerate(
        entries or [], start=1
    ):
        txid = uuid.uuid4()
        ids[key] = txid
        db.add(
            Transaction(
                id=txid,
                reference=f"JE-2026-{i:04d}",
                description=key,
                currency="GBP",
                status=status,
                effective_date=period_start,
                total_amount=0,
                created_at=now,
                updated_at=now,
            )
        )
        mirror: dict = {
            "txid": i,
            "reference": f"JE-2026-{i:04d}",
            "timestamp": timestamp,
            "postings": copy.deepcopy(postings),
        }
        if metadata is not None:
            meta = dict(metadata)
            meta.setdefault("transaction_id", str(txid))
            mirror["metadata"] = meta
        fake.transactions.append(mirror)
    await db.commit()
    monkeypatch.setattr("src.services.formance_reports.formance_client", fake)
    return World(db, engine, fake, ids, period_start, period_end)


def _sale_posting(amount: str) -> dict:
    return {"source": "4000", "destination": "1100", "amount": amount, "asset": GBP}


# ---------------------------------------------------------------------------
# Canonical flow scenario: capital, sale + VAT, supplier + VAT, depreciation,
# loan drawdown, loan interest, rent — all within the 2026 period.
# ---------------------------------------------------------------------------

CANONICAL_ENTRIES = [
    ("capital", "posted", "2026-01-05T09:00:00+00:00", [
        {"source": "3000", "destination": "1000", "amount": "20000.00", "asset": GBP},
        {"source": "3000", "destination": "1320", "amount": "4000.00", "asset": GBP},
    ], {}),
    ("sale", "posted", "2026-02-10T09:00:00+00:00", [
        {"source": "4000", "destination": "1100", "amount": "1000.00", "asset": GBP},
        {"source": "2210", "destination": "1100", "amount": "200.00", "asset": GBP},
    ], {}),
    ("supplier", "posted", "2026-03-01T09:00:00+00:00", [
        {"source": "2000", "destination": "5000", "amount": "900.00", "asset": GBP},
        {"source": "2000", "destination": "2220", "amount": "180.00", "asset": GBP},
    ], {}),
    ("depreciation", "posted", "2026-04-01T09:00:00+00:00", [
        {"source": "1360", "destination": "6500", "amount": "100.00", "asset": GBP},
    ], {}),
    ("loan", "posted", "2026-05-01T09:00:00+00:00", [
        {"source": "2100", "destination": "1000", "amount": "5000.00", "asset": GBP},
    ], {}),
    ("interest", "posted", "2026-06-01T09:00:00+00:00", [
        {"source": "1000", "destination": "6410", "amount": "50.00", "asset": GBP},
    ], {}),
    ("rent", "posted", "2026-07-01T09:00:00+00:00", [
        {"source": "1000", "destination": "6000", "amount": "300.00", "asset": GBP},
    ], {}),
]

# Expected P&L over the canonical flows (integer pence).
CANONICAL_PL = {
    "revenue": 100000,
    "direct_costs": 90000,
    "overheads": 30000,
    "staff_costs": 0,
    "depreciation": 10000,
    "finance_costs": 5000,
    "corporation_tax": 0,
    "gross_profit": 10000,
    "operating_profit": -30000,
    "profit_before_tax": -35000,
    "profit": -35000,
}

# Expected cash flow / funds over the canonical flows.
CANONICAL_CF = {
    "operating": -35000,
    "investing": -400000,
    "financing": 2900000,
    "net_change": 2465000,
    "bank_opening": 0,
    "bank_closing": 2465000,
}
CANONICAL_FUNDS = {
    "sources_total": 2875000,
    "applications_total": 2875000,
    "wc_movement": 2475000,
    "ca_delta": 2585000,
    "cl_delta": 110000,
}


async def _canonical_world(monkeypatch, **kwargs) -> World:
    kwargs.setdefault("balances", {"1000": "24650.00"})
    return await make_world(monkeypatch, entries=CANONICAL_ENTRIES, **kwargs)


# ---------------------------------------------------------------------------
# build_code_class_map
# ---------------------------------------------------------------------------


def test_build_code_class_map_from_dicts_and_orm() -> None:
    dict_rows = [{"code": "1000", "category": "Asset", "type": "Bank"}]
    assert build_code_class_map(dict_rows) == {"1000": "Asset (Bank)"}

    class FakeAccount:
        code = "2000"
        category = "Liability"
        type = "CurrentLiability"

    assert build_code_class_map([FakeAccount()]) == {
        "2000": "Liability (CurrentLiability)"
    }


# ---------------------------------------------------------------------------
# P&L
# ---------------------------------------------------------------------------


async def test_pl_known_scenario_correct(monkeypatch) -> None:
    world = await _canonical_world(monkeypatch)
    try:
        report = await generate_pl(
            world.db, "main", world.period_start, world.period_end
        )
        assert report["identity_ok"] is True
        assert report["totals"] == {
            "revenue": CANONICAL_PL["revenue"],
            "direct_costs": CANONICAL_PL["direct_costs"],
            "expenses": (
                CANONICAL_PL["overheads"]
                + CANONICAL_PL["staff_costs"]
                + CANONICAL_PL["depreciation"]
                + CANONICAL_PL["finance_costs"]
            ),
            "corporation_tax": 0,
        }
        assert report["subtotals"] == {
            "gross_profit": 10000,
            "operating_profit": -30000,
            "profit_before_tax": -35000,
        }
        assert report["profit"] == -35000

        by_code = {
            line["code"]: line["amount"]
            for section in report["sections"].values()
            for line in section
        }
        assert by_code["4000"] == 100000
        assert by_code["5000"] == 90000
        assert by_code["6000"] == 30000
        assert by_code["6500"] == 10000
        assert by_code["6410"] == 5000
        assert by_code["2040"] == 0
    finally:
        await world.close()


async def test_pl_sign_conventions_revenue_positive_expenses_positive(monkeypatch) -> None:
    world = await _canonical_world(monkeypatch)
    try:
        report = await generate_pl(world.db, "main", world.period_start, world.period_end)
        sections = report["sections"]
        assert all(line["amount"] >= 0 for line in sections["revenue"])
        assert all(line["amount"] >= 0 for line in sections["direct_costs"])
        assert all(line["amount"] >= 0 for line in sections["overheads"])
        assert report["profit"] == report["totals"]["revenue"] - sum(
            section_total
            for section_total in (
                report["totals"]["direct_costs"],
                report["totals"]["expenses"],
                report["totals"]["corporation_tax"],
            )
        )
    finally:
        await world.close()


async def test_pl_corporation_tax_charge_and_payment(monkeypatch) -> None:
    entries = [
        ("sale", "posted", "2026-01-02T00:00:00+00:00", [_sale_posting("1000.00")], {}),
        ("provision", "posted", "2026-01-03T00:00:00+00:00", [
            {"source": "2040", "destination": "3020", "amount": "100.00", "asset": GBP},
        ], {}),
        ("payment", "posted", "2026-01-04T00:00:00+00:00", [
            {"source": "1000", "destination": "2040", "amount": "40.00", "asset": GBP},
        ], {}),
    ]
    world = await make_world(monkeypatch, entries=entries)
    try:
        report = await generate_pl(world.db, "main", world.period_start, world.period_end)
        # Charge = credit flow on 2040 (the provision). The payment is a
        # balance-sheet movement and never reduces the charge.
        assert report["totals"]["corporation_tax"] == 10000
        assert report["profit"] == 100000 - 10000
    finally:
        await world.close()


async def test_pl_guard_fires_when_section_drops_a_line(monkeypatch) -> None:
    """The assembly guard is load-bearing: a section that silently drops a
    line item must raise (assembled subtotal != independent raw flow
    total), not return a quietly wrong P&L."""
    world = await _canonical_world(monkeypatch)
    try:
        real = fr._pl_from_flows

        def misassembled(debit, credit, class_map, name_map):
            body = real(debit, credit, class_map, name_map)
            body["sections"]["overheads"] = body["sections"]["overheads"][1:]
            return body

        monkeypatch.setattr(fr, "_pl_from_flows", misassembled)
        with pytest.raises(
            ReportEngineError, match="P&L section misassembled"
        ) as info:
            await generate_pl(world.db, "main", world.period_start, world.period_end)
        assert "overheads" in str(info.value)
    finally:
        await world.close()


async def test_pl_guard_fires_when_tax_line_dropped(monkeypatch) -> None:
    entries = [
        ("sale", "posted", "2026-01-02T00:00:00+00:00", [_sale_posting("1000.00")], {}),
        ("provision", "posted", "2026-01-03T00:00:00+00:00", [
            {"source": "2040", "destination": "3020", "amount": "100.00", "asset": GBP},
        ], {}),
    ]
    world = await make_world(monkeypatch, entries=entries)
    try:
        real = fr._pl_from_flows

        def misassembled(debit, credit, class_map, name_map):
            body = real(debit, credit, class_map, name_map)
            body["sections"]["corporation_tax"] = []
            return body

        monkeypatch.setattr(fr, "_pl_from_flows", misassembled)
        with pytest.raises(
            ReportEngineError, match="corporation_tax"
        ) as info:
            await generate_pl(world.db, "main", world.period_start, world.period_end)
        assert "P&L section misassembled" in str(info.value)
    finally:
        await world.close()


async def test_pl_empty_period_all_zero(monkeypatch) -> None:
    world = await make_world(monkeypatch, entries=[])
    try:
        report = await generate_pl(world.db, "main", world.period_start, world.period_end)
        assert report["profit"] == 0
        assert report["totals"]["revenue"] == 0
        assert all(
            line["amount"] == 0
            for section in report["sections"].values()
            for line in section
        )
    finally:
        await world.close()


async def test_pl_penny_exactness(monkeypatch) -> None:
    entries = [
        ("a", "posted", "2026-01-02T00:00:00+00:00", [_sale_posting("0.01")], {}),
        ("b", "posted", "2026-01-03T00:00:00+00:00", [_sale_posting("123.45")], {}),
    ]
    world = await make_world(monkeypatch, entries=entries)
    try:
        report = await generate_pl(world.db, "main", world.period_start, world.period_end)
        assert report["totals"]["revenue"] == 1 + 12345
        assert report["profit"] == 12346
    finally:
        await world.close()


async def test_pl_unparseable_amount_raises(monkeypatch) -> None:
    entries = [
        ("bad", "posted", "2026-01-02T00:00:00+00:00", [_sale_posting("abc")], {}),
    ]
    world = await make_world(monkeypatch, entries=entries)
    try:
        with pytest.raises(ReportEngineError, match="unparseable posting amount"):
            await generate_pl(world.db, "main", world.period_start, world.period_end)
    finally:
        await world.close()


async def test_pl_unknown_code_raises(monkeypatch) -> None:
    entries = [
        ("bad", "posted", "2026-01-02T00:00:00+00:00", [
            {"source": "4000", "destination": "9999", "amount": "10.00", "asset": GBP},
        ], {}),
    ]
    world = await make_world(monkeypatch, entries=entries)
    try:
        with pytest.raises(ReportEngineError, match="9999"):
            await generate_pl(world.db, "main", world.period_start, world.period_end)
    finally:
        await world.close()


async def test_flow_scope_filtering_posted_reversed_only(monkeypatch) -> None:
    foreign = uuid.uuid4()
    entries = [
        ("posted_ok", "posted", "2026-01-02T00:00:00+00:00", [_sale_posting("10.00")], {}),
        ("draft_out", "draft", "2026-01-03T00:00:00+00:00", [_sale_posting("10.00")], {}),
        ("reversed_ok", "reversed", "2026-01-04T00:00:00+00:00", [_sale_posting("10.00")], {}),
        ("no_meta", "posted", "2026-01-05T00:00:00+00:00", [_sale_posting("10.00")], None),
        ("foreign_id", "posted", "2026-01-06T00:00:00+00:00", [_sale_posting("10.00")],
         {"transaction_id": str(foreign)}),
    ]
    world = await make_world(monkeypatch, entries=entries)
    try:
        report = await generate_pl(world.db, "main", world.period_start, world.period_end)
        assert report["totals"]["revenue"] == 2000
    finally:
        await world.close()


async def test_pagination_followed(monkeypatch) -> None:
    entries = [
        (f"t{i}", "posted", "2026-01-02T00:00:00+00:00", [_sale_posting("10.00")], {})
        for i in range(5)
    ]
    world = await make_world(monkeypatch, entries=entries, page_size=2)
    try:
        report = await generate_pl(world.db, "main", world.period_start, world.period_end)
        assert report["totals"]["revenue"] == 5000
        afters = [call["after"] for call in world.fake.list_calls]
        assert afters == [None, "2", "4"]
        assert all(call["page_size"] == 500 for call in world.fake.list_calls)
    finally:
        await world.close()


async def test_timestamp_period_filtering(monkeypatch) -> None:
    entries = [
        ("before", "posted", "2025-12-31T23:59:59+00:00", [_sale_posting("10.00")], {}),
        ("start_edge", "posted", "2026-01-01T00:00:00+00:00", [_sale_posting("10.00")], {}),
        ("end_edge", "posted", "2026-12-31T23:59:59+00:00", [_sale_posting("10.00")], {}),
        ("after", "posted", "2027-01-01T00:00:00+00:00", [_sale_posting("10.00")], {}),
        ("no_ts", "posted", None, [_sale_posting("10.00")], {}),
        ("bad_ts", "posted", "not-a-timestamp", [_sale_posting("10.00")], {}),
    ]
    world = await make_world(monkeypatch, entries=entries)
    try:
        report = await generate_pl(world.db, "main", world.period_start, world.period_end)
        assert report["totals"]["revenue"] == 4000
    finally:
        await world.close()


# ---------------------------------------------------------------------------
# Balance sheet
# ---------------------------------------------------------------------------

BS_BALANCES = {
    "1000": "1910.00",
    "1100": "1200.00",
    "1110": "40.00",
    "1310": "500.00",
    "1320": "2500.00",
    "1360": "-500.00",
    "2000": "-1000.00",
    "2010": "-250.00",
    "2040": "-361.00",
    "2100": "-1000.00",
    "2210": "-20.00",
    "2220": "20.00",
    "3000": "-1500.00",
    "3020": "-1539.00",
}


async def _bs_world(monkeypatch, balances=None) -> World:
    return await make_world(monkeypatch, entries=[], balances=balances or BS_BALANCES)


async def test_bs_known_scenario_correct_and_signs(monkeypatch) -> None:
    world = await _bs_world(monkeypatch)
    try:
        report = await generate_bs(world.db, "main", as_at=date(2026, 12, 31))
        assert report["identity_ok"] is True
        assert report["totals"] == {
            "assets": 565000,
            "liabilities": 261100,
            "equity": 303900,
            "liabilities_and_equity": 565000,
        }
        sections = report["sections"]
        by_code = {
            line["code"]: line["amount"]
            for section in (
                sections["bank"],
                sections["current_assets"],
                sections["fixed_assets"],
                sections["accumulated_depreciation"],
            )
            for line in section
        }
        assert by_code["1000"] == 191000
        assert by_code["1100"] == 120000
        assert by_code["1110"] == 4000
        assert by_code["1310"] == 50000
        assert by_code["1320"] == 250000
        assert by_code["1360"] == -50000

        liab = {line["code"]: line["amount"] for line in sections["current_liabilities"]}
        assert liab["2000"] == 100000
        assert liab["2010"] == 25000
        assert liab["2040"] == 36100
        assert liab["VAT"] == 0
        lt = {line["code"]: line["amount"] for line in sections["long_term_liabilities"]}
        assert lt["2100"] == 100000
        assert lt["2110"] == 0
        eq = {line["code"]: line["amount"] for line in sections["equity"]}
        assert eq["3000"] == 150000
        assert eq["3020"] == 153900
    finally:
        await world.close()


async def test_bs_vat_net_positive_shown_as_liability(monkeypatch) -> None:
    balances = {"2210": "-200.00", "2220": "80.00", "1000": "120.00"}
    world = await _bs_world(monkeypatch, balances)
    try:
        report = await generate_bs(world.db, "main", as_at=date(2026, 12, 31))
        assert report["vat_net"] == 12000
        assert report["vat_recoverable_asset"] == 0
        vat_line = next(
            line for line in report["sections"]["current_liabilities"]
            if line["code"] == "VAT"
        )
        assert vat_line["amount"] == 12000
        assert report["totals"] == {
            "assets": 12000,
            "liabilities": 12000,
            "equity": 0,
            "liabilities_and_equity": 12000,
        }
    finally:
        await world.close()


async def test_bs_vat_net_negative_presented_as_1130_asset(monkeypatch) -> None:
    balances = {"2210": "-80.00", "2220": "200.00", "1000": "120.00", "3000": "-240.00"}
    world = await _bs_world(monkeypatch, balances)
    try:
        report = await generate_bs(world.db, "main", as_at=date(2026, 12, 31))
        assert report["vat_net"] == -12000
        assert report["vat_recoverable_asset"] == 12000
        recoverable = [
            line for line in report["sections"]["current_assets"]
            if line["code"] == "1130"
        ]
        assert len(recoverable) == 1
        assert recoverable[0]["amount"] == 12000
        vat_line = next(
            line for line in report["sections"]["current_liabilities"]
            if line["code"] == "VAT"
        )
        assert vat_line["amount"] == 0
        assert report["totals"]["assets"] == 24000
        assert report["totals"]["liabilities"] == 0
        assert report["totals"]["equity"] == 24000
    finally:
        await world.close()


async def test_bs_identity_violation_raises(monkeypatch) -> None:
    balances = {"1000": "100.00", "3000": "-101.00"}
    world = await _bs_world(monkeypatch, balances)
    try:
        with pytest.raises(ReportEngineError, match="balance sheet identity violated"):
            await generate_bs(world.db, "main", as_at=date(2026, 12, 31))
    finally:
        await world.close()


async def test_bs_unknown_code_raises(monkeypatch) -> None:
    world = await _bs_world(monkeypatch, {"9999": "10.00"})
    try:
        with pytest.raises(ReportEngineError, match="9999"):
            await generate_bs(world.db, "main", as_at=date(2026, 12, 31))
    finally:
        await world.close()


async def test_bs_penny_exactness(monkeypatch) -> None:
    balances = {"1000": "0.01", "3000": "-0.01"}
    world = await _bs_world(monkeypatch, balances)
    try:
        report = await generate_bs(world.db, "main", as_at=date(2026, 12, 31))
        assert report["totals"]["assets"] == 1
        assert report["totals"]["equity"] == 1
    finally:
        await world.close()


# ---------------------------------------------------------------------------
# Balance sheet — Contract F24: profit_for_period (flow-derived current
# profit in equity; identity assets == liabilities + equity + profit)
# ---------------------------------------------------------------------------


def _profit_line(report: dict) -> dict:
    return next(
        line
        for line in report["sections"]["equity"]
        if line["code"] == "profit_for_period"
    )


async def test_bs_current_profit_capital_plus_expense(monkeypatch) -> None:
    """A posted expense no longer breaks the identity: the equity section
    carries profit_for_period = −expense (exact pence) and the identity
    holds with assets == liabilities + equity (accounts + profit)."""
    entries = [
        ("capital", "posted", "2026-01-05T09:00:00+00:00", [
            {"source": "3000", "destination": "1000", "amount": "1000.00", "asset": GBP},
        ], {}),
        ("expense", "posted", "2026-02-01T09:00:00+00:00", [
            {"source": "1000", "destination": "6000", "amount": "50.00", "asset": GBP},
        ], {}),
    ]
    balances = {"1000": "950.00", "6000": "50.00", "3000": "-1000.00"}
    world = await make_world(monkeypatch, entries=entries, balances=balances)
    try:
        report = await generate_bs(world.db, "main", as_at=date(2026, 12, 31))
        assert report["identity_ok"] is True
        assert _profit_line(report) == {
            "code": "profit_for_period",
            "name": "Profit for the period",
            "amount": -5000,
        }
        eq = {line["code"]: line["amount"] for line in report["sections"]["equity"]}
        assert eq["3000"] == 100000
        assert report["totals"] == {
            "assets": 95000,
            "liabilities": 0,
            "equity": 95000,
            "liabilities_and_equity": 95000,
        }
    finally:
        await world.close()


async def test_bs_current_profit_income_only(monkeypatch) -> None:
    """Income-only period: profit_for_period is positive revenue and the
    identity holds (assets == equity accounts + profit)."""
    entries = [
        ("sale", "posted", "2026-01-02T00:00:00+00:00", [_sale_posting("10.00")], {}),
    ]
    balances = {"1100": "10.00", "4000": "-10.00"}
    world = await make_world(monkeypatch, entries=entries, balances=balances)
    try:
        report = await generate_bs(world.db, "main", as_at=date(2026, 12, 31))
        assert report["identity_ok"] is True
        assert _profit_line(report)["amount"] == 1000
        assert report["totals"] == {
            "assets": 1000,
            "liabilities": 0,
            "equity": 1000,
            "liabilities_and_equity": 1000,
        }
    finally:
        await world.close()


async def test_bs_current_profit_live_60_shape(monkeypatch) -> None:
    """The live £60 supplier-invoice shape: Dr 5000 5,000p expense, Dr 2220
    1,000p VAT input, Cr 2000 6,000p creditors → profit −5,000p, VAT
    recoverable 1,000p in assets, identity exact in pence."""
    entries = [
        ("supplier", "posted", "2026-10-02T09:00:00+00:00", [
            {"source": "2000", "destination": "5000", "amount": "50.00", "asset": GBP},
            {"source": "2000", "destination": "2220", "amount": "10.00", "asset": GBP},
        ], {}),
    ]
    balances = {"5000": "50.00", "2220": "10.00", "2000": "-60.00"}
    world = await make_world(monkeypatch, entries=entries, balances=balances)
    try:
        report = await generate_bs(world.db, "main", as_at=date(2026, 12, 31))
        assert report["identity_ok"] is True
        assert _profit_line(report)["amount"] == -5000
        assert report["vat_recoverable_asset"] == 1000
        assert report["totals"] == {
            "assets": 1000,
            "liabilities": 6000,
            "equity": -5000,
            "liabilities_and_equity": 1000,
        }
    finally:
        await world.close()


async def test_bs_current_profit_period_start_filtering(monkeypatch) -> None:
    """The fiscal-window ``period_start`` filters the profit flows: the
    pre-window expense is excluded from profit_for_period (the balances
    already close it into 3020), and dropping the window lets it leak in
    and breaks the identity loudly."""
    entries = [
        ("capital", "posted", "2026-01-05T09:00:00+00:00", [
            {"source": "3000", "destination": "1000", "amount": "1000.00", "asset": GBP},
        ], {}),
        ("expense_pre", "posted", "2026-02-01T09:00:00+00:00", [
            {"source": "1000", "destination": "6000", "amount": "50.00", "asset": GBP},
        ], {}),
        ("expense_in", "posted", "2026-07-01T09:00:00+00:00", [
            {"source": "1000", "destination": "6000", "amount": "20.00", "asset": GBP},
        ], {}),
    ]
    # Balances as at 31 Dec: the pre-window expense is already closed into
    # retained earnings (3020 carries the Dr 5,000p).
    balances = {"1000": "930.00", "3000": "-1000.00", "3020": "50.00"}
    world = await make_world(monkeypatch, entries=entries, balances=balances)
    try:
        report = await generate_bs(
            world.db,
            "main",
            as_at=date(2026, 12, 31),
            period_start=date(2026, 6, 1),
        )
        assert report["identity_ok"] is True
        assert _profit_line(report)["amount"] == -2000
        assert report["totals"] == {
            "assets": 93000,
            "liabilities": 0,
            "equity": 93000,
            "liabilities_and_equity": 93000,
        }
        # Inception (no window): both expenses flow into the profit line
        # and the identity breaks loudly instead of being fudged.
        with pytest.raises(ReportEngineError, match="balance sheet identity violated"):
            await generate_bs(world.db, "main", as_at=date(2026, 12, 31))
    finally:
        await world.close()


# ---------------------------------------------------------------------------
# Trial balance
# ---------------------------------------------------------------------------


async def test_tb_known_scenario_two_columns_by_sign(monkeypatch) -> None:
    world = await _bs_world(monkeypatch)
    try:
        report = await generate_tb(world.db, "main", as_at=date(2026, 12, 31))
        assert report["identity_ok"] is True
        assert report["total_debits"] == 617000
        assert report["total_credits"] == 617000
        rows = {row["code"]: row for row in report["accounts"]}
        assert rows["1000"]["debit"] == 191000 and rows["1000"]["credit"] == 0
        assert rows["1360"]["debit"] == 0 and rows["1360"]["credit"] == 50000
        assert rows["2000"]["debit"] == 0 and rows["2000"]["credit"] == 100000
        assert rows["3020"]["credit"] == 153900
        assert rows["2220"]["debit"] == 2000 and rows["2220"]["credit"] == 0
        for row in report["accounts"]:
            assert row["balance"] == row["debit"] - row["credit"]
            assert row["debit"] == 0 or row["credit"] == 0
    finally:
        await world.close()


async def test_tb_identity_violation_raises(monkeypatch) -> None:
    world = await _bs_world(monkeypatch, {"1000": "100.00", "2000": "-99.00"})
    try:
        with pytest.raises(ReportEngineError, match="trial balance does not balance"):
            await generate_tb(world.db, "main", as_at=date(2026, 12, 31))
    finally:
        await world.close()


async def test_tb_unknown_code_raises(monkeypatch) -> None:
    world = await _bs_world(monkeypatch, {"9999": "10.00"})
    try:
        with pytest.raises(ReportEngineError, match="9999"):
            await generate_tb(world.db, "main", as_at=date(2026, 12, 31))
    finally:
        await world.close()


async def test_tb_penny_exactness(monkeypatch) -> None:
    world = await _bs_world(monkeypatch, {"1000": "0.01", "3000": "-0.01"})
    try:
        report = await generate_tb(world.db, "main", as_at=date(2026, 12, 31))
        assert report["total_debits"] == 1
        assert report["total_credits"] == 1
    finally:
        await world.close()


# ---------------------------------------------------------------------------
# Cash flow
# ---------------------------------------------------------------------------


async def test_cash_flow_known_scenario(monkeypatch) -> None:
    world = await _canonical_world(monkeypatch)
    try:
        report = await generate_cash_flow(
            world.db, "main", world.period_start, world.period_end
        )
        assert report["identity_ok"] is True
        assert report["subtotals"] == {
            "operating": CANONICAL_CF["operating"],
            "investing": CANONICAL_CF["investing"],
            "financing": CANONICAL_CF["financing"],
        }
        assert report["reconciliation"] == {
            "net_change_in_cash": CANONICAL_CF["net_change"],
            "bank_opening": 0,
            "bank_closing": CANONICAL_CF["bank_closing"],
        }
        operating = report["sections"]["operating"]
        assert operating[0]["amount"] == CANONICAL_PL["profit"]
        assert operating[1]["amount"] == 10000  # depreciation add-back
        by_code = {line["code"]: line["amount"] for line in operating[2:]}
        assert by_code["1100"] == -120000
        assert by_code["2000"] == 108000
        assert by_code["VAT"] == 2000
        assert report["sections"]["investing"][0]["amount"] == -400000
        financing = {
            line["code"]: line["amount"] for line in report["sections"]["financing"]
        }
        assert financing["2100"] == 500000
        assert financing["3000"] == 2400000
    finally:
        await world.close()


async def test_cash_flow_identity_violation_raises(monkeypatch) -> None:
    entries = [
        ("equity_direct", "posted", "2026-01-02T00:00:00+00:00", [
            {"source": "3020", "destination": "1000", "amount": "50.00", "asset": GBP},
        ], {}),
    ]
    world = await make_world(monkeypatch, entries=entries, balances={"1000": "50.00"})
    try:
        with pytest.raises(ReportEngineError, match="cash flow identity violated") as info:
            await generate_cash_flow(world.db, "main", world.period_start, world.period_end)
        assert "3020" in str(info.value)
    finally:
        await world.close()


async def test_cash_flow_scope_and_period_filtering(monkeypatch) -> None:
    entries = [
        ("in", "posted", "2026-01-02T00:00:00+00:00", [_sale_posting("100.00")], {}),
        ("draft", "draft", "2026-01-03T00:00:00+00:00", [_sale_posting("100.00")], {}),
        ("future", "posted", "2027-01-01T00:00:00+00:00", [_sale_posting("100.00")], {}),
    ]
    world = await make_world(monkeypatch, entries=entries, balances={"1100": "100.00"})
    try:
        report = await generate_cash_flow(
            world.db, "main", world.period_start, world.period_end
        )
        # Only the in-scope sale moves debtors: operating = profit + Δ1100.
        assert report["subtotals"]["operating"] == 10000 - 10000
        assert report["reconciliation"]["net_change_in_cash"] == 0
    finally:
        await world.close()


# ---------------------------------------------------------------------------
# Funds
# ---------------------------------------------------------------------------


async def test_funds_known_scenario(monkeypatch) -> None:
    world = await _canonical_world(monkeypatch)
    try:
        report = await generate_funds(
            world.db, "main", world.period_start, world.period_end
        )
        assert report["identity_ok"] is True
        assert report["totals"] == {
            "sources": CANONICAL_FUNDS["sources_total"],
            "applications": CANONICAL_FUNDS["applications_total"],
        }
        sources = {line["code"]: line["amount"] for line in report["sources"]}
        assert sources["PROFIT"] == CANONICAL_PL["profit"]
        assert sources["6500"] == 10000
        assert sources["2100"] == 500000
        assert sources["3000"] == 2400000
        applications = {
            line["code"]: line["amount"] for line in report["applications"]
        }
        assert applications["1300-1350"] == 400000
        assert applications["WC"] == CANONICAL_FUNDS["wc_movement"]
        assert report["working_capital_split"] == {
            "current_assets": CANONICAL_FUNDS["ca_delta"],
            "current_liabilities": CANONICAL_FUNDS["cl_delta"],
            "net": CANONICAL_FUNDS["wc_movement"],
        }
    finally:
        await world.close()


async def test_funds_working_capital_decrease_presented_as_source(monkeypatch) -> None:
    entries = [
        ("expense_paid", "posted", "2026-01-02T00:00:00+00:00", [
            {"source": "1000", "destination": "6000", "amount": "100.00", "asset": GBP},
        ], {}),
    ]
    world = await make_world(monkeypatch, entries=entries)
    try:
        report = await generate_funds(
            world.db, "main", world.period_start, world.period_end
        )
        sources = {line["code"]: line["amount"] for line in report["sources"]}
        assert sources["PROFIT"] == -10000
        assert sources["WC"] == 10000
        assert report["totals"]["sources"] == report["totals"]["applications"] == 0
        assert report["working_capital_split"]["net"] == -10000
    finally:
        await world.close()


async def test_funds_identity_violation_raises(monkeypatch) -> None:
    entries = [
        ("equity_direct", "posted", "2026-01-02T00:00:00+00:00", [
            {"source": "3020", "destination": "1000", "amount": "50.00", "asset": GBP},
        ], {}),
    ]
    world = await make_world(monkeypatch, entries=entries)
    try:
        with pytest.raises(ReportEngineError, match="funds identity violated"):
            await generate_funds(world.db, "main", world.period_start, world.period_end)
    finally:
        await world.close()


async def test_unknown_code_raises_for_all_flow_reports(monkeypatch) -> None:
    entries = [
        ("bad", "posted", "2026-01-02T00:00:00+00:00", [
            {"source": "4000", "destination": "9999", "amount": "10.00", "asset": GBP},
        ], {}),
    ]
    world = await make_world(monkeypatch, entries=entries)
    try:
        for generate in (generate_pl, generate_cash_flow, generate_funds):
            with pytest.raises(ReportEngineError, match="9999"):
                await generate(world.db, "main", world.period_start, world.period_end)
    finally:
        await world.close()


# ---------------------------------------------------------------------------
# verify_against_postgres — cross-check against the Postgres ReportService
# ---------------------------------------------------------------------------

VERIFY_ENTRIES = [
    ("sale", "posted", "2026-01-02T00:00:00+00:00", [
        {"source": "4000", "destination": "1100", "amount": "1000.00", "asset": GBP},
    ], {}),
    ("costs", "posted", "2026-01-03T00:00:00+00:00", [
        {"source": "1000", "destination": "5000", "amount": "900.00", "asset": GBP},
    ], {}),
    ("depreciation", "posted", "2026-01-04T00:00:00+00:00", [
        {"source": "1360", "destination": "6500", "amount": "100.00", "asset": GBP},
    ], {}),
]

VERIFY_BALANCES = {
    "1100": "1000.00",
    "1000": "-900.00",
    "1360": "-100.00",
    "4000": "-1000.00",
    "5000": "900.00",
    "6500": "100.00",
}


async def _verify_world(monkeypatch, *, entries=None, balances=None) -> World:
    """Mirror the flows into real Posting rows so the Postgres ReportService
    sees the same ledger the scripted Formance serves."""
    world = await make_world(
        monkeypatch,
        entries=entries if entries is not None else VERIFY_ENTRIES,
        balances=balances if balances is not None else VERIFY_BALANCES,
    )
    result = await world.db.execute(select(Account))
    accounts = {a.code: a for a in result.scalars().all()}
    for key, _, _, postings, _ in (
        entries if entries is not None else VERIFY_ENTRIES
    ):
        txid = world.ids[key]
        for posting in postings:
            amount = int(Decimal(posting["amount"]) * 100)
            world.db.add(
                Posting(
                    transaction_id=txid,
                    account_id=accounts[posting["destination"]].id,
                    debit_amount=amount,
                    credit_amount=0,
                )
            )
            world.db.add(
                Posting(
                    transaction_id=txid,
                    account_id=accounts[posting["source"]].id,
                    debit_amount=0,
                    credit_amount=amount,
                )
            )
    await world.db.commit()
    return world


async def test_verify_against_postgres_empty_when_matching(monkeypatch) -> None:
    world = await _verify_world(monkeypatch)
    try:
        pl = await generate_pl(world.db, "main", world.period_start, world.period_end)
        assert await verify_against_postgres(world.db, pl, "pl") == []

        bs = await generate_bs(world.db, "main", as_at=world.period_end)
        assert await verify_against_postgres(world.db, bs, "bs") == []

        tb = await generate_tb(world.db, "main", as_at=world.period_end)
        assert await verify_against_postgres(world.db, tb, "tb") == []
    finally:
        await world.close()


async def test_verify_against_postgres_detects_drift(monkeypatch) -> None:
    world = await _verify_world(monkeypatch)
    try:
        # Sabotage the sale mirror: £1,000.00 -> £1,001.00.
        world.fake.transactions[0]["postings"][0]["amount"] = "1001.00"

        pl = await generate_pl(world.db, "main", world.period_start, world.period_end)
        diffs = await verify_against_postgres(world.db, pl, "pl")
        assert diffs
        lines = {d["line"]: d for d in diffs}
        assert lines["revenue"]["engine"] == 100100
        assert lines["revenue"]["postgres"] == 100000
        assert lines["net_profit"]["delta"] == 100
    finally:
        await world.close()


async def test_verify_cash_flow_funds_have_no_postgres_equivalent(monkeypatch) -> None:
    world = await _verify_world(monkeypatch)
    try:
        cf = await generate_cash_flow(
            world.db, "main", world.period_start, world.period_end
        )
        assert await verify_against_postgres(world.db, cf, "cash_flow") == []
        funds = await generate_funds(
            world.db, "main", world.period_start, world.period_end
        )
        assert await verify_against_postgres(world.db, funds, "funds") == []
    finally:
        await world.close()


# ---------------------------------------------------------------------------
# Static binding — engine tables must match the F8 SKILL.md Construction
# rules (the catalog stays the spec).
# ---------------------------------------------------------------------------

SECTION_RE = re.compile(
    r"^## Construction rules\s*$(.*?)(?=^## |\Z)", re.MULTILINE | re.DOTALL
)
CODE_RE = re.compile(r"`(\d{4})`")
RANGE_RE = re.compile(r"`(\d{4})`\s*[–—-]\s*`(\d{4})`")
CLASS_CELL_RE = re.compile(r"`([A-Z][A-Za-z]+)\s+\(([A-Za-z]+)\)`")

REPORT_IDS = (
    "profit-and-loss",
    "balance-sheet",
    "trial-balance",
    "cash-flow-statement",
    "funds-statement",
)


def _construction_text(report_id: str) -> str:
    text = (REPORTS_DIR / report_id / "SKILL.md").read_text(encoding="utf-8")
    match = SECTION_RE.search(text)
    assert match is not None, f"{report_id}: missing Construction rules section"
    return match.group(1)


def _template_codes() -> set[str]:
    codes: set[str] = set()
    for path in COA_DIR.glob("*.json"):
        for account in json.loads(path.read_text(encoding="utf-8")):
            codes.add(str(account["code"]))
    return codes


def _parsed_codes(report_id: str) -> set[str]:
    text = _construction_text(report_id)
    codes: set[str] = set()
    template_codes = _template_codes()
    # Ranges like `1300`–`1350` expand to the COA template's codes within
    # the range (the templates use the intervals-of-10 convention).
    for match in RANGE_RE.finditer(text):
        start, end = int(match.group(1)), int(match.group(2))
        expansion = {c for c in template_codes if start <= int(c) <= end}
        codes.update(expansion or {str(start), str(end)})
    codes.update(CODE_RE.findall(text))
    return codes


def _parsed_classes(report_id: str) -> set[str]:
    text = _construction_text(report_id)
    return {f"{m.group(1)} ({m.group(2)})" for m in CLASS_CELL_RE.finditer(text)}


def _engine_codes(report_id: str) -> set[str]:
    return {
        code
        for group in LINE_ITEM_CODES[report_id].values()
        for code in group
    }


def test_static_line_item_code_tables_keyed_by_skill_id() -> None:
    assert set(LINE_ITEM_CODES) == set(REPORT_IDS)
    for report_id in REPORT_IDS:
        assert isinstance(LINE_ITEM_CODES[report_id], dict)


def test_static_binding_line_item_codes_match_construction_rules() -> None:
    # Codes the SKILL mentions in identity/verification prose but which are
    # not line items themselves.
    context_codes = {
        "profit-and-loss": {"3020", "3030"},
        "cash-flow-statement": {"3020"},
    }
    # Codes the engine must additionally cover so the identities hold for
    # any client ledger (F8 lists the common cases only).
    allowed_extra = {
        "cash-flow-statement": {"1120", "1130", "1200", "1210", "2110"},
        "funds-statement": {"2110", "2200", "2230"},
    }
    for report_id in (
        "profit-and-loss",
        "balance-sheet",
        "cash-flow-statement",
        "funds-statement",
    ):
        engine = _engine_codes(report_id)
        parsed = _parsed_codes(report_id)
        ctx = context_codes.get(report_id, set())
        extra = allowed_extra.get(report_id, set())
        uncovered = (parsed - ctx) - engine
        assert not uncovered, (
            f"{report_id}: SKILL.md Construction rule codes not covered by "
            f"the engine: {uncovered}"
        )
        only_engine = engine - parsed
        assert only_engine <= extra, (
            f"{report_id}: unexpected engine-only codes {only_engine} "
            f"(allowed extras: {extra})"
        )


def test_static_binding_classes_match_construction_rules() -> None:
    for report_id in ("profit-and-loss", "balance-sheet", "trial-balance"):
        engine = {
            cls
            for group in LINE_ITEM_CLASSES[report_id].values()
            for cls in group
        }
        parsed = _parsed_classes(report_id)
        assert parsed == engine, (
            f"{report_id}: engine classes {engine} must match the SKILL.md "
            f"Construction rules classes {parsed}"
        )


# ---------------------------------------------------------------------------
# Static binding at GROUP level — each Construction-rules bullet that
# enumerates codes per line item must equal (or be a documented superset
# of) the engine's per-group table. Moving a code between groups — the
# 6310 overheads -> direct_costs mutation — fails this test.
# ---------------------------------------------------------------------------

# report -> group -> tuple of (anchor, keep, slice_end, take_before)
# specs. For every bullet segment containing ``anchor``, the window after
# (or, when ``take_before``, before) the anchor — truncated at
# ``slice_end`` — is scanned for 4-digit codes; ``keep`` restricts the
# codes kept from that window.
GROUP_BINDINGS = {
    "profit-and-loss": {
        "revenue": (("Revenue —", None, None, False),),
        "direct_costs": (("Direct costs —", None, None, False),),
        "overheads": (("Overheads —", None, None, False),),
        "staff_costs": (("Staff costs —", None, None, False),),
        "depreciation": (("Depreciation —", None, None, False),),
        "finance_costs": (("Finance costs —", None, None, False),),
        "corporation_tax": (("Corporation tax —", None, None, False),),
    },
    "balance-sheet": {
        "bank": (("`Asset (Bank)`: ", None, None, False),),
        "current_assets": (("`Asset (CurrentAsset)`: ", None, None, False),),
        "fixed_assets": (("`Asset (FixedAsset)` at cost: ", None, None, False),),
        "accumulated_depreciation": (("Contra-asset: ", None, None, False),),
        "current_liabilities": (
            ("`Liability (CurrentLiability)`: ", None, None, False),
        ),
        "vat": (("VAT net: ", None, None, False),),
        "long_term_liabilities": (
            ("`Liability (LongTermLiability)`: ", None, None, False),
        ),
        "equity": (
            ("`Equity (Equity)`: ", None, None, False),
            ("must equal cumulative profit", None, None, True),
        ),
        "contra_equity": (
            ("is contra-equity", frozenset({"3030"}), None, True),
        ),
    },
    "cash-flow-statement": {
        "depreciation_addback": (("Add back non-cash:", None, None, False),),
        "working_capital_assets": (
            ("Δ`1100`", None, None, False),
            ("Δ`1110`", None, None, False),
        ),
        "working_capital_liabilities": (
            ("Δ`2000`", None, None, False),
            ("Δ`2010`", None, None, False),
            ("Δ`2020`", None, None, False),
            ("Δ`2040`", None, None, False),
            ("Δ`2030`", None, None, False),
        ),
        "vat": (("VAT (`2210`", None, None, False),),
        "fixed_asset_purchases": (("4. Investing:", None, None, False),),
        "borrowings": (("5. Financing:", frozenset({"2100"}), None, False),),
        "capital_introduced": (("5. Financing:", frozenset({"3000"}), None, False),),
        "dividends_drawings": (
            ("5. Financing:", frozenset({"3010", "3030"}), None, False),
        ),
        "bank": (("6. Reconciliation:", None, None, False),),
    },
    "funds-statement": {
        "depreciation": (("`6500` Depreciation", None, None, False),),
        "borrowings": (("long-term borrowings:", None, None, False),),
        "capital_introduced": (("capital introduced:", None, None, False),),
        "fixed_asset_purchases": (("fixed asset purchases:", None, None, False),),
        "loan_repayments": (("loan repayments:", None, None, False),),
        "dividends_drawings": (("drawings / dividends:", None, None, False),),
        "working_capital_assets": (
            ("current assets are ", None, " and current liabilities are", False),
        ),
        "working_capital_liabilities": (
            ("current liabilities are ", None, None, False),
        ),
    },
}

# Codes that appear in a group's bullet as cross-references (e.g. the 3020
# equity leg of the tax provision) rather than line items of that group.
GROUP_CONTEXT = {
    "profit-and-loss": {"corporation_tax": {"3020"}},
    "cash-flow-statement": {"working_capital_liabilities": {"3020"}},
    "balance-sheet": {"vat": {"1130"}},
}

# Engine-only codes per group that the SKILL bullet does not enumerate but
# the engine must cover so the identities hold for any client ledger.
GROUP_ALLOWED_EXTRA = {
    "cash-flow-statement": {"borrowings": {"2110"}},
    "funds-statement": {
        "borrowings": {"2110"},
        "loan_repayments": {"2110"},
        "working_capital_liabilities": {"2200", "2230"},
    },
}


def _bullet_segments(text: str) -> list[str]:
    """Split Construction-rules text into bullet segments; wrapped
    continuation lines are joined onto their bullet."""
    segments: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        if re.match(r"^\s*-\s", line):
            if current:
                segments.append(" ".join(current))
            current = [line.strip()]
        elif line.strip():
            if current:
                current.append(line.strip())
        elif current:
            segments.append(" ".join(current))
            current = []
    if current:
        segments.append(" ".join(current))
    return segments


def _codes_in_window(window: str) -> set[str]:
    codes = set(CODE_RE.findall(window))
    template_codes = _template_codes()
    for match in RANGE_RE.finditer(window):
        start, end = int(match.group(1)), int(match.group(2))
        expansion = {c for c in template_codes if start <= int(c) <= end}
        codes.update(expansion or {str(start), str(end)})
    return codes


def _parsed_group_codes(report_id: str, group: str) -> set[str]:
    text = _construction_text(report_id)
    segments = _bullet_segments(text)
    parsed: set[str] = set()
    for anchor, keep, slice_end, take_before in GROUP_BINDINGS[report_id][group]:
        for segment in segments:
            idx = segment.find(anchor)
            if idx < 0:
                continue
            if take_before:
                window = segment[:idx]
            else:
                # Scan from the anchor start so codes embedded in the
                # anchor itself (e.g. Δ`1100`) are captured.
                window = segment[idx:]
                if slice_end is not None:
                    end_idx = window.find(slice_end)
                    if end_idx >= 0:
                        window = window[:end_idx]
            codes = _codes_in_window(window)
            if keep is not None:
                codes = {c for c in codes if c in keep}
            parsed |= codes
    return parsed - GROUP_CONTEXT.get(report_id, {}).get(group, set())


def test_static_binding_line_item_groups_match_construction_rules() -> None:
    """Bind at GROUP level: the engine's per-group code table must equal —
    or be a documented superset of — the codes its SKILL.md Construction
    bullet enumerates. Moving a code between groups (6310 overheads ->
    direct_costs) breaks this even though the per-report union is intact."""
    for report_id, groups in GROUP_BINDINGS.items():
        engine = LINE_ITEM_CODES[report_id]
        for group in groups:
            assert group in engine, f"{report_id}: engine has no {group!r} group"
            parsed = _parsed_group_codes(report_id, group)
            engine_codes = set(engine[group])
            missing = parsed - engine_codes
            assert not missing, (
                f"{report_id}/{group}: SKILL.md codes missing from the engine "
                f"group: {sorted(missing)}"
            )
            extra = engine_codes - parsed
            allowed = GROUP_ALLOWED_EXTRA.get(report_id, {}).get(group, set())
            assert extra <= allowed, (
                f"{report_id}/{group}: undocumented engine-only codes "
                f"{sorted(extra)} (documented extras: {sorted(allowed)})"
            )


# ---------------------------------------------------------------------------
# verify_against_postgres — entity scope pass-through (Contract F20)
# ---------------------------------------------------------------------------


class _ScopeProbe:
    """Stub ReportService output — subtotals/profits as plain ints."""

    def __init__(self, **values) -> None:
        self._values = values

    def __getattr__(self, name):
        if name in self._values:
            return self._values[name]
        raise AttributeError(name)


async def test_verify_against_postgres_passes_entity_scope_to_postgres_engine(
    monkeypatch,
) -> None:
    """Every cross-check call forwards the resolved entity_id to the
    Postgres ReportService (and stamps the ReportRunRequest) — the two
    engines read the SAME entity's rows."""
    from unittest.mock import AsyncMock, patch

    entity_id = uuid.uuid4()
    db = AsyncMock()

    class _Subtotal(_ScopeProbe):
        subtotal = 0

    pg_pl = _ScopeProbe(
        revenue=_Subtotal(),
        direct_costs=_Subtotal(),
        expenses=_Subtotal(),
        net_profit=0,
    )
    pg_bs = _ScopeProbe(
        current_assets=_Subtotal(),
        fixed_assets=_Subtotal(),
        current_liabilities=_Subtotal(),
        long_term_liabilities=_Subtotal(),
        equity=_Subtotal(),
        total_assets=0,
        total_liabilities=0,
    )
    pg_tb = _ScopeProbe(accounts=[])

    pl = {
        "report": "profit_and_loss",
        "period": {"start": date(2026, 1, 1), "end": date(2026, 12, 31)},
        "totals": {"revenue": 0, "direct_costs": 0, "expenses": 0},
        "profit": 0,
    }
    bs = {
        "report": "balance_sheet",
        "as_at": date(2026, 12, 31),
        "sections": {
            "bank": [],
            "current_assets": [],
            "fixed_assets": [],
            "accumulated_depreciation": [],
            "current_liabilities": [],
            "long_term_liabilities": [],
            "equity": [],
        },
        "totals": {"assets": 0, "liabilities": 0},
    }
    tb = {
        "report": "trial_balance",
        "as_at": date(2026, 12, 31),
        "accounts": [],
    }

    with (
        patch(
            "src.services.report_service.ReportService.run_pl",
            new_callable=AsyncMock,
            return_value=pg_pl,
        ) as run_pl,
        patch(
            "src.services.report_service.ReportService.run_bs",
            new_callable=AsyncMock,
            return_value=pg_bs,
        ) as run_bs,
        patch(
            "src.services.report_service.ReportService.run_tb",
            new_callable=AsyncMock,
            return_value=pg_tb,
        ) as run_tb,
    ):
        assert await verify_against_postgres(db, pl, "pl", entity_id=entity_id) == []
        assert await verify_against_postgres(db, bs, "bs", entity_id=entity_id) == []
        assert await verify_against_postgres(db, tb, "tb", entity_id=entity_id) == []

    run_pl.assert_awaited_once()
    assert run_pl.await_args.kwargs["entity_id"] == entity_id
    assert run_pl.await_args.args[1].entity_id == entity_id
    run_bs.assert_awaited_once()
    assert run_bs.await_args.kwargs["entity_id"] == entity_id
    run_tb.assert_awaited_once()
    assert run_tb.await_args.kwargs["entity_id"] == entity_id


async def test_verify_against_postgres_null_scope_keeps_legacy_engine(monkeypatch) -> None:
    """entity_id=None keeps the legacy global cross-check — the Postgres
    engine is called WITHOUT an entity scope."""
    from unittest.mock import AsyncMock, patch

    db = AsyncMock()

    class _Subtotal(_ScopeProbe):
        subtotal = 0

    pg_tb = _ScopeProbe(accounts=[])
    tb = {
        "report": "trial_balance",
        "as_at": date(2026, 12, 31),
        "accounts": [],
    }

    with patch(
        "src.services.report_service.ReportService.run_tb",
        new_callable=AsyncMock,
        return_value=pg_tb,
    ) as run_tb:
        assert await verify_against_postgres(db, tb, "tb") == []

    run_tb.assert_awaited_once()
    assert run_tb.await_args.kwargs["entity_id"] is None
    assert run_tb.await_args.args[1].entity_id is None
