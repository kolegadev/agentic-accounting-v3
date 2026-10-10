"""Contract F18 unit tests — multi-entity resolution, scoping, and stamping.

Covers the entity-resolution helpers (``get_active_entity`` /
``get_ledger_for_entity`` / ``AmbiguousEntityError``), entity-scoped
CoaService / TransactionService / BankService filters (asserted on the
generated WHERE clauses), entity stamping on every creation path, and the
per-row ledger resolution in ``post_transaction`` /
``reverse_transaction`` (a recorder captures the Formance ledger each
mirror was written to).  Legacy NULL-scope behavior stays byte-identical:
no entity_id → no filter, no stamp, ledger ``"main"``, no extra query.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.models.account import Account
from src.models.bank_account import BankAccount
from src.models.entity import Entity
from src.models.transaction import Posting, Transaction
from src.services.bank_service import BankService
from src.services.coa_service import CoaService
from src.services.entity_service import (
    AmbiguousEntityError,
    EntityServiceError,
    get_active_entity,
    get_active_ledger,
    get_ledger_for_entity,
)
from src.services.setup_service import _account_count, _bank_count, _draft_count
from src.services.transaction_service import TransactionService
from src.validators.account import AccountCreate
from src.validators.bank import BankAccountCreate
from src.validators.transaction import PostingCreate, TransactionCreate

NOW = datetime(2026, 10, 10, 12, 0, 0, tzinfo=UTC)
ENTITY_ID = uuid.uuid4()
USER_ID = uuid.uuid4()


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_db() -> AsyncMock:
    """AsyncMock DB session whose refresh populates server-default fields."""
    db = AsyncMock()
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()

    async def _refresh(obj: object, attribute_names=None) -> None:
        if hasattr(obj, "id") and obj.id is None:
            obj.id = uuid.uuid4()
        if hasattr(obj, "is_active") and obj.is_active is None:
            obj.is_active = True
        if hasattr(obj, "created_at") and obj.created_at is None:
            obj.created_at = NOW
        if hasattr(obj, "updated_at") and obj.updated_at is None:
            obj.updated_at = NOW

    db.refresh = _refresh
    return db


def make_entity(**overrides) -> Entity:
    kwargs = {
        "id": ENTITY_ID,
        "name": "Acme Holdings Ltd",
        "legal_type": "limited_company",
        "framework": "uk-gaap",
        "fiscal_year_start": "04-06",
        "vat_registered": True,
        "vat_scheme": "standard",
        "currency": "GBP",
        "ledger_name": "entity-acme123",
        "setup_state": "complete",
        "created_by": USER_ID,
        "is_active": True,
        "created_at": NOW,
        "updated_at": NOW,
    }
    kwargs.update(overrides)
    return Entity(**kwargs)


def _entities_result(entities: list[Entity]) -> MagicMock:
    result = MagicMock()
    result.scalars.return_value.all.return_value = entities
    return result


def _scalar_result(value) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    result.scalars.return_value.all.return_value = [value] if value else []
    return result


def _empty_scalars_result() -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    result.scalars.return_value.all.return_value = []
    result.scalar_one.return_value = 0
    return result


def make_transaction(entity_id: uuid.UUID | None = None, **overrides) -> Transaction:
    tx_id = uuid.uuid4()
    kwargs = {
        "id": tx_id,
        "entity_id": entity_id,
        "reference": None,
        "description": "Opening balances",
        "currency": "GBP",
        "status": "draft",
        "effective_date": date(2026, 4, 6),
        "total_amount": 1000000,
        "created_at": NOW,
        "updated_at": NOW,
    }
    kwargs.update(overrides)
    tx = Transaction(**kwargs)
    bank_id = uuid.uuid4()
    equity_id = uuid.uuid4()
    tx.postings = [
        Posting(
            id=uuid.uuid4(),
            transaction_id=tx_id,
            account_id=bank_id,
            debit_amount=1000000,
            credit_amount=0,
            created_at=NOW,
        ),
        Posting(
            id=uuid.uuid4(),
            transaction_id=tx_id,
            account_id=equity_id,
            debit_amount=0,
            credit_amount=1000000,
            created_at=NOW,
        ),
    ]
    return tx


class LedgerRecorder:
    """Records the ledger each Formance mirror was written to."""

    def __init__(self) -> None:
        self.ledgers: list[str] = []

    async def create_transaction(self, **kwargs) -> dict:
        self.ledgers.append(kwargs.get("ledger"))
        return {"txid": len(self.ledgers)}


# ---------------------------------------------------------------------------
# get_active_entity
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_active_entity_zero_returns_none(mock_db: AsyncMock) -> None:
    mock_db.execute.return_value = _entities_result([])
    assert await get_active_entity(mock_db) is None


@pytest.mark.asyncio
async def test_get_active_entity_single_returns_it(mock_db: AsyncMock) -> None:
    entity = make_entity()
    mock_db.execute.return_value = _entities_result([entity])
    assert await get_active_entity(mock_db) is entity


@pytest.mark.asyncio
async def test_get_active_entity_filters_is_active(mock_db: AsyncMock) -> None:
    """The query filters on is_active (modelled by returning post-filter rows)."""
    captured: dict[str, str] = {}

    def _execute(stmt, *args, **kwargs):
        captured["stmt"] = str(stmt)
        return _entities_result([])

    mock_db.execute.side_effect = _execute
    await get_active_entity(mock_db)
    assert "entities.is_active IS true" in captured["stmt"]


@pytest.mark.asyncio
async def test_get_active_entity_two_raises_ambiguous_409(
    mock_db: AsyncMock,
) -> None:
    """More than one active entity → AmbiguousEntityError (EntityServiceError 409)."""
    e1 = make_entity()
    e2 = make_entity(id=uuid.uuid4(), name="Beta LLP")
    mock_db.execute.return_value = _entities_result([e1, e2])

    with pytest.raises(AmbiguousEntityError) as excinfo:
        await get_active_entity(mock_db)

    assert isinstance(excinfo.value, EntityServiceError)
    assert excinfo.value.status_code == 409
    assert excinfo.value.error_type == "ambiguous_entity"
    assert set(excinfo.value.active_ids) == {e1.id, e2.id}


@pytest.mark.asyncio
async def test_get_active_entity_inactive_only_returns_none(
    mock_db: AsyncMock,
) -> None:
    """An INACTIVE-only entity set (post-filter rows) resolves to None."""
    mock_db.execute.return_value = _entities_result([])
    assert await get_active_entity(mock_db) is None


@pytest.mark.asyncio
async def test_get_active_ledger_multiple_entities_still_main(
    mock_db: AsyncMock,
) -> None:
    """get_active_ledger keeps single-entity semantics: two active entities
    → 'main' (delegates to get_active_entity, catches the ambiguity)."""
    e1 = make_entity()
    e2 = make_entity(id=uuid.uuid4(), name="Beta LLP")
    mock_db.execute.return_value = _entities_result([e1, e2])
    assert await get_active_ledger(mock_db) == "main"


def test_ambiguous_entity_error_contract() -> None:
    """AmbiguousEntityError is an EntityServiceError with 409 + error_type."""
    err = AmbiguousEntityError()
    assert isinstance(err, EntityServiceError)
    assert err.status_code == 409
    assert err.error_type == "ambiguous_entity"
    assert "explicit entity_id" in err.message


# ---------------------------------------------------------------------------
# get_ledger_for_entity
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_ledger_for_entity_none_is_main_without_query(
    mock_db: AsyncMock,
) -> None:
    """None → 'main' WITHOUT touching the DB (legacy global scope)."""
    assert await get_ledger_for_entity(mock_db, None) == "main"
    mock_db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_ledger_for_entity_known_entity(mock_db: AsyncMock) -> None:
    entity = make_entity(ledger_name="entity-acme123")
    mock_db.execute.return_value = _scalar_result(entity)
    assert await get_ledger_for_entity(mock_db, entity.id) == "entity-acme123"


@pytest.mark.asyncio
async def test_get_ledger_for_entity_unknown_is_main(mock_db: AsyncMock) -> None:
    mock_db.execute.return_value = _scalar_result(None)
    assert await get_ledger_for_entity(mock_db, uuid.uuid4()) == "main"


@pytest.mark.asyncio
async def test_get_ledger_for_entity_failure_is_main(mock_db: AsyncMock) -> None:
    """A lookup failure (e.g. no entities table) degrades to 'main'."""
    mock_db.execute = AsyncMock(side_effect=RuntimeError("no such table"))
    assert await get_ledger_for_entity(mock_db, uuid.uuid4()) == "main"


# ---------------------------------------------------------------------------
# CoaService — entity scoping + stamping
# ---------------------------------------------------------------------------

def _accounts_list_result(accounts: list[Account]) -> MagicMock:
    result = MagicMock()
    result.scalars.return_value.all.return_value = accounts
    return result


@pytest.mark.asyncio
async def test_list_accounts_scoped_adds_entity_filter(mock_db: AsyncMock) -> None:
    captured: dict[str, str] = {}

    def _execute(stmt, *args, **kwargs):
        captured["stmt"] = str(stmt)
        return _accounts_list_result([])

    mock_db.execute.side_effect = _execute
    await CoaService.list_accounts(mock_db, entity_id=ENTITY_ID)
    assert "entity_id = :entity_id" in captured["stmt"]


@pytest.mark.asyncio
async def test_list_accounts_legacy_has_no_entity_filter(
    mock_db: AsyncMock,
) -> None:
    """entity_id=None keeps the legacy global listing (no WHERE on entity_id)."""
    captured: dict[str, str] = {}

    def _execute(stmt, *args, **kwargs):
        captured["stmt"] = str(stmt)
        return _accounts_list_result([])

    mock_db.execute.side_effect = _execute
    await CoaService.list_accounts(mock_db)
    assert "entity_id = :entity_id" not in captured["stmt"]


@pytest.mark.asyncio
async def test_get_account_by_code_scoped_adds_entity_filter(
    mock_db: AsyncMock,
) -> None:
    captured: dict[str, str] = {}

    def _execute(stmt, *args, **kwargs):
        captured["stmt"] = str(stmt)
        return _scalar_result(None)

    mock_db.execute.side_effect = _execute
    await CoaService.get_account_by_code(mock_db, "1000", entity_id=ENTITY_ID)
    assert "entity_id = :entity_id" in captured["stmt"]


@pytest.mark.asyncio
async def test_get_account_by_code_legacy_has_no_entity_filter(
    mock_db: AsyncMock,
) -> None:
    captured: dict[str, str] = {}

    def _execute(stmt, *args, **kwargs):
        captured["stmt"] = str(stmt)
        return _scalar_result(None)

    mock_db.execute.side_effect = _execute
    await CoaService.get_account_by_code(mock_db, "1000")
    assert "entity_id = :entity_id" not in captured["stmt"]


@pytest.mark.asyncio
async def test_create_account_stamps_entity_id(mock_db: AsyncMock) -> None:
    """create_account stamps the entity AND scopes the uniqueness check."""
    captured: dict[str, str] = {}

    def _execute(stmt, *args, **kwargs):
        captured["stmt"] = str(stmt)
        return _scalar_result(None)

    mock_db.execute.side_effect = _execute
    result = await CoaService.create_account(
        mock_db,
        AccountCreate(
            code="5210",
            name="Marketing",
            category="Expense",
            type="Expense",
        ),
        entity_id=ENTITY_ID,
    )
    assert result.code == "5210"
    # the uniqueness lookup was scoped to the entity
    assert "entity_id = :entity_id" in captured["stmt"]
    # the created ORM account carries the stamp
    added = mock_db.add.call_args_list[0].args[0]
    assert isinstance(added, Account)
    assert added.entity_id == ENTITY_ID


@pytest.mark.asyncio
async def test_load_template_stamps_every_account(mock_db: AsyncMock) -> None:
    """load_template stamps every created account and scopes its dedup check."""
    captured: dict[str, str] = {}

    def _execute(stmt, *args, **kwargs):
        captured.setdefault("stmt", str(stmt))
        return _scalar_result(None)

    mock_db.execute.side_effect = _execute
    created = await CoaService.load_template(
        mock_db, "uk_limited_company_vat", entity_id=ENTITY_ID
    )
    assert len(created) == 65
    assert "entity_id = :entity_id" in captured["stmt"]  # dedup query scoped
    added_accounts = [
        call.args[0] for call in mock_db.add.call_args_list
    ]
    assert len(added_accounts) == 65
    assert all(a.entity_id == ENTITY_ID for a in added_accounts)


@pytest.mark.asyncio
async def test_load_template_legacy_null_stamps_and_global_dedup(
    mock_db: AsyncMock,
) -> None:
    """entity_id=None → NULL stamps and the legacy unscoped dedup check."""
    captured: dict[str, str] = {}

    def _execute(stmt, *args, **kwargs):
        captured.setdefault("stmt", str(stmt))
        return _scalar_result(None)

    mock_db.execute.side_effect = _execute
    created = await CoaService.load_template(mock_db, "uk_limited_company_vat")
    assert len(created) == 65
    assert "entity_id = :entity_id" not in captured["stmt"]
    added_accounts = [call.args[0] for call in mock_db.add.call_args_list]
    assert len(added_accounts) == 65
    assert all(a.entity_id is None for a in added_accounts)


@pytest.mark.asyncio
async def test_create_account_legacy_null_stamp_and_global_uniqueness(
    mock_db: AsyncMock,
) -> None:
    captured: dict[str, str] = {}

    def _execute(stmt, *args, **kwargs):
        captured["stmt"] = str(stmt)
        return _scalar_result(None)

    mock_db.execute.side_effect = _execute
    result = await CoaService.create_account(
        mock_db,
        AccountCreate(code="5210", name="Marketing", category="Expense", type="Expense"),
    )
    assert result.code == "5210"
    assert "entity_id = :entity_id" not in captured["stmt"]  # global check
    added = mock_db.add.call_args_list[0].args[0]
    assert added.entity_id is None


# ---------------------------------------------------------------------------
# BankService — entity stamping + scoped listing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bank_create_account_stamps_entity_id(mock_db: AsyncMock) -> None:
    result = await BankService.create_account(
        mock_db,
        BankAccountCreate(name="Barclays Business"),
        entity_id=ENTITY_ID,
    )
    assert result.name == "Barclays Business"
    added = mock_db.add.call_args_list[0].args[0]
    assert isinstance(added, BankAccount)
    assert added.entity_id == ENTITY_ID


@pytest.mark.asyncio
async def test_bank_create_account_legacy_null_stamp(mock_db: AsyncMock) -> None:
    result = await BankService.create_account(
        mock_db, BankAccountCreate(name="Barclays Business")
    )
    assert result.name == "Barclays Business"
    added = mock_db.add.call_args_list[0].args[0]
    assert added.entity_id is None


@pytest.mark.asyncio
async def test_bank_list_accounts_scoped_adds_entity_filter(
    mock_db: AsyncMock,
) -> None:
    captured: dict[str, str] = {}

    def _execute(stmt, *args, **kwargs):
        captured["stmt"] = str(stmt)
        return _accounts_list_result([])

    mock_db.execute.side_effect = _execute
    await BankService.list_accounts(mock_db, entity_id=ENTITY_ID)
    assert "entity_id = :entity_id" in captured["stmt"]


@pytest.mark.asyncio
async def test_bank_list_accounts_legacy_has_no_entity_filter(
    mock_db: AsyncMock,
) -> None:
    captured: dict[str, str] = {}

    def _execute(stmt, *args, **kwargs):
        captured["stmt"] = str(stmt)
        return _accounts_list_result([])

    mock_db.execute.side_effect = _execute
    await BankService.list_accounts(mock_db)
    assert "entity_id = :entity_id" not in captured["stmt"]


# ---------------------------------------------------------------------------
# setup_service — entity-scoped guards (counts)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "counter",
    [_account_count, _bank_count, _draft_count],
)
@pytest.mark.asyncio
async def test_setup_counts_scoped_add_entity_filter(
    mock_db: AsyncMock,
    counter,
) -> None:
    captured: dict[str, str] = {}

    def _execute(stmt, *args, **kwargs):
        captured["stmt"] = str(stmt)
        r = MagicMock()
        r.scalar_one_or_none.return_value = 0
        return r

    mock_db.execute.side_effect = _execute
    assert await counter(mock_db, entity_id=ENTITY_ID) == 0
    assert "entity_id = :entity_id" in captured["stmt"]


@pytest.mark.parametrize(
    "counter",
    [_account_count, _bank_count, _draft_count],
)
@pytest.mark.asyncio
async def test_setup_counts_legacy_unfiltered(
    mock_db: AsyncMock,
    counter,
) -> None:
    captured: dict[str, str] = {}

    def _execute(stmt, *args, **kwargs):
        captured["stmt"] = str(stmt)
        r = MagicMock()
        r.scalar_one_or_none.return_value = 0
        return r

    mock_db.execute.side_effect = _execute
    assert await counter(mock_db) == 0
    assert "entity_id = :entity_id" not in captured["stmt"]


# ---------------------------------------------------------------------------
# TransactionService — entity stamping, scoped listing, per-row ledger
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_transaction_stamps_entity_id(mock_db: AsyncMock) -> None:
    """create_transaction stamps the row with the entity id."""
    acc1 = Account(id=uuid.uuid4(), code="1000", name="Bank", category="Asset", type="Bank", is_active=True)
    acc2 = Account(id=uuid.uuid4(), code="3000", name="Capital", category="Equity", type="Equity", is_active=True)
    mock_db.execute.side_effect = [
        _scalar_result(None),  # idempotency check
        _accounts_list_result([acc1, acc2]),  # account validation
    ]

    data = TransactionCreate(
        description="Opening balances",
        effective_date=date(2026, 4, 6),
        idempotency_key=uuid.uuid4(),
        postings=[
            PostingCreate(account_id=acc1.id, debit_amount=1000000, credit_amount=0),
            PostingCreate(account_id=acc2.id, debit_amount=0, credit_amount=1000000),
        ],
    )
    tx = await TransactionService.create_transaction(mock_db, data, entity_id=ENTITY_ID)
    assert tx.entity_id == ENTITY_ID


@pytest.mark.asyncio
async def test_list_transactions_scoped_adds_entity_filter(
    mock_db: AsyncMock,
) -> None:
    captured: dict[str, str] = {}

    def _execute(stmt, *args, **kwargs):
        captured.setdefault("stmt", str(stmt))
        r = MagicMock()
        r.scalar_one.return_value = 0
        r.scalars.return_value.all.return_value = []
        return r

    mock_db.execute.side_effect = _execute
    items, total = await TransactionService.list_transactions(
        mock_db, entity_id=ENTITY_ID
    )
    assert total == 0 and items == []
    assert "entity_id = :entity_id" in captured["stmt"]


@pytest.mark.asyncio
async def test_list_transactions_legacy_has_no_entity_filter(
    mock_db: AsyncMock,
) -> None:
    captured: dict[str, str] = {}

    def _execute(stmt, *args, **kwargs):
        captured.setdefault("stmt", str(stmt))
        r = MagicMock()
        r.scalar_one.return_value = 0
        r.scalars.return_value.all.return_value = []
        return r

    mock_db.execute.side_effect = _execute
    items, total = await TransactionService.list_transactions(mock_db)
    assert total == 0 and items == []
    assert "entity_id = :entity_id" not in captured["stmt"]


@pytest.mark.asyncio
async def test_post_transaction_resolves_ledger_from_tx_entity(
    mock_db: AsyncMock,
    monkeypatch,
) -> None:
    """No ledger arg → the mirror goes to the TRANSACTION's entity ledger."""
    monkeypatch.setenv("FORNANCE_LEDGER_URL", "http://formance:3068")
    entity = make_entity(ledger_name="entity-acme123")
    draft = make_transaction(entity_id=entity.id)
    recorder = LedgerRecorder()

    mock_db.execute.side_effect = [
        _scalar_result(draft),  # post_transaction load
        _scalar_result(None),  # _generate_je_reference
        _scalar_result(entity),  # get_ledger_for_entity lookup
        _accounts_list_result([]),  # _post_to_formance account-code lookup
    ]
    with patch(
        "src.services.transaction_service.formance_client.create_transaction",
        AsyncMock(side_effect=recorder.create_transaction),
    ):
        await TransactionService.post_transaction(mock_db, draft.id)

    assert recorder.ledgers == ["entity-acme123"]


