"""Integration test: ledger parity verifier over a real async ORM (Contract F2).

Drives the real TransactionService against a real SQLAlchemy async session on
an in-memory SQLite database (same pattern as ``test_transaction_orm_reversal.py``
— the mapped models are PostgreSQL-native, so a hand-written SQLite schema is
used as fallback with a ``gen_random_uuid`` shim).  A scripted fake Formance
records every ``create_transaction`` mirror call made by the service and serves
those records back through patched ``list_transactions`` / ``get_balances``.

Phases:
  1. Perfect parity -> ok=True.
  2. Sabotage: drop one mirror -> missing entry (plus exact balance cascade).
  3. Sabotage: alter one mirror amount -> leg mismatch (plus balance cascade).
  4. Sabotage: inject an orphan row -> exactly one orphan entry.
"""

from __future__ import annotations

import copy
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import event, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.config.database import Base
from src.models.account import Account
from src.services.formance_client import formance_client
from src.services.ledger_parity import verify_parity
from src.services.transaction_service import TransactionService
from src.validators.transaction import PostingCreate, TransactionCreate

GBP = "GBP/2"

_SQLITE_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS accounts (
        id CHAR(36) PRIMARY KEY NOT NULL DEFAULT (gen_random_uuid()),
        code VARCHAR(10) NOT NULL UNIQUE,
        name VARCHAR(255) NOT NULL,
        category VARCHAR(20) NOT NULL,
        type VARCHAR(30) NOT NULL,
        parent_id CHAR(36),
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


class FakeFormance:
    """Scripted Formance ledger: records mirror calls, serves them back."""

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
                src[asset] = src.get(asset, Decimal("0")) - amount
                dst = nets.setdefault(posting["destination"], {})
                dst[asset] = dst.get(asset, Decimal("0")) + amount
        return {
            "data": [
                {"address": addr, "balances": {a: str(v) for a, v in assets.items()}}
                for addr, assets in nets.items()
            ]
        }

    # ---- sabotage helpers ------------------------------------------------

    def mirror_for(self, tx_id: str) -> dict:
        return next(
            t for t in self.transactions if t["metadata"]["transaction_id"] == str(tx_id)
        )

    def drop_transaction(self, txid: int) -> None:
        self.transactions = [t for t in self.transactions if t["txid"] != txid]

    def alter_amount(self, txid: int, new_amount: str) -> None:
        for t in self.transactions:
            if t["txid"] == txid:
                for p in t["postings"]:
                    p["amount"] = new_amount

    def add_orphan(self, *, txid, reference, metadata, postings) -> None:
        self.transactions.append(
            {
                "txid": txid,
                "reference": reference,
                "metadata": metadata,
                "postings": postings,
            }
        )


async def _build_scripted_ledger(monkeypatch) -> dict:
    """Create accounts + post 2 transactions + 1 reversal through real services."""
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
    fake = FakeFormance()
    db = session_factory()

    now = datetime.now(UTC)
    bank = Account(
        id=uuid.uuid4(), code="1000", name="Bank Current",
        category="Asset", type="Bank", is_active=True, created_at=now, updated_at=now,
    )
    revenue = Account(
        id=uuid.uuid4(), code="4000", name="Sales Revenue",
        category="Revenue", type="Revenue", is_active=True, created_at=now, updated_at=now,
    )
    expense = Account(
        id=uuid.uuid4(), code="5210", name="Marketing Expense",
        category="Expense", type="Expense", is_active=True, created_at=now, updated_at=now,
    )
    db.add_all([bank, revenue, expense])
    await db.commit()

    tx1 = await TransactionService.create_transaction(
        db,
        TransactionCreate(
            description="Sales invoice",
            idempotency_key=uuid.uuid4(),
            postings=[
                PostingCreate(account_id=bank.id, debit_amount=10000),
                PostingCreate(account_id=revenue.id, credit_amount=10000),
            ],
        ),
    )
    tx2 = await TransactionService.create_transaction(
        db,
        TransactionCreate(
            description="Marketing expense",
            idempotency_key=uuid.uuid4(),
            postings=[
                PostingCreate(account_id=expense.id, debit_amount=2500),
                PostingCreate(account_id=bank.id, credit_amount=2500),
            ],
        ),
    )
    # A draft that is never posted: must be invisible to parity on both the
    # transaction level and the balance level (£500 legs would blow up both).
    await TransactionService.create_transaction(
        db,
        TransactionCreate(
            description="Draft never posted",
            idempotency_key=uuid.uuid4(),
            postings=[
                PostingCreate(account_id=bank.id, debit_amount=50000),
                PostingCreate(account_id=revenue.id, credit_amount=50000),
            ],
        ),
    )

    with patch.object(
        formance_client, "create_transaction", AsyncMock(side_effect=fake.create_transaction)
    ):
        tx1 = await TransactionService.post_transaction(db, tx1.id)
        tx2 = await TransactionService.post_transaction(db, tx2.id)
        reversal = await TransactionService.reverse_transaction(db, tx1.id)

    assert len(fake.transactions) == 3
    return {
        "engine": engine,
        "session_factory": session_factory,
        "db": db,
        "fake": fake,
        "accounts": {"bank": bank, "revenue": revenue, "expense": expense},
        "transactions": {"tx1": tx1, "tx2": tx2, "reversal": reversal},
    }


async def _verify(db, fake):
    client = MagicMock()
    client.list_transactions = AsyncMock(side_effect=fake.list_transactions)
    client.get_balances = AsyncMock(side_effect=fake.get_balances)
    with patch("src.services.ledger_parity.formance_client", client):
        return await verify_parity(db)


# ---------------------------------------------------------------------------
# Phase 1: perfect parity
# ---------------------------------------------------------------------------


async def test_perfect_parity_with_scripted_formance(monkeypatch) -> None:
    ctx = await _build_scripted_ledger(monkeypatch)
    try:
        fake = ctx["fake"]
        report = await _verify(ctx["db"], fake)

        assert report.ok is True
        assert report.error is None
        assert report.missing_in_formance == []
        assert report.orphans_in_formance == []
        assert report.leg_mismatches == []
        assert report.balance_mismatches == []
        assert "OK" in report.summary

        # Spot-check the mirrors the service actually recorded: 1 reversal of
        # tx1, tx2 untouched, and the reversal's legs swapped.
        reversal = ctx["transactions"]["reversal"]
        tx1 = ctx["transactions"]["tx1"]
        assert fake.mirror_for(str(tx1.id))["postings"] == [
            {"source": "4000", "destination": "1000", "amount": "100.00", "asset": GBP}
        ]
        assert fake.mirror_for(str(reversal.id))["postings"] == [
            {"source": "1000", "destination": "4000", "amount": "100.00", "asset": GBP}
        ]
    finally:
        await ctx["db"].close()
        await ctx["engine"].dispose()


# ---------------------------------------------------------------------------
# Phase 2: drop a mirror
# ---------------------------------------------------------------------------


async def test_sabotage_drop_transaction_flagged(monkeypatch) -> None:
    ctx = await _build_scripted_ledger(monkeypatch)
    try:
        fake = ctx["fake"]
        tx2 = ctx["transactions"]["tx2"]
        fake.drop_transaction(fake.mirror_for(str(tx2.id))["txid"])

        report = await _verify(ctx["db"], fake)

        assert report.ok is False
        assert report.missing_in_formance == [
            {
                "kind": "missing_mirror",
                "transaction_id": str(tx2.id),
                "reference": tx2.reference,
                "expected": str(tx2.id),
                "actual": None,
            }
        ]
        assert report.orphans_in_formance == []
        assert report.leg_mismatches == []
        # Balance cascade: tx2's legs (Dr 5210 £25 / Cr 1000 £25) vanish.
        entries = {
            (e["account_code"], e["expected"], e["actual"])
            for e in report.balance_mismatches
        }
        assert entries == {("1000", -2500, 0), ("5210", 2500, 0)}
    finally:
        await ctx["db"].close()
        await ctx["engine"].dispose()


# ---------------------------------------------------------------------------
# Phase 3: alter a mirror amount
# ---------------------------------------------------------------------------


async def test_sabotage_alter_amount_flagged(monkeypatch) -> None:
    ctx = await _build_scripted_ledger(monkeypatch)
    try:
        fake = ctx["fake"]
        tx2 = ctx["transactions"]["tx2"]
        fake.alter_amount(fake.mirror_for(str(tx2.id))["txid"], "30.00")

        report = await _verify(ctx["db"], fake)

        assert report.ok is False
        assert report.missing_in_formance == []
        assert report.orphans_in_formance == []
        assert report.leg_mismatches == [
            {
                "kind": "postings",
                "transaction_id": str(tx2.id),
                "reference": tx2.reference,
                "expected": [
                    {"source": "1000", "destination": "5210", "amount": "25.00", "asset": GBP}
                ],
                "actual": [
                    {"source": "1000", "destination": "5210", "amount": "30.00", "asset": GBP}
                ],
            }
        ]
        # Balance cascade: £5 extra flows 1000 -> 5210.
        entries = {
            (e["account_code"], e["expected"], e["actual"])
            for e in report.balance_mismatches
        }
        assert entries == {("1000", -2500, -3000), ("5210", 2500, 3000)}
    finally:
        await ctx["db"].close()
        await ctx["engine"].dispose()


# ---------------------------------------------------------------------------
# Phase 4: inject an orphan row
# ---------------------------------------------------------------------------


async def test_sabotage_orphan_injection_flagged(monkeypatch) -> None:
    ctx = await _build_scripted_ledger(monkeypatch)
    try:
        fake = ctx["fake"]
        rogue_id = str(uuid.uuid4())
        fake.add_orphan(
            txid=99,
            reference="JE-1999-9999",
            metadata={"transaction_id": rogue_id, "description": "rogue row"},
            postings=[{"source": "9999", "destination": "8888", "amount": "1.00", "asset": GBP}],
        )

        report = await _verify(ctx["db"], fake)

        assert report.ok is False
        assert report.orphans_in_formance == [
            {
                "kind": "orphan",
                "transaction_id": rogue_id,
                "reference": "JE-1999-9999",
                "formance_txid": 99,
                "expected": None,
                "actual": {"txid": 99, "reference": "JE-1999-9999"},
            }
        ]
        assert report.missing_in_formance == []
        assert report.leg_mismatches == []
        assert report.balance_mismatches == []
    finally:
        await ctx["db"].close()
        await ctx["engine"].dispose()
