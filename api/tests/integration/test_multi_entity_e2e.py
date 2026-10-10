"""Contract F18 end-to-end — two entities, two COAs, two ledgers, full isolation.

Drives EntityService + SetupService + CoaService + TransactionService +
BankService through a real SQLAlchemy async session against in-memory
SQLite (the F11/F12 DDL + ``gen_random_uuid`` shim pattern, with the
``entity_id`` columns declared).  A scripted fake Formance records every
mirror INCLUDING THE LEDGER NAME it was written to, and serves the
recorded mirrors back through ``list_transactions`` / ``get_balances``
(ledger-agnostic, the F11 pattern — per-ledger isolation is proven by the
recorder's captured ledger names; ``verify_parity`` itself becomes
entity-aware in F19).

Flow:
  create user → two entities (limited company + sole trader) → setup each
  with its own template/bank/opening balance → each COA loaded with the
  SAME codes but OWN rows (65 vs 55) → scoped listings see only their
  entity → each opening draft stamped with its entity → posting each draft
  WITHOUT a ledger arg mirrors to the TRANSACTION's own entity ledger
  (never "main", never the other entity's) → per-ledger parity ok → cross
  entity lookups return only the matching entity's rows → a reversal
  inherits the original's entity and ledger.  A second test re-runs the
  legacy NULL-scope flow (no entity_id anywhere) and asserts the old
  global behavior is untouched even with entities present.
"""

from __future__ import annotations

import copy
import json
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.config.database import Base
from src.models.account import Account
from src.models.bank_account import BankAccount
from src.models.transaction import Transaction
from src.models.user import User
from src.routes.orchestrator import (
    TransactionRef,
    approve as approve_route,
    pending as pending_route,
    reject as reject_route,
)
from src.routes.formance_reports import formance_report as formance_report_route
from src.services.bank_service import BankService
from src.services.coa_service import CoaService
from src.services.entity_service import (
    AmbiguousEntityError,
    EntityService,
    get_active_entity,
    get_active_ledger,
    get_ledger_for_entity,
)
from src.services.formance_client import formance_client
from src.services.formance_reports import _load_coa, generate_pl
from src.services.ledger_parity import verify_parity
from src.services.orchestrator import approve_entry
from src.services.setup_service import setup_entity
from src.services.transaction_service import (
    EntityMismatchError,
    TransactionService,
)
from src.validators.bank import BankAccountCreate
from src.validators.entity import (
    EntityCreate,
    EntityUpdate,
    SetupBankAccount,
    SetupOpeningBalanceItem,
    SetupOpeningBalances,
)
from src.validators.transaction import PostingCreate, TransactionCreate

