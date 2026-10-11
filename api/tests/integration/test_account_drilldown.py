"""Contract F37 — account drill-down: service + route.

A real async ORM on in-memory SQLite (the ``test_transaction_orm_reversal``
DDL pattern) drives ``TransactionService.list_for_account`` and the
``GET /api/v1/accounts/{code}/transactions`` route: newest-first items,
per-item running balances, date/entity/limit filters, the 404
``{type: account_not_found}``, and the route's limit clamp.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.config.database import Base, get_db
from src.index import app
from src.models.account import Account
from src.models.transaction import Posting, Transaction
from src.routes.transactions import list_account_transactions
from src.services.transaction_service import TransactionService

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
    ("2000", "Trade Creditors", "Liability", "CurrentLiability"),
    ("4000", "Sales - Standard Rated", "Revenue", "Revenue"),
    ("5000", "Cost of Sales", "Expense", "DirectCost"),
]

# (ref, status, effective_date, [(code, debit_pence, credit_pence)], entity_key)
LEDGER_ROWS = [
    ("JE-2026-0001", "posted", date(2026, 1, 10), [("1100", 100000, 0), ("4000", 0, 100000)], None),
    ("JE-2026-0002", "posted", date(2026, 2, 10), [("1100", 50000, 0), ("4000", 0, 50000)], None),
    ("JE-2026-0003", "posted", date(2026, 3, 1), [("1000", 30000, 0), ("1100", 0, 30000)], None),
    ("JE-2026-0004", "draft", date(2026, 3, 2), [("1100", 77777, 0), ("4000", 0, 77777)], None),
    ("JE-2026-0005", "posted", date(2026, 3, 5), [("5000", 25000, 0), ("2000", 0, 25000)], None),
]


class World:
    def __init__(self, db: AsyncSession, engine: Any, accounts: dict[str, Account]) -> None:
        self.db = db
        self.engine = engine
        self.accounts = accounts


async def _make_world(coa_rows=COA_SEED, *, with_ledger: bool = True) -> World:
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
    accounts: dict[str, Account] = {}
    for code, name, category, type_ in coa_rows:
        account = Account(
            id=uuid.uuid4(),
            code=code,
            name=name,
            category=category,
            type=type_,
            is_active=True,
            created_at=now,
            updated_at=now,
        )
        accounts[code] = account
        db.add(account)

    if with_ledger:
        for ref, status, day, legs, _entity in LEDGER_ROWS:
            db.add(
                Transaction(
                    id=uuid.uuid4(),
                    reference=ref,
                    description=f"Test {ref}",
                    currency="GBP",
                    status=status,
                    effective_date=day,
                    total_amount=0,
                    created_at=now,
                    updated_at=now,
                )
            )
            tx = (
                await db.execute(
                    text("SELECT id FROM transactions WHERE reference = :ref"),
                    {"ref": ref},
                )
            ).scalar_one()
            for code, debit, credit in legs:
                db.add(
                    Posting(
                        transaction_id=uuid.UUID(str(tx)),
                        account_id=accounts[code].id,
                        debit_amount=debit,
                        credit_amount=credit,
                    )
                )
    await db.commit()
    return World(db, engine, accounts)


async def _close(world: World) -> None:
    await world.db.close()
    await world.engine.dispose()


# ---------------------------------------------------------------------------
# Service — list_for_account
# ---------------------------------------------------------------------------


async def test_list_for_account_running_balance_newest_first() -> None:
    world = await _make_world()
    try:
        result = await TransactionService.list_for_account(world.db, "1100")
    finally:
        await _close(world)

    assert result is not None
    assert result["code"] == "1100"
    assert result["name"] == "Trade Debtors"
    assert result["count"] == 3
    # Newest first; the DRAFT JE-2026-0004 is out of the posted scope.
    assert [item["reference"] for item in result["items"]] == [
        "JE-2026-0003",
        "JE-2026-0002",
        "JE-2026-0001",
    ]
    assert [item["effective_date"] for item in result["items"]] == [
        date(2026, 3, 1),
        date(2026, 2, 10),
        date(2026, 1, 10),
    ]
    # THIS account's legs only.
    assert [(item["debit_pence"], item["credit_pence"]) for item in result["items"]] == [
        (0, 30000),
        (50000, 0),
        (100000, 0),
    ]
    # Per-item running balance, cumulative from the oldest.
    assert [item["balance_pence"] for item in result["items"]] == [120000, 150000, 100000]
    assert result["running_balance_pence"] == 120000
    assert {item["status"] for item in result["items"]} == {"posted"}
    assert all("transaction_id" in item for item in result["items"])


async def test_list_for_account_unknown_and_inactive_codes_return_none() -> None:
    world = await _make_world()
    try:
        assert await TransactionService.list_for_account(world.db, "9999") is None
        world.accounts["1100"].is_active = False
        await world.db.commit()
        assert await TransactionService.list_for_account(world.db, "1100") is None
    finally:
        await _close(world)


async def test_list_for_account_date_window_and_limit() -> None:
    world = await _make_world()
    try:
        windowed = await TransactionService.list_for_account(
            world.db, "1100", date_from=date(2026, 2, 1), date_to=date(2026, 3, 31)
        )
        limited = await TransactionService.list_for_account(world.db, "1100", limit=2)
    finally:
        await _close(world)

    assert windowed is not None
    assert [item["reference"] for item in windowed["items"]] == [
        "JE-2026-0003",
        "JE-2026-0002",
    ]
    # The window filters the LISTING; the ending balance still covers the
    # whole in-scope history.
    assert windowed["running_balance_pence"] == 120000
    assert [item["balance_pence"] for item in windowed["items"]] == [120000, 150000]

    assert limited is not None
    assert limited["count"] == 2
    assert [item["reference"] for item in limited["items"]] == [
        "JE-2026-0003",
        "JE-2026-0002",
    ]


async def test_list_for_account_entity_scope_filters_transactions() -> None:
    entity = uuid.uuid4()
    world = await _make_world()
    try:
        for account in world.accounts.values():
            account.entity_id = entity
        await world.db.execute(
            text(
                "UPDATE transactions SET entity_id = :entity, "
                "reference = 'JE-2026-0001' WHERE reference = 'JE-2026-0001'"
            ),
            {"entity": entity.hex},
        )
        await world.db.commit()

        scoped = await TransactionService.list_for_account(
            world.db, "1100", entity_id=entity
        )
        other = await TransactionService.list_for_account(
            world.db, "1100", entity_id=uuid.uuid4()
        )
    finally:
        await _close(world)

    assert scoped is not None
    assert [item["reference"] for item in scoped["items"]] == ["JE-2026-0001"]
    assert scoped["running_balance_pence"] == 100000
    # A different entity has no 1100 account of its own → 404 upstream.
    assert other is None


# ---------------------------------------------------------------------------
# Route — GET /api/v1/accounts/{code}/transactions
# ---------------------------------------------------------------------------


async def test_route_shape_and_running_balance() -> None:
    world = await _make_world()
    try:
        response = await list_account_transactions(
            code="1100", db=world.db, limit=100
        )
    finally:
        await _close(world)

    assert response.status_code == 200
    body = json.loads(response.body)
    assert set(body) == {"code", "name", "items", "running_balance_pence", "count"}
    assert body["code"] == "1100"
    assert body["name"] == "Trade Debtors"
    assert body["count"] == 3
    assert body["running_balance_pence"] == 120000
    assert [item["reference"] for item in body["items"]] == [
        "JE-2026-0003",
        "JE-2026-0002",
        "JE-2026-0001",
    ]
    assert body["items"][0]["effective_date"] == "2026-03-01"
    assert body["items"][0]["balance_pence"] == 120000


async def test_route_unknown_and_inactive_code_404() -> None:
    world = await _make_world()
    try:
        missing = await list_account_transactions(code="9999", db=world.db, limit=100)
        world.accounts["2000"].is_active = False
        await world.db.commit()
        inactive = await list_account_transactions(code="2000", db=world.db, limit=100)
    finally:
        await _close(world)

    for response in (missing, inactive):
        assert response.status_code == 404
        body = json.loads(response.body)
        assert body["type"] == "account_not_found"
        assert body["message"]


@pytest.fixture
async def api_client():
    """ASGI client over the real app with get_db bound to the SQLite world."""
    world = await _make_world()
    override = get_db

    async def _override_get_db():
        yield world.db

    app.dependency_overrides[override] = _override_get_db
    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://test")
    yield client, world
    app.dependency_overrides.clear()
    await client.aclose()
    await _close(world)


async def test_route_path_is_registered_under_api_v1_accounts(api_client) -> None:
    client, _world = api_client
    response = await client.get("/api/v1/accounts/1100/transactions")

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == "1100"
    assert body["running_balance_pence"] == 120000


async def test_route_limit_clamp_and_query_params(api_client) -> None:
    client, _world = api_client

    clamped = await client.get("/api/v1/accounts/1100/transactions?limit=500")
    assert clamped.status_code == 422  # le=200

    limited = await client.get(
        "/api/v1/accounts/1100/transactions?limit=1&date_from=2026-01-01&date_to=2026-03-31"
    )
    assert limited.status_code == 200
    body = limited.json()
    assert body["count"] == 1
    assert body["items"][0]["reference"] == "JE-2026-0003"

    missing = await client.get("/api/v1/accounts/9999/transactions")
    assert missing.status_code == 404
    assert missing.json()["type"] == "account_not_found"

    bad_entity = await client.get(
        f"/api/v1/accounts/1100/transactions?entity_id={uuid.uuid4()}"
    )
    assert bad_entity.status_code == 404
    assert bad_entity.json()["type"] == "account_not_found"