@pytest.mark.asyncio
async def test_post_transaction_explicit_ledger_overrides_entity(
    mock_db: AsyncMock,
    monkeypatch,
) -> None:
    """An explicit ledger is an OVERRIDE — no entity lookup happens at all."""
    monkeypatch.setenv("FORNANCE_LEDGER_URL", "http://formance:3068")
    entity = make_entity(ledger_name="entity-acme123")
    draft = make_transaction(entity_id=entity.id)
    recorder = LedgerRecorder()

    # Exactly three queries: load, JE reference, account-code lookup.
    # A get_ledger_for_entity query would consume a 4th slot and blow up.
    mock_db.execute.side_effect = [
        _scalar_result(draft),
        _scalar_result(None),
        _accounts_list_result([]),
    ]
    with patch(
        "src.services.transaction_service.formance_client.create_transaction",
        AsyncMock(side_effect=recorder.create_transaction),
    ):
        await TransactionService.post_transaction(
            mock_db, draft.id, ledger="custom-ledger"
        )

    assert recorder.ledgers == ["custom-ledger"]


@pytest.mark.asyncio
async def test_post_transaction_null_entity_uses_main_without_lookup(
    mock_db: AsyncMock,
    monkeypatch,
) -> None:
    """NULL-scoped rows mirror to 'main' WITHOUT an entity lookup — even when
    entities exist, the legacy scope never consults them."""
    monkeypatch.setenv("FORNANCE_LEDGER_URL", "http://formance:3068")
    draft = make_transaction(entity_id=None)
    recorder = LedgerRecorder()

    mock_db.execute.side_effect = [
        _scalar_result(draft),
        _scalar_result(None),
        _accounts_list_result([]),
    ]
    with patch(
        "src.services.transaction_service.formance_client.create_transaction",
        AsyncMock(side_effect=recorder.create_transaction),
    ):
        await TransactionService.post_transaction(mock_db, draft.id)

    assert recorder.ledgers == ["main"]


