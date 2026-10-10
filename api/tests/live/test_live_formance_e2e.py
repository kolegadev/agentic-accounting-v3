"""Contract F14 — live end-to-end against the REAL Formance ledger.

Runs the v3 pipeline (real aiosqlite ORM + real Formance HTTP API + the
production client, mirror, parity verifier and F9 report engine) against a
genuine Formance ledger for the first time.

Every test is decorated with a module-level ``pytestmark`` skip that checks
the environment ONCE per pytest session (env var set AND ``/_info`` returns
200); on hosts without a real ledger (e.g. this Mac) the whole module skips
cleanly and the local suite stays green.

Coverage, in contract order:
  a. ``create_ledger`` on a fresh ``entity-live-<rand>`` ledger — real
     200/201/204/409 behaviour via the client (idempotent, tolerant).
  b. Entity setup on the real ORM (COA template, bank, opening-balances
     draft) → approve via ``orchestrator.approve_entry`` → the REAL mirror
     lands in the entity's Formance ledger → ``verify_parity`` ok.
  c. Supplier-invoice (VAT), sales-invoice (VAT) and depreciation journal
     through the constructors/pipeline → approve each → ``verify_parity``
     ok after every post.
  d. All five F9 reports over the live ledger: identities hold and
     P&L/BS/TB ``verify_against_postgres`` diffs are empty.
  e. SHAPE CAPTURE: the real ``list_transactions`` / ``get_balances``
     payloads are asserted against the client's parsing (cursor object,
     metadata pass-through, per-address/per-asset balances, pagination
     walk), which empirically settles the F13 residuals I3 (trailing-zero
     >2dp amount strings) and I4 (zero-volume addresses omitted from
     balances — probed with a posted-then-reversed transfer).

Run (on thebrick, from /tmp/aa-live):
  FORNANCE_LEDGER_URL=http://127.0.0.1:3068 .venv/bin/python -m pytest \\
      tests/live -v --timeout=120
See docs/live-e2e.md for the full runbook.
"""

from __future__ import annotations

import functools
import json
import os
import random
import string
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import event, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.config.database import Base
from src.models.account import Account
from src.models.user import User
from src.services.entity_service import EntityService
from src.services.formance_client import FormanceClient, formance_client
from src.services.formance_reports import (
    generate_bs,
    generate_cash_flow,
    generate_funds,
    generate_pl,
    generate_tb,
    verify_against_postgres,
)
from src.services.ledger_parity import _parse_balances, verify_parity
from src.services.orchestrator import approve_entry, orchestrate_message
from src.services.setup_service import get_entity_status, setup_entity
from src.services.transaction_service import TransactionService
from src.validators.entity import (
    EntityCreate,
    SetupBankAccount,
    SetupOpeningBalanceItem,
    SetupOpeningBalances,
)
from src.validators.transaction import PostingCreate, TransactionCreate

# ---------------------------------------------------------------------------
# Environment gate — evaluated once per pytest session
# ---------------------------------------------------------------------------

FORNANCE_LEDGER_URL = os.getenv("FORNANCE_LEDGER_URL", "").rstrip("/")


@functools.lru_cache(maxsize=1)
def _live_reachable() -> bool:
    """True when the env var is set AND the real ledger answers ``/_info``.

    Cached: the reachability probe runs exactly once per pytest session
    (contract F14's skipif helper).
    """
    if not FORNANCE_LEDGER_URL:
        return False
    try:
        resp = httpx.get(f"{FORNANCE_LEDGER_URL}/_info", timeout=5.0)
        return resp.status_code == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _live_reachable(),
    reason=(
        "LIVE suite: FORNANCE_LEDGER_URL not set or the real Formance ledger "
        "is unreachable (_info != 200) — skipped on hosts without a ledger"
    ),
)

# ---------------------------------------------------------------------------
# Captured payload observations (written to $LIVE_CAPTURE_OUT, if set)
# ---------------------------------------------------------------------------

SHAPES: dict = {}


def _record(key: str, value) -> None:
    SHAPES[key] = value


