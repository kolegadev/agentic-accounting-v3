"""Contract F11 end-to-end — entity setup through the real async ORM.

Drives EntityService + SetupService + TransactionService through a real
SQLAlchemy async session against in-memory SQLite (the
``test_transaction_orm_reversal`` DDL + ``gen_random_uuid`` shim pattern,
extended with the users/entities/bank_accounts tables).  A scripted fake
Formance records every mirror and serves the recorded mirrors back through
``list_transactions`` / ``get_balances``.

Flow:
  create user (owner) → create entity → setup (uk_limited_company_vat +
  bank account + £10,000 opening capital) → COA loaded (65 accounts),
  balanced DRAFT legs (Dr 1000 1,000,000 / Cr 3000 1,000,000), no Formance
  mirror yet (drafts are never posted by setup) → double setup refused →
  approve via ``TransactionService.post_transaction`` (mirror recorded) →
  ``verify_parity`` ok → status snapshot shows posted + parity.
"""

from __future__ import annotations

import copy
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.config.database import Base
from src.models.user import User
from src.services.entity_service import EntityService
from src.services.formance_client import formance_client
from src.services.ledger_parity import verify_parity
from src.services.setup_service import (
    SetupError,
    get_entity_status,
    setup_entity,
)
from src.services.transaction_service import TransactionService
from src.validators.entity import (
    EntityCreate,
    SetupBankAccount,
    SetupOpeningBalanceItem,
    SetupOpeningBalances,
)

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
        created_by CHAR(36) NOT NULL REFERENCES users(id),
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
    # Posting.vat_lines is lazy="selectin" — loaded eagerly on refresh.
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
    # BankAccount.transactions is lazy="selectin" — refresh() loads it, so the
    # table must exist even though no bank transactions are seeded.
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
    """Scripted Formance ledger: records mirrors, serves them back.

    Balances are derived from the recorded postings (same pattern as
    test_formance_reports_e2e).
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


async def test_entity_setup_e2e(monkeypatch) -> None:
    """The full initial-ledger flow: entity → setup → draft → approve → parity."""
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
    try:
        now = datetime.now(UTC)
        owner = User(
            id=uuid.uuid4(),
            email="owner@acme.test",
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
        ):
            # ---- create entity (created_by = the admin owner) ----
            entity = await EntityService.create_entity(
                db,
                EntityCreate(
                    name="Acme Holdings Ltd",
                    legal_type="limited_company",
                    framework="uk-gaap",
                    vat_registered=True,
                    vat_scheme="standard",
                ),
                created_by=owner.id,
            )
            assert entity.created_by == owner.id
            assert entity.ledger_name == f"entity-{entity.id.hex[:8]}"

            # ---- setup: COA + bank account + £10,000 opening capital ----
            result = await setup_entity(
                db,
                entity.id,
                "uk_limited_company_vat",
                bank_accounts=[
                    SetupBankAccount(
                        name="Barclays Business Current",
                        sort_code="20-00-00",
                        account_number="12345678",
                        opening_balance=1000,
                    )
                ],
                opening_balances=SetupOpeningBalances(
                    opening_date=date(2026, 4, 6),
                    equity_account_code="3000",
                    assets=[
                        SetupOpeningBalanceItem(
                            code="1000",
                            name="Bank Current Account",
                            balance_pence=1000000,
                        )
                    ],
                ),
            )
            assert result.coa_count == 65
            assert result.ledger_name == entity.ledger_name
            assert len(result.bank_account_ids) == 1
            assert len(result.opening_transaction_ids) == 1
            assert result.opening_draft_total_pence == 1000000

            # ---- idempotency: a second setup is refused ----
            with pytest.raises(SetupError, match="already set up"):
                await setup_entity(db, entity.id, "uk_limited_company_vat")

            # ---- the opening entry is a balanced DRAFT ----
            draft = await TransactionService.get_transaction(
                db, result.opening_transaction_ids[0]
            )
            assert draft is not None
            assert draft.status == "draft"
            assert draft.reference is None
            assert draft.total_amount == 1000000
            legs = {
                (p.account.code, p.debit_amount, p.credit_amount)
                for p in draft.postings
            }
            assert legs == {
                ("1000", 1000000, 0),
                ("3000", 0, 1000000),
            }

            # ---- drafts are NEVER mirrored by setup ----
            assert fake.transactions == []

            # ---- approve via TransactionService.post_transaction ----
            posted = await TransactionService.post_transaction(db, draft.id)
            assert posted.status == "posted"
            assert posted.reference == "JE-2026-0001"
            assert len(fake.transactions) == 1

            # ---- parity: Formance is a faithful image of Postgres ----
            parity_client = MagicMock()
            parity_client.list_transactions = AsyncMock(
                side_effect=fake.list_transactions
            )
            parity_client.get_balances = AsyncMock(side_effect=fake.get_balances)
            with patch(
                "src.services.ledger_parity.formance_client", parity_client
            ):
                report = await verify_parity(db)
                assert report.ok is True, report.summary

                # ---- status endpoint data reflects posted + parity ----
                status = await get_entity_status(db, entity.id)
                assert status.entity.id == entity.id
                assert status.has_coa is True
                assert status.account_count == 65
                assert len(status.bank_accounts) == 1
                assert status.pending_opening_transactions == []
                assert status.ledger_name == entity.ledger_name
                assert status.formance == "reachable"
                assert status.parity == "ok"
    finally:
        await db.close()
        await engine.dispose()