@pytest.mark.asyncio
async def test_create_transaction_legacy_null_stamp(mock_db: AsyncMock) -> None:
    """entity_id=None → the legacy NULL stamp on the row."""
    acc1 = Account(id=uuid.uuid4(), code="1000", name="Bank", category="Asset", type="Bank", is_active=True)
    acc2 = Account(id=uuid.uuid4(), code="3000", name="Capital", category="Equity", type="Equity", is_active=True)
    mock_db.execute.side_effect = [
        _scalar_result(None),
        _accounts_list_result([acc1, acc2]),
    ]

    data = TransactionCreate(
        description="Legacy entry",
        effective_date=date(2026, 4, 6),
        idempotency_key=uuid.uuid4(),
        postings=[
            PostingCreate(account_id=acc1.id, debit_amount=1000000, credit_amount=0),
            PostingCreate(account_id=acc2.id, debit_amount=0, credit_amount=1000000),
        ],
    )
    tx = await TransactionService.create_transaction(mock_db, data)
    assert tx.entity_id is None


@pytest.mark.asyncio
async def test_reverse_transaction_null_entity_uses_main_without_lookup(
    mock_db: AsyncMock,
    monkeypatch,
) -> None:
    """NULL-scoped reversals mirror to 'main' WITHOUT an entity lookup."""
    monkeypatch.setenv("FORNANCE_LEDGER_URL", "http://formance:3068")
    original = make_transaction(entity_id=None, status="posted", reference="JE-2026-0001")
    recorder = LedgerRecorder()

    added_objects: list[object] = []

    def _capture_add(obj) -> None:
        added_objects.append(obj)

    mock_db.add.side_effect = _capture_add

    async def _refresh(obj, attribute_names=None):
        if hasattr(obj, "id") and obj.id is None:
            obj.id = uuid.uuid4()
        if attribute_names and "postings" in attribute_names:
            postings = [p for p in added_objects if isinstance(p, Posting)]
            for p in postings:
                p.transaction_id = obj.id
            obj.postings = postings

    mock_db.refresh = _refresh

    # Exactly three queries: load, JE reference, account-code lookup.
    mock_db.execute.side_effect = [
        _scalar_result(original),
        _scalar_result(None),
        _accounts_list_result([]),
    ]
    with patch(
        "src.services.transaction_service.formance_client.create_transaction",
        AsyncMock(side_effect=recorder.create_transaction),
    ):
        reversing = await TransactionService.reverse_transaction(
            mock_db, original.id
        )

    assert reversing.entity_id is None
    assert recorder.ledgers == ["main"]