def _flush_capture() -> None:
    """Persist the shape findings for the runbook (env LIVE_CAPTURE_OUT)."""
    out = os.getenv("LIVE_CAPTURE_OUT")
    if not out:
        return
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(SHAPES, fh, indent=2, default=str)


# ---------------------------------------------------------------------------
# aiosqlite ORM scaffolding (same DDL + gen_random_uuid shim pattern as
# tests/integration/test_entity_setup_e2e.py)
# ---------------------------------------------------------------------------

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


def _register_gen_random_uuid(engine) -> None:
    @event.listens_for(engine.sync_engine, "connect")
    def _shim(dbapi_conn, _record) -> None:
        dbapi_conn.create_function("gen_random_uuid", 0, lambda: uuid.uuid4().hex)


async def _make_db():
    """Fresh in-memory aiosqlite ORM engine + session with the full schema."""
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    _register_gen_random_uuid(engine)
    async with engine.begin() as conn:
        try:
            await conn.run_sync(Base.metadata.create_all)
        except SQLAlchemyError:
            for ddl in _SQLITE_SCHEMA:
                await conn.execute(text(ddl))
    factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    return factory(), engine


async def _make_owner(db: AsyncSession) -> User:
    now = datetime.now(UTC)
    owner = User(
        id=uuid.uuid4(),
        email=f"owner-{uuid.uuid4().hex[:8]}@acme.test",
        hashed_password="h",
        display_name="Live Owner",
        role="owner",
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    db.add(owner)
    await db.commit()
    return owner


@pytest.fixture
async def db():
    """Fresh aiosqlite ORM with an owner user, per test."""
    session, engine = await _make_db()
    try:
        await _make_owner(session)
        yield session
    finally:
        await session.close()
        await engine.dispose()


async def _create_entity(db: AsyncSession, name: str):
    owner = (await db.execute(select(User).limit(1))).scalars().first()
    return await EntityService.create_entity(
        db,
        EntityCreate(name=name, legal_type="limited_company"),
        created_by=owner.id,
    )


# ---------------------------------------------------------------------------
# Scenario payloads (same numbers as tests/integration/test_formance_reports_e2e.py)
# ---------------------------------------------------------------------------

START = date(2026, 1, 1)
END = date(2026, 12, 31)

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

DEPRECIATION_RAW = (
    '{"skill_id": "depreciation", "facts": {'
    '"asset_account_code": "1320", "cost_pence": 400000, '
    '"residual_pence": 0, "useful_life_months": 40, '
    '"period_end_date": "2026-10-31", "method": "straight_line"}}'
)


def _make_fake_llm(responses: dict[str, str]):
    async def transport(_system_prompt: str, user_message: str) -> str:
        lowered = user_message.lower()
        for key, raw in responses.items():
            if key.lower() in lowered:
                return raw
        return '{"skill_id": null, "clarification": "unrecognised message"}'

    return transport


def _live_llm():
    return _make_fake_llm(
        {
            "opening balances": OPENING_RAW,
            "sales invoice INV-2001": SALES_RAW,
            "supplier invoice INV-3001": SUPPLIER_RAW,
            "depreciation": DEPRECIATION_RAW,
        }
    )


async def _setup_entity(db: AsyncSession, entity):
    return await setup_entity(
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
            opening_date=date(2026, 1, 1),
            equity_account_code="3000",
            assets=[
                SetupOpeningBalanceItem(
                    code="1000", name="Bank Current Account", balance_pence=2000000
                ),
                SetupOpeningBalanceItem(
                    code="1320", name="Computer Equipment", balance_pence=400000
                ),
            ],
        ),
    )


async def _approve(db: AsyncSession, transaction_id: uuid.UUID):
    approval = await approve_entry(db, transaction_id)
    assert approval.parity_ok is True, (
        f"parity drift after approving {transaction_id}: "
        f"{approval.parity_report.summary}"
    )
    return approval