_SQLITE_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS users (
        id CHAR(36) PRIMARY KEY NOT NULL DEFAULT (gen_random_uuid()),
        email VARCHAR(255) NOT NULL UNIQUE,
        hashed_password VARCHAR(255) NOT NULL,
        display_name VARCHAR(255) NOT NULL,
        role VARCHAR(20) NOT NULL DEFAULT 'viewer',
        is_active BOOLEAN NOT NULL DEFAULT 1,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
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
        created_by CHAR(36) NOT NULL REFERENCES users(id),
        is_active BOOLEAN NOT NULL DEFAULT 1,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS accounts (
        id CHAR(36) PRIMARY KEY NOT NULL DEFAULT (gen_random_uuid()),
        entity_id CHAR(36) REFERENCES entities(id) ON DELETE SET NULL,
        code VARCHAR(10) NOT NULL,
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
    # F18: a code is unique WITHIN an entity; legacy (NULL-scope) codes
    # stay globally unique — mirrors migration 017's partial indexes.
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_accounts_entity_code
        ON accounts (entity_id, code) WHERE entity_id IS NOT NULL
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_accounts_legacy_code
        ON accounts (code) WHERE entity_id IS NULL
    """,
    """
    CREATE TABLE IF NOT EXISTS transactions (
        id CHAR(36) PRIMARY KEY NOT NULL DEFAULT (gen_random_uuid()),
        entity_id CHAR(36) REFERENCES entities(id) ON DELETE SET NULL,
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
    """
    CREATE TABLE IF NOT EXISTS bank_accounts (
        id CHAR(36) PRIMARY KEY NOT NULL DEFAULT (gen_random_uuid()),
        entity_id CHAR(36) REFERENCES entities(id) ON DELETE SET NULL,
        name VARCHAR(255) NOT NULL,
        sort_code VARCHAR(10),
        account_number VARCHAR(20),
        iban VARCHAR(34),
        currency VARCHAR(3) NOT NULL DEFAULT 'GBP',
        opening_balance INTEGER NOT NULL DEFAULT 0,
        current_balance INTEGER NOT NULL DEFAULT 0,
        is_active BOOLEAN NOT NULL DEFAULT 1,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS bank_transactions (
        id CHAR(36) PRIMARY KEY NOT NULL DEFAULT (gen_random_uuid()),
        bank_account_id CHAR(36) NOT NULL REFERENCES bank_accounts(id) ON DELETE CASCADE,
        date DATE NOT NULL,
        description TEXT NOT NULL,
        amount INTEGER NOT NULL,
        reference VARCHAR(255),
        type VARCHAR(50),
        fitid VARCHAR(255),
        import_hash VARCHAR(64),
        status VARCHAR(20) NOT NULL DEFAULT 'imported',
        matched_transaction_id CHAR(36),
        contact_id CHAR(36),
        category VARCHAR(100),
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
]


class FakeFormance:
    """Scripted Formance ledger: records mirrors (incl. the LEDGER NAME),
    serves them back ledger-agnostically (the F11 e2e pattern)."""

    def __init__(self) -> None:
        self.transactions: list[dict] = []

    async def create_transaction(self, **kwargs) -> dict:
        txid = len(self.transactions) + 1
        self.transactions.append(
            {
                "txid": txid,
                "ledger": kwargs.get("ledger"),
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


class LedgerAwareFormance(FakeFormance):
    """F19 scripted Formance: per-ledger isolation — lists and balances are
    served for the REQUESTED ledger only (mirrors of other ledgers are
    invisible), so scoped parity proves real per-entity isolation."""

    def _for(self, ledger: str) -> list[dict]:
        return [t for t in self.transactions if t["ledger"] == ledger]

    async def list_transactions(self, ledger, page_size=None, after=None) -> dict:
        return {"cursor": {"hasMore": False}, "data": copy.deepcopy(self._for(ledger))}

    async def get_balances(self, ledger, address=None) -> dict:
        nets: dict[str, dict[str, Decimal]] = {}
        for tx in self._for(ledger):
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


def _make_session_factory():
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)

    @event.listens_for(engine.sync_engine, "connect")
    def _register_gen_random_uuid(dbapi_conn, _record) -> None:
        dbapi_conn.create_function("gen_random_uuid", 0, lambda: uuid.uuid4().hex)

    return engine


def _opening(amount_pence: int, day: int = 6) -> SetupOpeningBalances:
    return SetupOpeningBalances(
        opening_date=date(2026, 4, day),
        equity_account_code="3000",
        assets=[
            SetupOpeningBalanceItem(
                code="1000",
                name="Bank Current Account",
                balance_pence=amount_pence,
            )
        ],
    )


async def test_multi_entity_e2e(monkeypatch) -> None:
    """Two entities in one DB: own COAs, own banks, own drafts, own ledgers."""
    monkeypatch.setenv("FORNANCE_LEDGER_URL", "http://formance:3068")
    engine = _make_session_factory()

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
    try:
        now = datetime.now(UTC)
        owner = User(
            id=uuid.uuid4(),
            email="owner@multi.test",
            hashed_password="h",
            display_name="Owner",
            role="owner",
            is_active=True,
            created_at=now,
            updated_at=now,
        )
        db.add(owner)
        await db.commit()

        fake = FakeFormance()
        parity_client = MagicMock()
        parity_client.list_transactions = AsyncMock(
            side_effect=fake.list_transactions
        )
        parity_client.get_balances = AsyncMock(side_effect=fake.get_balances)
        with (
            patch.object(
                formance_client,
                "create_transaction",
                AsyncMock(side_effect=fake.create_transaction),
            ),
            patch.object(
                formance_client, "create_ledger", AsyncMock(return_value=True)
            ),
            patch.object(
                formance_client, "health_check", AsyncMock(return_value=True)
            ),
            patch("src.services.ledger_parity.formance_client", parity_client),
        ):
            # ---- two entities, both active ----
            entity_a = await EntityService.create_entity(
                db,
                EntityCreate(
                    name="Alpha Holdings Ltd",
                    legal_type="limited_company",
                    vat_registered=True,
                ),
                created_by=owner.id,
            )
            entity_b = await EntityService.create_entity(
                db,
                EntityCreate(
                    name="Beta Trading",
                    legal_type="sole_trader",
                    vat_registered=True,
                ),
                created_by=owner.id,
            )
            assert entity_a.ledger_name != entity_b.ledger_name

            # ---- setup entity A; post its draft and verify parity while
            #      A's COA is the ONLY one loaded (verify_parity becomes
            #      entity-aware in F19 — with two COAs its code map would
            #      collapse the shared codes) ----
            res_a = await setup_entity(
                db,
                entity_a.id,
                "uk_limited_company_vat",
                bank_accounts=[
                    SetupBankAccount(
                        name="Alpha Bank",
                        sort_code="20-00-00",
                        account_number="11111111",
                        opening_balance=1000,
                    )
                ],
                opening_balances=_opening(1000000),
            )
            assert res_a.coa_count == 65
            assert len(res_a.bank_account_ids) == 1
            assert len(res_a.opening_transaction_ids) == 1

            draft_a = await TransactionService.get_transaction(
                db, res_a.opening_transaction_ids[0]
            )
            assert draft_a is not None and draft_a.entity_id == entity_a.id

            drafts_a, _ = await TransactionService.list_transactions(
                db, status="draft", entity_id=entity_a.id
            )
            assert [t.id for t in drafts_a] == [draft_a.id]

            # ---- post WITHOUT a ledger arg: per-row resolution mirrors to
            #      the TRANSACTION's own entity ledger (never a global guess
            #      — get_active_ledger would say 'main' with 2 active) ----
            posted_a = await TransactionService.post_transaction(db, draft_a.id)
            assert posted_a.entity_id == entity_a.id
            assert fake.transactions[-1]["ledger"] == entity_a.ledger_name
            assert fake.transactions[-1]["ledger"] != "main"

            report_a = await verify_parity(db, ledger=entity_a.ledger_name)
            assert report_a.ok is True, report_a.summary

            # ---- setup entity B with its own template ----
            res_b = await setup_entity(
                db,
                entity_b.id,
                "uk_sole_trader_vat",
                bank_accounts=[
                    SetupBankAccount(
                        name="Beta Bank",
                        sort_code="30-00-00",
                        account_number="22222222",
                        opening_balance=2000,
                    )
                ],
                opening_balances=_opening(500000, day=7),
            )
            assert res_b.coa_count == 55
            assert len(res_b.bank_account_ids) == 1
            assert len(res_b.opening_transaction_ids) == 1

            # ---- scoped account counts; duplicate codes across entities ----
            global_count = (
                await db.execute(select(func.count()).select_from(Account))
            ).scalar_one()
            assert global_count == 65 + 55

            accounts_a = await CoaService.list_accounts(db, entity_id=entity_a.id)
            accounts_b = await CoaService.list_accounts(db, entity_id=entity_b.id)
            assert len(accounts_a) == 65
            assert len(accounts_b) == 55
            # legacy listing (no entity_id) still returns EVERY row — no filter
            legacy_accounts = await CoaService.list_accounts(db)
            assert len(legacy_accounts) == 120

            # the same code exists in BOTH entities but resolves per entity
            acc_a = await CoaService.get_account_by_code(
                db, "1000", entity_id=entity_a.id
            )
            acc_b = await CoaService.get_account_by_code(
                db, "1000", entity_id=entity_b.id
            )
            assert acc_a is not None and acc_b is not None
            assert acc_a.id != acc_b.id
            assert acc_a.code == acc_b.code == "1000"

            # ---- bank accounts are entity-scoped too ----
            banks_a = await BankService.list_accounts(db, entity_id=entity_a.id)
            banks_b = await BankService.list_accounts(db, entity_id=entity_b.id)
            assert [b.name for b in banks_a] == ["Alpha Bank"]
            assert [b.name for b in banks_b] == ["Beta Bank"]
            assert len(await BankService.list_accounts(db)) == 2

            # ---- opening drafts stamped with their entity ----
            draft_b = await TransactionService.get_transaction(
                db, res_b.opening_transaction_ids[0]
            )
            assert draft_b is not None and draft_b.entity_id == entity_b.id

            drafts_b, _ = await TransactionService.list_transactions(
                db, status="draft", entity_id=entity_b.id
            )
            assert [t.id for t in drafts_b] == [draft_b.id]
            # A is already posted; the legacy (unscoped) listing still sees
            # the entity-scoped draft — it never filters.
            legacy_drafts, _ = await TransactionService.list_transactions(
                db, status="draft"
            )
            assert [t.id for t in legacy_drafts] == [draft_b.id]

            # ---- two active entities: get_active_ledger is fail-loud
            #      (409) — never a silent 'main' (Contract F19) ----
            with pytest.raises(AmbiguousEntityError):
                await get_active_ledger(db)
            with pytest.raises(AmbiguousEntityError) as excinfo:
                await get_active_entity(db)
            assert excinfo.value.status_code == 409

            # ---- post B's draft WITHOUT a ledger arg: per-row resolution
            #      mirrors to B's own ledger (never A's, never 'main') ----
            posted_b = await TransactionService.post_transaction(db, draft_b.id)
            assert posted_b.entity_id == entity_b.id
            assert fake.transactions[-1]["ledger"] == entity_b.ledger_name
            assert fake.transactions[-1]["ledger"] != entity_a.ledger_name
            assert fake.transactions[-1]["ledger"] != "main"

            # ---- cross-entity transaction isolation ----
            rows_a, total_a = await TransactionService.list_transactions(
                db, entity_id=entity_a.id
            )
            rows_b, total_b = await TransactionService.list_transactions(
                db, entity_id=entity_b.id
            )
            assert {t.id for t in rows_a} == {posted_a.id}
            assert {t.id for t in rows_b} == {posted_b.id}
            assert all(t.entity_id == entity_a.id for t in rows_a)
            assert all(t.entity_id == entity_b.id for t in rows_b)

            # ---- a reversal inherits the original's entity and ledger ----
            reversing_b = await TransactionService.reverse_transaction(
                db, posted_b.id
            )
            assert reversing_b.entity_id == entity_b.id
            assert fake.transactions[-1]["ledger"] == entity_b.ledger_name

            # ---- explicit per-entity ledger resolution helpers ----
            assert await get_ledger_for_entity(db, entity_a.id) == entity_a.ledger_name
            assert await get_ledger_for_entity(db, entity_b.id) == entity_b.ledger_name
            assert await get_ledger_for_entity(db, uuid.uuid4()) == "main"
            assert await get_ledger_for_entity(db, None) == "main"

            # ---- deactivating B leaves a single active entity ----
            await EntityService.update_entity(
                db, entity_b.id, EntityUpdate(is_active=False)
            )
            active = await get_active_entity(db)
            assert active is not None and active.id == entity_a.id
            assert await get_active_ledger(db) == entity_a.ledger_name
    finally:
        await db.close()
        await engine.dispose()


async def test_entity_b_per_row_ledger_parity(monkeypatch) -> None:
    """A sole-trader entity's draft mirrors to ITS ledger via per-row
    resolution even when TWO active entities make get_active_ledger say
    'main' — and its ledger passes parity."""
    monkeypatch.setenv("FORNANCE_LEDGER_URL", "http://formance:3068")
    engine = _make_session_factory()

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
    try:
        now = datetime.now(UTC)
        owner = User(
            id=uuid.uuid4(),
            email="owner@beta.test",
            hashed_password="h",
            display_name="Owner",
            role="owner",
            is_active=True,
            created_at=now,
            updated_at=now,
        )
        db.add(owner)
        await db.commit()

        fake = FakeFormance()
        parity_client = MagicMock()
        parity_client.list_transactions = AsyncMock(
            side_effect=fake.list_transactions
        )
        parity_client.get_balances = AsyncMock(side_effect=fake.get_balances)
        with (
            patch.object(
                formance_client,
                "create_transaction",
                AsyncMock(side_effect=fake.create_transaction),
            ),
            patch.object(
                formance_client, "create_ledger", AsyncMock(return_value=True)
            ),
            patch.object(
                formance_client, "health_check", AsyncMock(return_value=True)
            ),
            patch("src.services.ledger_parity.formance_client", parity_client),
        ):
            # TWO active entities — get_active_ledger raises (409); the
            # per-row resolution never consults it.
            entity_b = await EntityService.create_entity(
                db,
                EntityCreate(name="Beta Trading", legal_type="sole_trader"),
                created_by=owner.id,
            )
            _other = await EntityService.create_entity(
                db,
                EntityCreate(name="Gamma Holdings Ltd"),
                created_by=owner.id,
            )
            with pytest.raises(AmbiguousEntityError):
                await get_active_ledger(db)

            res_b = await setup_entity(
                db,
                entity_b.id,
                "uk_sole_trader_vat",
                bank_accounts=[
                    SetupBankAccount(name="Beta Bank", opening_balance=2000)
                ],
                opening_balances=_opening(500000, day=7),
            )
            assert res_b.coa_count == 55

            draft_b = await TransactionService.get_transaction(
                db, res_b.opening_transaction_ids[0]
            )
            assert draft_b is not None and draft_b.entity_id == entity_b.id

            # Per-row resolution beats the global guess: the mirror goes to
            # B's ledger even though get_active_ledger says 'main'.
            posted_b = await TransactionService.post_transaction(db, draft_b.id)
            assert posted_b.entity_id == entity_b.id
            assert fake.transactions[-1]["ledger"] == entity_b.ledger_name
            assert fake.transactions[-1]["ledger"] != "main"

            report_b = await verify_parity(db, ledger=entity_b.ledger_name)
            assert report_b.ok is True, report_b.summary
    finally:
        await db.close()
        await engine.dispose()


async def test_null_scope_backward_compat_probe(monkeypatch) -> None:
    """The legacy NULL-scope flow behaves EXACTLY as before even with an
    active entity present: global COA, NULL stamps, mirror to 'main'."""
    monkeypatch.setenv("FORNANCE_LEDGER_URL", "http://formance:3068")
    engine = _make_session_factory()

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
    try:
        now = datetime.now(UTC)
        owner = User(
            id=uuid.uuid4(),
            email="owner@null.test",
            hashed_password="h",
            display_name="Owner",
            role="owner",
            is_active=True,
            created_at=now,
            updated_at=now,
        )
        db.add(owner)
        await db.commit()

        fake = FakeFormance()
        parity_client = MagicMock()
        parity_client.list_transactions = AsyncMock(
            side_effect=fake.list_transactions
        )
        parity_client.get_balances = AsyncMock(side_effect=fake.get_balances)
        with (
            patch.object(
                formance_client,
                "create_transaction",
                AsyncMock(side_effect=fake.create_transaction),
            ),
            patch.object(
                formance_client, "create_ledger", AsyncMock(return_value=True)
            ),
            patch.object(
                formance_client, "health_check", AsyncMock(return_value=True)
            ),
            patch("src.services.ledger_parity.formance_client", parity_client),
        ):
            # An ACTIVE entity exists — but this flow never passes entity_id.
            entity = await EntityService.create_entity(
                db,
                EntityCreate(name="Legacy Holdings Ltd"),
                created_by=owner.id,
            )

            # ---- NULL-scope COA load: global, all rows unstamped ----
            created = await CoaService.load_template(db, "uk_limited_company_vat")
            assert len(created) == 65
            all_accounts = (
                await db.execute(select(Account))
            ).scalars().all()
            assert len(all_accounts) == 65
            assert all(a.entity_id is None for a in all_accounts)

            # ---- NULL-scope bank account ----
            bank = await BankService.create_account(
                db,
                BankAccountCreate(
                    name="Legacy Bank",
                    opening_balance=1000,
                ),
            )
            assert bank.name == "Legacy Bank"
            bank_row = (
                await db.execute(select(BankAccount))
            ).scalars().all()
            assert all(b.entity_id is None for b in bank_row)

            # ---- NULL-scope transaction → mirror to "main", NOT the
            #      entity's ledger (NULL scope never consults entities) ----
            bank_acct = await CoaService.get_account_by_code(db, "1000")
            equity_acct = await CoaService.get_account_by_code(db, "3000")
            assert bank_acct is not None and equity_acct is not None

            tx = await TransactionService.create_transaction(
                db,
                TransactionCreate(
                    description="Legacy opening balances",
                    effective_date=date(2026, 4, 6),
                    idempotency_key=uuid.uuid4(),
                    postings=[
                        PostingCreate(
                            account_id=bank_acct.id,
                            debit_amount=1000000,
                            credit_amount=0,
                        ),
                        PostingCreate(
                            account_id=equity_acct.id,
                            debit_amount=0,
                            credit_amount=1000000,
                        ),
                    ],
                ),
            )
            assert tx.entity_id is None

            posted = await TransactionService.post_transaction(db, tx.id)
            assert posted.entity_id is None
            assert fake.transactions[-1]["ledger"] == "main"
            assert fake.transactions[-1]["ledger"] != entity.ledger_name

            # ---- parity on the legacy global ledger ----
            report = await verify_parity(db, ledger="main")
            assert report.ok is True, report.summary

            # ---- legacy global listings still see everything ----
            listed, total = await TransactionService.list_transactions(db)
            assert total == 1 and listed[0].id == tx.id

            # ---- single-entity resolution semantics unchanged ----
            assert await get_active_ledger(db) == entity.ledger_name
            active = await get_active_entity(db)
            assert active is not None and active.id == entity.id
    finally:
        await db.close()
        await engine.dispose()


# ---------------------------------------------------------------------------
# F19 — scoped approve / pending / reports / cross-entity refusal
# ---------------------------------------------------------------------------


async def _f19_session(engine) -> tuple[AsyncSession, User]:
    async def build() -> None:
        async with engine.begin() as conn:
            try:
                await conn.run_sync(Base.metadata.create_all)
            except SQLAlchemyError:
                for ddl in _SQLITE_SCHEMA:
                    await conn.execute(text(ddl))

    await build()
    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    db = session_factory()
    now = datetime.now(UTC)
    owner = User(
        id=uuid.uuid4(),
        email=f"owner-{uuid.uuid4().hex[:8]}@f19.test",
        hashed_password="h",
        display_name="Owner",
        role="owner",
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    db.add(owner)
    await db.commit()
    return db, owner


async def test_f19_approve_scoped_parity_and_pending_isolation(monkeypatch) -> None:
    """Two active entities: approve_entry follows the TRANSACTION's own
    entity (recorder captures A's ledger, never 'main'), the post-approve
    parity is scoped to A, B's pending list excludes A's drafts, and the
    ambiguous no-entity_id route calls return 409 — never a silent
    'main'."""
    monkeypatch.setenv("FORNANCE_LEDGER_URL", "http://formance:3068")
    engine = _make_session_factory()
    db, owner = await _f19_session(engine)
    try:
        fake = LedgerAwareFormance()
        parity_client = MagicMock()
        parity_client.list_transactions = AsyncMock(
            side_effect=fake.list_transactions
        )
        parity_client.get_balances = AsyncMock(side_effect=fake.get_balances)
        with (
            patch.object(
                formance_client,
                "create_transaction",
                AsyncMock(side_effect=fake.create_transaction),
            ),
            patch.object(
                formance_client, "create_ledger", AsyncMock(return_value=True)
            ),
            patch.object(
                formance_client, "health_check", AsyncMock(return_value=True)
            ),
            patch("src.services.ledger_parity.formance_client", parity_client),
        ):
            entity_a = await EntityService.create_entity(
                db,
                EntityCreate(name="Alpha Holdings Ltd"),
                created_by=owner.id,
            )
            entity_b = await EntityService.create_entity(
                db,
                EntityCreate(name="Beta Trading", legal_type="sole_trader"),
                created_by=owner.id,
            )
            res_a = await setup_entity(
                db, entity_a.id, "uk_limited_company_vat",
                opening_balances=_opening(1000000),
            )
            res_b = await setup_entity(
                db, entity_b.id, "uk_sole_trader_vat",
                opening_balances=_opening(500000, day=7),
            )
            draft_a = await TransactionService.get_transaction(
                db, res_a.opening_transaction_ids[0]
            )
            draft_b = await TransactionService.get_transaction(
                db, res_b.opening_transaction_ids[0]
            )

            # A's draft approved with NO explicit scope — the tx row decides:
            # A's ledger (never 'main') + A's scoped parity, all green.
            post_a = await approve_entry(db, draft_a.id)
            assert post_a.parity_ok is True, post_a.parity_report.summary
            assert fake.transactions[-1]["ledger"] == entity_a.ledger_name
            assert fake.transactions[-1]["ledger"] != "main"

            # B's pending list excludes A's drafts (entity-scoped route).
            pending_b = await pending_route(entity_id=entity_b.id, db=db)
            assert [t.id for t in pending_b["transactions"]] == [draft_b.id]

            # Ambiguous route calls (no entity_id, two active) -> 409.
            resp = await pending_route(entity_id=None, db=db)
            assert resp.status_code == 409
            assert json.loads(resp.body)["type"] == "ambiguous_entity"
            resp = await approve_route(
                TransactionRef(transaction_id=draft_b.id), entity_id=None, db=db
            )
            assert resp.status_code == 409
            assert json.loads(resp.body)["type"] == "ambiguous_entity"

            # An explicit scope approves B's draft to B's ledger + parity.
            resp = await approve_route(
                TransactionRef(transaction_id=draft_b.id),
                entity_id=entity_b.id,
                db=db,
            )
            assert resp["status"] == "posted"
            assert resp["parity_ok"] is True
            assert fake.transactions[-1]["ledger"] == entity_b.ledger_name
            assert fake.transactions[-1]["ledger"] != entity_a.ledger_name
    finally:
        await db.close()
        await engine.dispose()


async def test_f19_report_uses_entity_scoped_coa_no_code_collapse(
    monkeypatch,
) -> None:
    """Two COAs share codes; B's 4000 is sabotaged to a WRONG class.  A's
    entity-scoped report still classifies 4000 as Revenue (no code-map
    collapse) and the identity holds."""
    monkeypatch.setenv("FORNANCE_LEDGER_URL", "http://formance:3068")
    engine = _make_session_factory()
    db, owner = await _f19_session(engine)
    try:
        fake = LedgerAwareFormance()
        report_client = MagicMock()
        report_client.list_transactions = AsyncMock(
            side_effect=fake.list_transactions
        )
        report_client.get_balances = AsyncMock(side_effect=fake.get_balances)
        with (
            patch.object(
                formance_client,
                "create_transaction",
                AsyncMock(side_effect=fake.create_transaction),
            ),
            patch.object(
                formance_client, "create_ledger", AsyncMock(return_value=True)
            ),
            patch.object(
                formance_client, "health_check", AsyncMock(return_value=True)
            ),
            patch("src.services.formance_reports.formance_client", report_client),
        ):
            entity_a = await EntityService.create_entity(
                db,
                EntityCreate(name="Alpha Holdings Ltd"),
                created_by=owner.id,
            )
            entity_b = await EntityService.create_entity(
                db,
                EntityCreate(name="Beta Trading", legal_type="sole_trader"),
                created_by=owner.id,
            )
            await setup_entity(
                db, entity_a.id, "uk_limited_company_vat",
                opening_balances=_opening(1000000),
            )
            await setup_entity(
                db, entity_b.id, "uk_sole_trader_vat",
                opening_balances=_opening(500000, day=7),
            )

            # Sabotage B's 4000 row: same code, WRONG class.
            rogue = (
                await db.execute(
                    select(Account).where(
                        Account.entity_id == entity_b.id,
                        Account.code == "4000",
                    )
                )
            ).scalar_one()
            rogue.category = "Expense"
            rogue.type = "DirectCost"
            await db.commit()

            # A posts a sale: Dr bank / Cr sales.
            acc_bank = await CoaService.get_account_by_code(
                db, "1000", entity_id=entity_a.id
            )
            acc_sales = await CoaService.get_account_by_code(
                db, "4000", entity_id=entity_a.id
            )
            assert acc_bank is not None and acc_sales is not None
            tx = await TransactionService.create_transaction(
                db,
                TransactionCreate(
                    description="Cash sale",
                    effective_date=date(2026, 10, 1),
                    idempotency_key=uuid.uuid4(),
                    postings=[
                        PostingCreate(
                            account_id=acc_bank.id,
                            debit_amount=100000,
                            credit_amount=0,
                        ),
                        PostingCreate(
                            account_id=acc_sales.id,
                            debit_amount=0,
                            credit_amount=100000,
                        ),
                    ],
                ),
                entity_id=entity_a.id,
            )
            await TransactionService.post_transaction(db, tx.id)
            assert fake.transactions[-1]["ledger"] == entity_a.ledger_name

            # A's scoped classification: 4000 stays Revenue — B's rogue row
            # never collapses into A's code map.
            class_map, _name_map = await _load_coa(db, entity_id=entity_a.id)
            assert class_map["4000"] == "Revenue (Revenue)"

            report = await generate_pl(
                db,
                ledger=entity_a.ledger_name,
                period_start=date(2026, 1, 1),
                period_end=date(2026, 12, 31),
                entity_id=entity_a.id,
            )
            assert report["identity_ok"] is True
            revenue = {
                line["code"]: line["amount"]
                for line in report["sections"]["revenue"]
            }
            assert revenue.get("4000") == 100000
    finally:
        await db.close()
        await engine.dispose()


async def test_f19_cross_entity_posting_refused(monkeypatch) -> None:
    """A hostile cross-entity posting (A's transaction referencing B's
    account) is refused with EntityMismatchError (422) at the write path."""
    monkeypatch.setenv("FORNANCE_LEDGER_URL", "http://formance:3068")
    engine = _make_session_factory()
    db, owner = await _f19_session(engine)
    try:
        with (
            patch.object(
                formance_client, "create_ledger", AsyncMock(return_value=True)
            ),
            patch.object(
                formance_client, "health_check", AsyncMock(return_value=True)
            ),
        ):
            entity_a = await EntityService.create_entity(
                db,
                EntityCreate(name="Alpha Holdings Ltd"),
                created_by=owner.id,
            )
            entity_b = await EntityService.create_entity(
                db,
                EntityCreate(name="Beta Trading", legal_type="sole_trader"),
                created_by=owner.id,
            )
            await setup_entity(
                db, entity_a.id, "uk_limited_company_vat",
                opening_balances=_opening(1000000),
            )
            await setup_entity(
                db, entity_b.id, "uk_sole_trader_vat",
                opening_balances=_opening(500000, day=7),
            )

            acc_a = await CoaService.get_account_by_code(
                db, "1000", entity_id=entity_a.id
            )
            acc_b = await CoaService.get_account_by_code(
                db, "1000", entity_id=entity_b.id
            )
            assert acc_a is not None and acc_b is not None
            assert acc_a.id != acc_b.id

            with pytest.raises(EntityMismatchError) as excinfo:
                await TransactionService.create_transaction(
                    db,
                    TransactionCreate(
                        description="Hostile cross-entity entry",
                        effective_date=date(2026, 10, 1),
                        idempotency_key=uuid.uuid4(),
                        postings=[
                            PostingCreate(
                                account_id=acc_a.id,
                                debit_amount=100000,
                                credit_amount=0,
                            ),
                            PostingCreate(
                                account_id=acc_b.id,
                                debit_amount=0,
                                credit_amount=100000,
                            ),
                        ],
                    ),
                    entity_id=entity_a.id,
                )

            assert excinfo.value.status_code == 422
            assert excinfo.value.error_type == "entity_mismatch"
            assert str(acc_b.id) in excinfo.value.message
    finally:
        await db.close()
        await engine.dispose()


# ---------------------------------------------------------------------------
# F20 — entity-scoped cross-check (no false drift), reject ownership,
#       unknown entity_id 404
# ---------------------------------------------------------------------------


async def test_f20_report_route_cross_check_no_false_drift(monkeypatch) -> None:
    """F20: two entities BOTH with posted data — the report route for A
    scopes BOTH engines (the Postgres cross-check included) and returns
    EMPTY cross_check diffs (the F19 verifier's measured false-drift case:
    unscoped, B's posted opening balance leaked into A's cross-check)."""
    monkeypatch.setenv("FORNANCE_LEDGER_URL", "http://formance:3068")
    engine = _make_session_factory()
    db, owner = await _f19_session(engine)
    try:
        fake = LedgerAwareFormance()
        report_client = MagicMock()
        report_client.list_transactions = AsyncMock(
            side_effect=fake.list_transactions
        )
        report_client.get_balances = AsyncMock(side_effect=fake.get_balances)
        with (
            patch.object(
                formance_client,
                "create_transaction",
                AsyncMock(side_effect=fake.create_transaction),
            ),
            patch.object(
                formance_client, "create_ledger", AsyncMock(return_value=True)
            ),
            patch.object(
                formance_client, "health_check", AsyncMock(return_value=True)
            ),
            patch("src.services.formance_reports.formance_client", report_client),
        ):
            entity_a = await EntityService.create_entity(
                db,
                EntityCreate(name="Alpha Holdings Ltd"),
                created_by=owner.id,
            )
            entity_b = await EntityService.create_entity(
                db,
                EntityCreate(name="Beta Trading", legal_type="sole_trader"),
                created_by=owner.id,
            )
            res_a = await setup_entity(
                db, entity_a.id, "uk_limited_company_vat",
                opening_balances=_opening(1000000),
            )
            res_b = await setup_entity(
                db, entity_b.id, "uk_sole_trader_vat",
                opening_balances=_opening(500000, day=7),
            )
            draft_a = await TransactionService.get_transaction(
                db, res_a.opening_transaction_ids[0]
            )
            draft_b = await TransactionService.get_transaction(
                db, res_b.opening_transaction_ids[0]
            )
            await TransactionService.post_transaction(db, draft_a.id)
            await TransactionService.post_transaction(db, draft_b.id)

            # A's scoped report: both engines read ONLY A's rows — the
            # cross-check agrees exactly (previously: false drift from B).
            resp = await formance_report_route(
                report_type="balance-sheet",
                as_at=date(2026, 12, 31),
                entity_id=entity_a.id,
                db=db,
            )
            assert resp.status_code == 200, resp.body
            body = json.loads(resp.body)
            assert body["report"]["identity_ok"] is True
            assert body["cross_check"]["ok"] is True
            assert body["cross_check"]["diffs"] == []

            # B's scoped report agrees too.
            resp_b = await formance_report_route(
                report_type="balance-sheet",
                as_at=date(2026, 12, 31),
                entity_id=entity_b.id,
                db=db,
            )
            assert resp_b.status_code == 200, resp_b.body
            assert json.loads(resp_b.body)["cross_check"]["diffs"] == []

            # Unscoped with two active entities stays fail-loud (409).
            resp = await formance_report_route(
                report_type="balance-sheet",
                as_at=date(2026, 12, 31),
                entity_id=None,
                db=db,
            )
            assert resp.status_code == 409
            assert json.loads(resp.body)["type"] == "ambiguous_entity"
    finally:
        await db.close()
        await engine.dispose()


async def test_f20_reject_cross_entity_refused(monkeypatch) -> None:
    """F20: the reject route refuses a draft stamped with a DIFFERENT
    entity (404 entity_mismatch, nothing discarded) and discards it
    through its OWN entity scope."""
    monkeypatch.setenv("FORNANCE_LEDGER_URL", "http://formance:3068")
    engine = _make_session_factory()
    db, owner = await _f19_session(engine)
    try:
        with (
            patch.object(
                formance_client, "create_ledger", AsyncMock(return_value=True)
            ),
            patch.object(
                formance_client, "health_check", AsyncMock(return_value=True)
            ),
        ):
            entity_a = await EntityService.create_entity(
                db,
                EntityCreate(name="Alpha Holdings Ltd"),
                created_by=owner.id,
            )
            entity_b = await EntityService.create_entity(
                db,
                EntityCreate(name="Beta Trading", legal_type="sole_trader"),
                created_by=owner.id,
            )
            await setup_entity(
                db, entity_a.id, "uk_limited_company_vat",
                opening_balances=_opening(1000000),
            )
            res_b = await setup_entity(
                db, entity_b.id, "uk_sole_trader_vat",
                opening_balances=_opening(500000, day=7),
            )
            draft_b = await TransactionService.get_transaction(
                db, res_b.opening_transaction_ids[0]
            )
            assert draft_b is not None and draft_b.entity_id == entity_b.id

            # A's scope refuses B's draft — never a cross-entity discard.
            resp = await reject_route(
                TransactionRef(transaction_id=draft_b.id),
                entity_id=entity_a.id,
                db=db,
            )
            assert resp.status_code == 404
            body = json.loads(resp.body)
            assert body["type"] == "entity_mismatch"
            still = await TransactionService.get_transaction(db, draft_b.id)
            assert still is not None and still.status == "draft"

            # B's own scope discards it.
            resp = await reject_route(
                TransactionRef(transaction_id=draft_b.id),
                entity_id=entity_b.id,
                db=db,
            )
            assert resp["status"] == "discarded"
            assert resp["transaction_id"] == str(draft_b.id)
            assert await TransactionService.get_transaction(db, draft_b.id) is None
    finally:
        await db.close()
        await engine.dispose()


async def test_f20_unknown_entity_id_404(monkeypatch) -> None:
    """F20: an explicit entity_id that does not exist (or is inactive)
    gets 404 {type: entity_not_found} on the orchestrator + report routes
    — never a silent 'main' degradation."""
    monkeypatch.setenv("FORNANCE_LEDGER_URL", "http://formance:3068")
    engine = _make_session_factory()
    db, owner = await _f19_session(engine)
    try:
        with (
            patch.object(
                formance_client, "create_ledger", AsyncMock(return_value=True)
            ),
            patch.object(
                formance_client, "health_check", AsyncMock(return_value=True)
            ),
        ):
            entity_a = await EntityService.create_entity(
                db,
                EntityCreate(name="Alpha Holdings Ltd"),
                created_by=owner.id,
            )
            entity_b = await EntityService.create_entity(
                db,
                EntityCreate(name="Beta Trading", legal_type="sole_trader"),
                created_by=owner.id,
            )
            res_a = await setup_entity(
                db, entity_a.id, "uk_limited_company_vat",
                opening_balances=_opening(1000000),
            )
            draft_a = await TransactionService.get_transaction(
                db, res_a.opening_transaction_ids[0]
            )
            unknown = uuid.uuid4()

            resp = await pending_route(entity_id=unknown, db=db)
            assert resp.status_code == 404
            assert json.loads(resp.body)["type"] == "entity_not_found"

            resp = await reject_route(
                TransactionRef(transaction_id=draft_a.id),
                entity_id=unknown,
                db=db,
            )
            assert resp.status_code == 404
            assert json.loads(resp.body)["type"] == "entity_not_found"

            resp = await formance_report_route(
                report_type="trial-balance",
                as_at=date(2026, 12, 31),
                entity_id=unknown,
                db=db,
            )
            assert resp.status_code == 404
            assert json.loads(resp.body)["type"] == "entity_not_found"

            # An INACTIVE entity is refused the same way (exists != usable).
            await EntityService.update_entity(
                db, entity_b.id, EntityUpdate(is_active=False)
            )
            resp = await pending_route(entity_id=entity_b.id, db=db)
            assert resp.status_code == 404
            assert json.loads(resp.body)["type"] == "entity_not_found"
    finally:
        await db.close()
        await engine.dispose()