@pytest.mark.asyncio
async def test_reverse_transaction_inherits_entity_and_ledger(
    mock_db: AsyncMock,
    monkeypatch,
) -> None:
    """The reversal inherits the original's entity_id and mirrors to that
    entity's ledger (per-row resolution)."""
    monkeypatch.setenv("FORNANCE_LEDGER_URL", "http://formance:3068")
    entity = make_entity(ledger_name="entity-acme123")
    original = make_transaction(entity_id=entity.id, status="posted", reference="JE-2026-0001")
    recorder = LedgerRecorder()

    mock_db.execute.side_effect = [
        _scalar_result(original),  # reverse_transaction load
        _scalar_result(None),  # _generate_je_reference for the reversal
        _scalar_result(entity),  # get_ledger_for_entity (reversal's entity)
        _accounts_list_result([]),  # _post_to_formance account-code lookup
    ]

    # db.refresh must populate the reversal's postings (added via db.add).
    added_objects: list[object] = []

    def _capture_add(obj) -> None:
        added_objects.append(obj)

    mock_db.add.side_effect = _capture_add

    async def _refresh(obj, attribute_names=None):
        if hasattr(obj, "id") and obj.id is None:
            obj.id = uuid.uuid4()
        if attribute_names and "postings" in attribute_names:
            postings = [p for p in added_objects if isinstance(p, Posting)]
            for p in postings:
                p.transaction_id = obj.id
            obj.postings = postings

    mock_db.refresh = _refresh

    with patch(
        "src.services.transaction_service.formance_client.create_transaction",
        AsyncMock(side_effect=recorder.create_transaction),
    ):
        reversing = await TransactionService.reverse_transaction(
            mock_db, original.id
        )

    added = added_objects[0]
    assert isinstance(added, Transaction)
    assert added.entity_id == entity.id
    assert reversing.entity_id == entity.id
    assert recorder.ledgers == ["entity-acme123"]