async def _seed_full_scenario(db: AsyncSession, entity):
    """Setup + approve the opening entry and the three F9 entries.

    ``verify_parity`` must be ok after EVERY post (contract (c)).
    """
    setup = await _setup_entity(db, entity)
    assert setup.coa_count == 65
    opening_id = setup.opening_transaction_ids[0]
    await _approve(db, opening_id)
    assert (await verify_parity(db, ledger=entity.ledger_name)).ok is True

    llm = _live_llm()
    results = {"opening": opening_id}
    for label, message in (
        ("supplier", "supplier invoice INV-3001 from Beta Office Ltd"),
        ("sales", "sales invoice INV-2001 to Acme Corp"),
        ("depreciation", "depreciation for the month on the computer equipment"),
    ):
        preview = await orchestrate_message(db, message, llm=llm)
        assert preview.status == "preview", preview.error
        await _approve(db, preview.transaction_id)
        assert (await verify_parity(db, ledger=entity.ledger_name)).ok is True
        results[label] = preview.transaction_id
    return results


# ---------------------------------------------------------------------------
# (a) create_ledger against the real API
# ---------------------------------------------------------------------------


async def test_live_create_ledger_real_route(db) -> None:
    """A fresh ledger is creatable via the client; the route answers
    200/201/204 on create and 409 on repeat — both tolerated."""
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    ledger = f"entity-live-{suffix}"

    client = FormanceClient(base_url=FORNANCE_LEDGER_URL)

    async with httpx.AsyncClient(timeout=10.0) as hc:
        created = await hc.post(f"{FORNANCE_LEDGER_URL}/v2/{ledger}")
        repeat = await hc.post(f"{FORNANCE_LEDGER_URL}/v2/{ledger}")
    _record("create_ledger_route", "POST /v2/{ledger}")
    _record("create_ledger_first_status", created.status_code)
    _record("create_ledger_repeat_status", repeat.status_code)
    assert created.status_code in (200, 201, 204), created.text
    # Real v2.3.x answers the duplicate with 400 LEDGER_ALREADY_EXISTS
    # (older builds used 409) — both are idempotent successes here.
    assert repeat.status_code in (200, 201, 204, 400, 409), repeat.text

    # Client tolerates the duplicate (400/409) path and stays idempotent.
    first = await client.create_ledger(ledger)
    assert first is True
    _flush_capture()


# ---------------------------------------------------------------------------
# (b) entity setup -> approve -> REAL mirror -> parity
# ---------------------------------------------------------------------------


async def test_live_entity_setup_approve_mirror_parity(db) -> None:
    """F11 setup on the real ORM; the approved opening entry lands as a REAL
    Formance mirror on the entity's own ledger and parity is ok."""
    entity = await _create_entity(db, "Acme Holdings Live Ltd")
    ledger = entity.ledger_name
    assert ledger.startswith("entity-")

    setup = await _setup_entity(db, entity)
    assert setup.coa_count == 65
    assert setup.ledger_name == ledger

    approval = await _approve(db, setup.opening_transaction_ids[0])
    assert approval.transaction.status == "posted"
    assert approval.transaction.reference == "JE-2026-0001"

    # The REAL ledger holds exactly one mirror, tagged with our metadata.
    listed = await formance_client.list_transactions(ledger)
    data = listed.get("data") or []
    assert len(data) == 1
    mirror = data[0]
    assert mirror.get("reference") == "JE-2026-0001"
    assert mirror.get("metadata", {}).get("transaction_id") == str(
        setup.opening_transaction_ids[0]
    )

    report = await verify_parity(db, ledger=ledger)
    assert report.ok is True, report.summary

    status = await get_entity_status(db, entity.id)
    assert status.ledger_name == ledger
    assert status.formance == "reachable"
    assert status.parity == "ok"
    _flush_capture()


# ---------------------------------------------------------------------------
# (c) supplier invoice (VAT) + sales invoice (VAT) + depreciation journal
# ---------------------------------------------------------------------------


async def test_live_three_entries_parity_after_each(db) -> None:
    """Each constructor entry is approved and parity holds after EVERY post."""
    entity = await _create_entity(db, "Live Trading Ltd")
    ledger = entity.ledger_name
    await _seed_full_scenario(db, entity)

    listed = await formance_client.list_transactions(ledger)
    data = listed.get("data") or []
    assert len(data) == 4, f"expected 4 mirrors, got {len(data)}"
    _flush_capture()


