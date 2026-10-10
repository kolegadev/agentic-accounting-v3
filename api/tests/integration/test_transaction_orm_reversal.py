"""Integration test: TransactionService reversal through a real async ORM.

The unit suite mocks AsyncSession, which hid a production regression:
assigning the reversing transaction's ``postings`` collection while it is
unloaded triggers an implicit lazy load, which raises
``sqlalchemy.exc.MissingGreenlet`` under AsyncSession (breaking
POST /transactions/{id}/reverse with a 500).  This test drives
TransactionService through a real SQLAlchemy async session against an
in-memory SQLite database so lazy loading, refresh, and commit behaviour
are genuinely exercised — reintroducing that collection assignment makes
this test fail with MissingGreenlet.

The mapped models are PostgreSQL-native: ``UUID(as_uuid=True)`` columns,
``ANY(ARRAY[...])`` check constraints, and ``gen_random_uuid()`` server
defaults.  ``Base.metadata.create_all`` therefore cannot compile on
SQLite (it fails with a syntax error on the array check constraint), so
this test falls back to a minimal SQLite-compatible schema for the four
tables the reversal flow touches.  A ``gen_random_uuid`` SQL function is
registered per-connection, returning hex form to match how the ORM binds
UUID values.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

from sqlalchemy import event, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.config.database import Base
from src.models.account import Account
from src.services.transaction_service import TransactionService
from src.validators.transaction import PostingCreate, TransactionCreate

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


async def test_reverse_transaction_roundtrip_on_real_async_orm(monkeypatch) -> None:
    """Create → post → reverse against a real async engine.

    Asserts the reversal persists, the original is marked reversed, and
    the Formance mirror of the reversal swaps source/destination relative
    to the original while keeping amounts equal.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _register_gen_random_uuid(dbapi_conn, _record) -> None:
        dbapi_conn.create_function("gen_random_uuid", 0, lambda: uuid.uuid4().hex)

    async with engine.begin() as conn:
        try:
            await conn.run_sync(Base.metadata.create_all)
        except SQLAlchemyError:
            # Postgres-specific model constructs cannot compile on SQLite;
            # use the minimal SQLite-compatible schema above instead.
            for ddl in _SQLITE_SCHEMA:
                await conn.execute(text(ddl))

    session_factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    try:
        async with session_factory() as db:
            now = datetime.now(UTC)
            bank = Account(
                id=uuid.uuid4(),
                code="1000",
                name="Bank Current",
                category="Asset",
                type="Bank",
                is_active=True,
                created_at=now,
                updated_at=now,
            )
            revenue = Account(
                id=uuid.uuid4(),
                code="4000",
                name="Sales Revenue",
                category="Revenue",
                type="Revenue",
                is_active=True,
                created_at=now,
                updated_at=now,
            )
            db.add_all([bank, revenue])
            await db.commit()

            data = TransactionCreate(
                description="Sales invoice",
                idempotency_key=uuid.uuid4(),
                postings=[
                    PostingCreate(account_id=bank.id, debit_amount=10000),
                    PostingCreate(account_id=revenue.id, credit_amount=10000),
                ],
            )

            mock_create = AsyncMock(return_value={"txid": 42})
            monkeypatch.setenv("FORNANCE_LEDGER_URL", "http://formance:3068")
            with patch(
                "src.services.transaction_service.formance_client.create_transaction",
                mock_create,
            ):
                draft = await TransactionService.create_transaction(db, data)
                original = await TransactionService.post_transaction(db, draft.id)
                reversing = await TransactionService.reverse_transaction(
                    db, original.id
                )

            assert original.status == "reversed"
            assert reversing.status == "posted"
            assert reversing.reference is not None
            assert len(reversing.postings) == 2

            # Both the posting and the reversal are mirrored to Formance.
            assert mock_create.call_count == 2
            original_mirror = mock_create.call_args_list[0].kwargs["postings"]
            reversal_mirror = mock_create.call_args_list[1].kwargs["postings"]

            # Original: Dr 1000 £100.00 / Cr 4000 £100.00 ->
            # {source: 4000 (credit), destination: 1000 (debit)}.
            assert original_mirror == [
                {
                    "source": "4000",
                    "destination": "1000",
                    "amount": "100.00",
                    "asset": "GBP/2",
                }
            ]
            # Reversal swaps the legs: {source: 1000, destination: 4000}.
            assert reversal_mirror == [
                {
                    "source": "1000",
                    "destination": "4000",
                    "amount": "100.00",
                    "asset": "GBP/2",
                }
            ]
            assert reversal_mirror[0]["amount"] == original_mirror[0]["amount"]
            for posting in (*original_mirror, *reversal_mirror):
                assert posting["source"] != "world"
                assert posting["destination"] != "world"
    finally:
        await engine.dispose()