# ---------------------------------------------------------------------------
# (d) all five F9 reports over the live ledger
# ---------------------------------------------------------------------------


async def test_live_five_reports_identities_and_crosscheck(db) -> None:
    """All 5 reports satisfy their identities on live data and P&L/BS/TB
    agree exactly with the Postgres report service."""
    entity = await _create_entity(db, "Live Reports Ltd")
    ledger = entity.ledger_name
    await _seed_full_scenario(db, entity)

    reports = {
        "pl": await generate_pl(db, ledger, START, END),
        "bs": await generate_bs(db, ledger, as_at=END),
        "tb": await generate_tb(db, ledger, as_at=END),
        "cf": await generate_cash_flow(db, ledger, START, END),
        "funds": await generate_funds(db, ledger, START, END),
    }
    for name, report in reports.items():
        assert report["identity_ok"] is True, f"{name} identity failed: {report}"

    assert reports["pl"]["totals"] == {
        "revenue": 100000,
        "direct_costs": 90000,
        "expenses": 10000,
        "corporation_tax": 0,
    }
    assert reports["bs"]["totals"]["assets"] == 2510000
    assert reports["tb"]["total_debits"] == 2638000
    assert reports["cf"]["reconciliation"]["net_change_in_cash"] == 2000000
    assert reports["funds"]["totals"] == {
        "sources": 2410000,
        "applications": 2410000,
    }

    assert await verify_against_postgres(db, reports["pl"], "pl") == []
    assert await verify_against_postgres(db, reports["bs"], "bs") == []
    assert await verify_against_postgres(db, reports["tb"], "tb") == []
    _flush_capture()


# ---------------------------------------------------------------------------
# (e) SHAPE CAPTURE — real payloads vs the client's parsing (I3 + I4)
# ---------------------------------------------------------------------------


def _scan_amounts(rows: list[dict]) -> tuple[int, list[str]]:
    """Count amount strings with more than 2 decimal places (F13 I3)."""
    count = 0
    samples: list[str] = []
    for row in rows:
        for posting in row.get("postings") or []:
            amount = posting.get("amount")
            if isinstance(amount, (int, float)):
                samples.append(str(amount))
                continue
            if amount is None:
                continue
            text_amt = str(amount)
            if len(samples) < 5:
                samples.append(text_amt)
            try:
                value = Decimal(text_amt)
            except Exception:
                count += 1
                continue
            if value.as_tuple().exponent < -2:
                count += 1
    return count, samples


async def test_live_payload_shape_capture(db) -> None:
    """Empirically settles F13 residuals I3/I4 against the real payloads.

    Asserts the real ``list_transactions`` / ``get_balances`` shapes match
    the client's parsing (cursor object, metadata pass-through,
    per-address/per-asset balances, pagination walk) and probes the
    zero-volume-address behaviour with a posted-then-reversed transfer.
    """
    entity = await _create_entity(db, "Live Shape Capture Ltd")
    ledger = entity.ledger_name
    await _seed_full_scenario(db, entity)

    client = FormanceClient(base_url=FORNANCE_LEDGER_URL)

    # ---- list_transactions: cursor object + tx shape + metadata pass-through
    listed = await client.list_transactions(ledger)
    assert isinstance(listed, dict)
    cursor = listed.get("cursor")
    assert isinstance(cursor, dict), f"cursor is not an object: {cursor!r}"
    assert isinstance(cursor.get("hasMore"), bool)
    if cursor.get("hasMore"):
        assert isinstance(cursor.get("next"), (str, int))
    data = listed.get("data")
    assert isinstance(data, list) and len(data) == 4
    for tx in data:
        assert isinstance(tx, dict)
        assert isinstance(tx.get("txid"), int), f"txid shape: {tx.get('txid')!r}"
        assert isinstance(tx.get("reference"), str)
        assert isinstance(tx.get("timestamp"), str)
        meta = tx.get("metadata")
        assert isinstance(meta, dict), "metadata must pass through"
        assert isinstance(meta.get("transaction_id"), str)
        postings = tx.get("postings")
        assert isinstance(postings, list) and postings
        for posting in postings:
            for key in ("source", "destination", "amount", "asset"):
                assert isinstance(posting.get(key), (str, int)), (
                    f"posting.{key} shape: {posting.get(key)!r}"
                )

    # ---- pagination walk: the client's cursor-following sees every txid
    walked: list[int] = []
    after = None
    for _ in range(50):
        page = await client.list_transactions(ledger, page_size=1, after=after)
        pcursor = (page.get("cursor") or {})
        batch = page.get("data") or []
        walked.extend(tx["txid"] for tx in batch if isinstance(tx, dict))
        if not pcursor.get("hasMore"):
            break
        after = pcursor.get("next") or (batch[-1]["txid"] if batch else None)
        if after is None:
            break
    assert set(walked) == {tx["txid"] for tx in data}, (
        f"pagination walk missed transactions: {walked}"
    )

    # ---- get_balances: per-address/per-asset shape + strict parse (I3)
    balances = await client.get_balances(ledger)
    assert isinstance(balances, dict)
    bdata = balances.get("data")
    assert isinstance(bdata, list) and bdata
    expected_addresses = {
        "1000", "1320", "1360", "1100", "2000", "2210", "2220", "3000",
        "4000", "5000", "6500",
    }
    seen_addresses = set()
    for item in bdata:
        assert isinstance(item, dict)
        address = item.get("address")
        assert isinstance(address, str)
        seen_addresses.add(address)
        per_asset = item.get("balances")
        assert isinstance(per_asset, dict), f"balances shape: {per_asset!r}"
        for asset, value in per_asset.items():
            assert asset == "GBP/2", f"unexpected asset: {asset!r}"
            assert isinstance(value, (str, int)), f"amount shape: {value!r}"
    assert expected_addresses <= seen_addresses, (
        f"real get_balances omitted nonzero addresses: "
        f"{sorted(expected_addresses - seen_addresses)}"
    )

    parsed, parse_errors, foreign_assets = _parse_balances(
        balances, strict=True, expected_assets={"GBP/2"}
    )
    assert parse_errors == [], f"strict parse errors: {parse_errors}"
    assert foreign_assets == [], f"foreign assets: {foreign_assets}"
    assert set(parsed) == seen_addresses

    # I3 record: how many amount strings have >2dp (trailing zeros included)
    over_2dp, samples = _scan_amounts(data)
    _record("i3_amounts_more_than_2dp", over_2dp)
    _record("i3_amount_samples", samples)
    _record("i3_strict_parse_errors", len(parse_errors))

    # ---- I4 probe: a posted-then-reversed transfer nets every touched
    #      address to zero — does the real ledger omit them from balances?
    bank = None
    deposit = None
    for account in (await db.execute(select(Account))).scalars().all():
        if account.code == "1000":
            bank = account
        elif account.code == "1010":
            deposit = account
    assert bank is not None and deposit is not None

    transfer = await TransactionService.create_transaction(
        db,
        TransactionCreate(
            description="I4 probe transfer",
            effective_date=date(2026, 11, 1),
            idempotency_key=uuid.uuid4(),
            postings=[
                PostingCreate(account_id=bank.id, debit_amount=50000),
                PostingCreate(account_id=deposit.id, credit_amount=50000),
            ],
        ),
    )
    posted = await TransactionService.post_transaction(
        db, transfer.id, ledger=ledger
    )
    assert posted.status == "posted"
    reversed_tx = await TransactionService.reverse_transaction(
        db, transfer.id, ledger=ledger
    )
    assert reversed_tx.status == "posted"

    report = await verify_parity(db, ledger=ledger)
    assert report.ok is True, (
        f"parity not ok after reversal (I4): {report.summary}"
    )

    balances_after = await client.get_balances(ledger)
    addresses_after = {
        str(item["address"]) for item in (balances_after.get("data") or [])
    }
    omitted_zero_volume = {"1000", "1010"} - addresses_after
    _record("i4_zero_volume_addresses_omitted", sorted(omitted_zero_volume))
    _flush_capture()
