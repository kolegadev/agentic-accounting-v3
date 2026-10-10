"""Contract F19 unit tests — multi-entity scoping across the orchestrator,
parity verifier, transaction validation, and the route layer.

Covers the fail-loud entity resolution (``resolve_entity_scope``),
entity-scoped COA resolution + draft stamping in ``orchestrate_message``,
``approve_entry`` resolving the ledger from the TRANSACTION's own entity
(with a scoped post-approve parity call and NEVER ``get_active_ledger``),
``verify_parity`` entity_id filtering on BOTH the transaction and account
queries, the ``_validate_accounts`` ownership check (EntityMismatchError
422, NULL-scope accounts still allowed for entity rows), and the route
layer's 409 {type: ambiguous_entity} + entity_id pass-through.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.index import app
from src.models.account import Account
from src.models.entity import Entity
from src.models.transaction import Posting, Transaction
from src.services.entity_service import (
    AmbiguousEntityError,
    EntityNotFoundError,
    require_active_entity,
    resolve_entity_scope,
)
from src.services.ledger_parity import ParityReport, verify_parity
from src.services.orchestrator import (
    OrchestrateResult,
    approve_entry,
    orchestrate_message,
)
from src.services.transaction_service import (
    EntityMismatchError,
    TransactionService,
)
from src.validators.transaction import PostingCreate, TransactionCreate

NOW = datetime(2026, 10, 10, 12, 0, 0, tzinfo=UTC)
ENTITY_A = uuid.uuid4()
ENTITY_B = uuid.uuid4()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_entity(entity_id: uuid.UUID = ENTITY_A, **overrides) -> Entity:
    kwargs = {
        "id": entity_id,
        "name": "Acme Holdings Ltd",
        "legal_type": "limited_company",
        "framework": "uk-gaap",
        "fiscal_year_start": "04-06",
        "vat_registered": False,
        "currency": "GBP",
        "ledger_name": f"entity-{entity_id.hex[:8]}",
        "created_by": uuid.uuid4(),
        "is_active": True,
        "created_at": NOW,
        "updated_at": NOW,
    }
    kwargs.update(overrides)
    return Entity(**kwargs)


def make_account(
    code: str,
    *,
    entity_id: uuid.UUID | None = None,
    category: str = "Asset",
    type_: str = "Bank",
) -> Account:
    return Account(
        id=uuid.uuid4(),
        code=code,
        name=f"Account {code}",
        category=category,
        type=type_,
        entity_id=entity_id,
        is_active=True,
        created_at=NOW,
        updated_at=NOW,
    )


def make_result(*, scalar=None, scalars=None) -> MagicMock:
    m = MagicMock()
    m.scalar_one_or_none.return_value = scalar
    m.scalars.return_value.all.return_value = scalars if scalars is not None else []
    return m


def entities_result(entities: list[Entity]) -> MagicMock:
    result = MagicMock()
    result.scalars.return_value.all.return_value = entities
    return result


def capturing_execute(sequence: list[MagicMock]) -> tuple[AsyncMock, list[str]]:
    """An execute side_effect that consumes ``sequence`` in order while
    recording each statement's compiled SQL."""
    captured: list[str] = []

    async def _execute(stmt, *args, **kwargs):
        captured.append(str(stmt))
        return sequence.pop(0)

    return AsyncMock(side_effect=_execute), captured


def make_draft(entity_id: uuid.UUID | None = None) -> Transaction:
    tx_id = uuid.uuid4()
    tx = Transaction(
        id=tx_id,
        entity_id=entity_id,
        reference=None,
        description="Opening balances",
        currency="GBP",
        status="draft",
        effective_date=date(2026, 4, 6),
        total_amount=1000000,
        created_at=NOW,
        updated_at=NOW,
    )
    tx.postings = [
        Posting(
            id=uuid.uuid4(),
            transaction_id=tx_id,
            account_id=uuid.uuid4(),
            debit_amount=1000000,
            credit_amount=0,
            created_at=NOW,
        ),
        Posting(
            id=uuid.uuid4(),
            transaction_id=tx_id,
            account_id=uuid.uuid4(),
            debit_amount=0,
            credit_amount=1000000,
            created_at=NOW,
        ),
    ]
    return tx


async def _bank_transfer_llm(raw: str):
    async def transport(system_prompt: str, user_message: str) -> str:
        return raw

    return transport


BANK_TRANSFER_RAW = (
    '{"skill_id": "bank-transfer", "facts": {'
    '"source_account_code": "1000", "destination_account_code": "1001", '
    '"amount_pence": 50000, "transfer_date": "2026-10-02", '
    '"reference": "sweep"}}'
)


class DraftTrackingDB:
    """AsyncMock-style session that assigns ids on flush and tracks adds."""

    def __init__(self) -> None:
        self.db = AsyncMock()
        self.added: list = []
        self.db.add = self.added.append
        self.db.commit = AsyncMock()
        self.db.delete = AsyncMock()
        self.db.flush = AsyncMock()

    def add(self, obj) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        for obj in self.added:
            if isinstance(obj, Transaction) and obj.id is None:
                obj.id = uuid.uuid4()

    async def refresh(self, obj, attribute_names=None) -> None:
        pass


# ---------------------------------------------------------------------------
# resolve_entity_scope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_entity_scope_explicit_wins_without_query() -> None:
    db = AsyncMock()
    assert await resolve_entity_scope(db, ENTITY_A) == ENTITY_A
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_entity_scope_single_active_returns_it() -> None:
    entity = make_entity()
    db = AsyncMock()
    db.execute.return_value = entities_result([entity])
    assert await resolve_entity_scope(db) == entity.id


@pytest.mark.asyncio
async def test_resolve_entity_scope_none_active_is_none() -> None:
    db = AsyncMock()
    db.execute.return_value = entities_result([])
    assert await resolve_entity_scope(db) is None


@pytest.mark.asyncio
async def test_resolve_entity_scope_ambiguous_raises() -> None:
    e1 = make_entity(ENTITY_A)
    e2 = make_entity(ENTITY_B, name="Beta LLP")
    db = AsyncMock()
    db.execute.return_value = entities_result([e1, e2])

    with pytest.raises(AmbiguousEntityError) as excinfo:
        await resolve_entity_scope(db)

    assert excinfo.value.status_code == 409
    assert excinfo.value.error_type == "ambiguous_entity"
    assert set(excinfo.value.active_ids) == {ENTITY_A, ENTITY_B}


@pytest.mark.asyncio
async def test_resolve_entity_scope_lookup_failure_degrades_to_none() -> None:
    """A non-ambiguous lookup failure (e.g. no entities table) degrades to
    the legacy global scope — never a crash."""
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=RuntimeError("no such table"))
    assert await resolve_entity_scope(db) is None


# ---------------------------------------------------------------------------
# require_active_entity — explicit-id validation (Contract F20)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_require_active_entity_returns_active_entity() -> None:
    entity = make_entity()
    db = AsyncMock()
    db.execute.return_value = make_result(scalar=entity)
    assert await require_active_entity(db, entity.id) is entity


@pytest.mark.asyncio
async def test_require_active_entity_unknown_id_raises_404() -> None:
    db = AsyncMock()
    db.execute.return_value = make_result(scalar=None)

    with pytest.raises(EntityNotFoundError) as excinfo:
        await require_active_entity(db, ENTITY_A)

    assert excinfo.value.status_code == 404
    assert excinfo.value.error_type == "entity_not_found"


@pytest.mark.asyncio
async def test_require_active_entity_inactive_raises_404() -> None:
    entity = make_entity(is_active=False)
    db = AsyncMock()
    db.execute.return_value = make_result(scalar=entity)

    with pytest.raises(EntityNotFoundError) as excinfo:
        await require_active_entity(db, entity.id)

    assert excinfo.value.status_code == 404
    assert excinfo.value.error_type == "entity_not_found"


@pytest.mark.asyncio
async def test_orchestrator_resolve_scope_unknown_explicit_id_raises() -> None:
    """The route's _resolve_scope validates an explicit id FIRST — an
    unknown id raises EntityNotFoundError before any resolution."""
    from src.routes.orchestrator import _resolve_scope

    db = AsyncMock()
    db.execute.return_value = make_result(scalar=None)

    with pytest.raises(EntityNotFoundError):
        await _resolve_scope(db, ENTITY_A)


@pytest.mark.asyncio
async def test_formance_reports_resolve_scope_unknown_explicit_id_raises() -> None:
    """Same validation on the report route's _resolve_scope."""
    from src.routes.formance_reports import _resolve_scope as report_resolve

    db = AsyncMock()
    db.execute.return_value = make_result(scalar=None)

    with pytest.raises(EntityNotFoundError):
        await report_resolve(db, ENTITY_A)


# ---------------------------------------------------------------------------
# orchestrate_message — entity scope resolution + draft stamping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orchestrate_stamps_draft_and_scopes_coa_when_single_active() -> None:
    entity = make_entity()
    bank_src = make_account("1000", entity_id=entity.id)
    bank_dst = make_account("1001", entity_id=entity.id)
    tracker = DraftTrackingDB()
    tracker.db.flush = tracker.flush
    tracker.db.refresh = tracker.refresh

    execute, captured = capturing_execute(
        [
            entities_result([entity]),  # resolve_entity_scope
            make_result(scalars=[bank_src, bank_dst]),  # list_accounts
            make_result(scalar=bank_dst),  # get_account_by_code
            make_result(scalar=bank_src),
            make_result(scalar=None),  # idempotency check
            make_result(scalars=[bank_src, bank_dst]),  # validate accounts
        ]
    )
    tracker.db.execute = execute

    llm = await _bank_transfer_llm(BANK_TRANSFER_RAW)
    result = await orchestrate_message(
        tracker.db, "Move £500 from 1000 to 1001", llm=llm
    )

    assert result.status == "preview"
    # The COA listing AND the account-code lookups were entity-scoped.
    assert "entity_id = :entity_id" in captured[1]
    assert "entity_id = :entity_id" in captured[2]
    assert "entity_id = :entity_id" in captured[3]
    drafts = [o for o in tracker.added if isinstance(o, Transaction)]
    assert len(drafts) == 1
    assert drafts[0].entity_id == entity.id


@pytest.mark.asyncio
async def test_orchestrate_explicit_entity_id_skips_active_lookup() -> None:
    bank_src = make_account("1000", entity_id=ENTITY_A)
    bank_dst = make_account("1001", entity_id=ENTITY_A)
    tracker = DraftTrackingDB()
    tracker.db.flush = tracker.flush
    tracker.db.refresh = tracker.refresh

    execute, captured = capturing_execute(
        [
            make_result(scalars=[bank_src, bank_dst]),  # list_accounts
            make_result(scalar=bank_dst),
            make_result(scalar=bank_src),
            make_result(scalar=None),
            make_result(scalars=[bank_src, bank_dst]),
        ]
    )
    tracker.db.execute = execute

    llm = await _bank_transfer_llm(BANK_TRANSFER_RAW)
    result = await orchestrate_message(
        tracker.db, "Move £500 from 1000 to 1001", llm=llm, entity_id=ENTITY_A
    )

    assert result.status == "preview"
    # No active-entity query was issued — the explicit id wins outright.
    assert not any("entities.is_active" in stmt for stmt in captured)
    drafts = [o for o in tracker.added if isinstance(o, Transaction)]
    assert drafts[0].entity_id == ENTITY_A


@pytest.mark.asyncio
async def test_orchestrate_ambiguous_scope_is_structured_error() -> None:
    e1 = make_entity(ENTITY_A)
    e2 = make_entity(ENTITY_B, name="Beta LLP")
    tracker = DraftTrackingDB()
    tracker.db.execute = AsyncMock(return_value=entities_result([e1, e2]))

    llm = await _bank_transfer_llm(BANK_TRANSFER_RAW)
    result = await orchestrate_message(tracker.db, "Move £500", llm=llm)

    assert result.status == "error"
    assert result.error["type"] == "ambiguous_entity"
    assert "explicit entity_id" in result.error["message"]
    assert not [o for o in tracker.added if isinstance(o, Transaction)]


# ---------------------------------------------------------------------------
# approve_entry — per-row ledger + scoped parity, never get_active_ledger
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approve_entry_uses_tx_entity_ledger_and_scoped_parity(
    monkeypatch,
) -> None:
    monkeypatch.setenv("FORNANCE_LEDGER_URL", "http://formance:3068")
    entity = make_entity(ledger_name="entity-acme123")
    draft = make_draft(entity_id=entity.id)
    db = AsyncMock()
    db.execute.side_effect = [
        make_result(scalar=draft),  # post_transaction load
        make_result(scalar=None),  # _generate_je_reference
        make_result(scalar=entity),  # get_ledger_for_entity (post mirror)
        make_result(scalars=[]),  # _post_to_formance account lookup
        make_result(scalar=entity),  # get_ledger_for_entity (parity)
    ]
    parity = ParityReport(ok=True, summary="Ledger parity OK")

    with (
        patch(
            "src.services.transaction_service.formance_client.create_transaction",
            AsyncMock(return_value={"txid": 1}),
        ),
        patch(
            "src.services.orchestrator.verify_parity",
            AsyncMock(return_value=parity),
        ) as mock_parity,
        patch(
            "src.services.entity_service.get_active_ledger",
            AsyncMock(return_value="main"),
        ) as mock_active_ledger,
    ):
        result = await approve_entry(db, draft.id)

    assert result.parity_ok is True
    mock_active_ledger.assert_not_awaited()
    mock_parity.assert_awaited_once()
    assert mock_parity.await_args.args[1] == "entity-acme123"
    assert mock_parity.await_args.kwargs["entity_id"] == entity.id


@pytest.mark.asyncio
async def test_approve_entry_explicit_entity_id_overrides_parity_scope(
    monkeypatch,
) -> None:
    monkeypatch.setenv("FORNANCE_LEDGER_URL", "http://formance:3068")
    entity = make_entity(ENTITY_A, ledger_name="entity-acme123")
    draft = make_draft(entity_id=entity.id)
    db = AsyncMock()
    db.execute.side_effect = [
        make_result(scalar=draft),
        make_result(scalar=None),
        make_result(scalar=entity),
        make_result(scalars=[]),
        make_result(scalar=entity),
    ]
    parity = ParityReport(ok=True, summary="Ledger parity OK")

    with (
        patch(
            "src.services.transaction_service.formance_client.create_transaction",
            AsyncMock(return_value={"txid": 1}),
        ),
        patch(
            "src.services.orchestrator.verify_parity",
            AsyncMock(return_value=parity),
        ) as mock_parity,
    ):
        await approve_entry(db, draft.id, entity_id=ENTITY_B)

    # The route's resolved scope is passed through and wins for parity.
    assert mock_parity.await_args.kwargs["entity_id"] == ENTITY_B


@pytest.mark.asyncio
async def test_approve_entry_null_entity_scope_is_null(monkeypatch) -> None:
    """A NULL-scoped draft → ledger 'main' and a NULL parity scope — no
    entity lookup at all (the legacy flow never consults entities)."""
    monkeypatch.setenv("FORNANCE_LEDGER_URL", "http://formance:3068")
    draft = make_draft(entity_id=None)
    db = AsyncMock()
    db.execute.side_effect = [
        make_result(scalar=draft),
        make_result(scalar=None),
        make_result(scalars=[]),
    ]
    parity = ParityReport(ok=True, summary="Ledger parity OK")

    with (
        patch(
            "src.services.transaction_service.formance_client.create_transaction",
            AsyncMock(return_value={"txid": 1}),
        ),
        patch(
            "src.services.orchestrator.verify_parity",
            AsyncMock(return_value=parity),
        ) as mock_parity,
    ):
        result = await approve_entry(db, draft.id)

    assert result.parity_ok is True
    assert mock_parity.await_args.args[1] == "main"
    assert mock_parity.await_args.kwargs["entity_id"] is None


# ---------------------------------------------------------------------------
# verify_parity — entity_id filtering on transactions AND accounts
# ---------------------------------------------------------------------------


class CapturingParitySession:
    """Answers verify_parity's Postgres queries with empty rows while
    capturing the compiled SQL of every statement."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    async def execute(self, stmt):
        self.statements.append(str(stmt))
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        return result


def _empty_client() -> MagicMock:
    client = MagicMock()
    client.list_transactions = AsyncMock(
        return_value={"cursor": {"hasMore": False}, "data": []}
    )
    client.get_balances = AsyncMock(return_value={"data": []})
    return client


async def _run_parity(db, client, *, entity_id=None) -> ParityReport:
    with patch("src.services.ledger_parity.formance_client", client):
        return await verify_parity(db, entity_id=entity_id)


@pytest.mark.asyncio
async def test_verify_parity_entity_id_filters_both_queries() -> None:
    db = CapturingParitySession()
    report = await _run_parity(db, _empty_client(), entity_id=ENTITY_A)

    assert report.ok is True
    tx_stmt, account_stmt = db.statements[0], db.statements[1]
    assert "transactions.entity_id = :entity_id" in tx_stmt
    assert "accounts.entity_id = :entity_id" in account_stmt


@pytest.mark.asyncio
async def test_verify_parity_null_scope_has_no_entity_filter() -> None:
    db = CapturingParitySession()
    report = await _run_parity(db, _empty_client())

    assert report.ok is True
    for stmt in db.statements[:2]:
        assert "= :entity_id" not in stmt


# ---------------------------------------------------------------------------
# _validate_accounts — entity-ownership (EntityMismatchError 422)
# ---------------------------------------------------------------------------


def _tx_create(postings: list[PostingCreate]) -> TransactionCreate:
    return TransactionCreate(
        description="Opening balances",
        effective_date=date(2026, 4, 6),
        idempotency_key=uuid.uuid4(),
        postings=postings,
    )


def _creation_db(accounts: list[Account]) -> AsyncMock:
    db = AsyncMock()
    db.added: list = []
    db.add = db.added.append
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    db.execute.side_effect = [
        make_result(scalar=None),  # idempotency check
        make_result(scalars=accounts),  # account validation
    ]

    async def _refresh(obj, attribute_names=None) -> None:
        if isinstance(obj, Transaction) and obj.id is None:
            obj.id = uuid.uuid4()
        if isinstance(obj, Transaction) and "postings" in (attribute_names or []):
            obj.postings = []

    db.refresh = _refresh
    return db


@pytest.mark.asyncio
async def test_validate_accounts_cross_entity_raises_entity_mismatch() -> None:
    own = make_account("1000", entity_id=ENTITY_A)
    foreign = make_account("3000", entity_id=ENTITY_B, category="Equity", type_="Equity")
    db = _creation_db([own, foreign])

    with pytest.raises(EntityMismatchError) as excinfo:
        await TransactionService.create_transaction(
            db,
            _tx_create(
                [
                    PostingCreate(account_id=own.id, debit_amount=1000000, credit_amount=0),
                    PostingCreate(account_id=foreign.id, debit_amount=0, credit_amount=1000000),
                ]
            ),
            entity_id=ENTITY_A,
        )

    assert excinfo.value.status_code == 422
    assert excinfo.value.error_type == "entity_mismatch"
    assert str(foreign.id) in excinfo.value.message


@pytest.mark.asyncio
async def test_validate_accounts_null_scoped_account_allowed_for_entity_tx() -> None:
    """A NULL-scoped legacy account is allowed inside an entity-stamped
    transaction (mixed legacy data stays usable)."""
    own = make_account("1000", entity_id=ENTITY_A)
    legacy = make_account("3000", entity_id=None, category="Equity", type_="Equity")
    db = _creation_db([own, legacy])

    tx = await TransactionService.create_transaction(
        db,
        _tx_create(
            [
                PostingCreate(account_id=own.id, debit_amount=1000000, credit_amount=0),
                PostingCreate(account_id=legacy.id, debit_amount=0, credit_amount=1000000),
            ]
        ),
        entity_id=ENTITY_A,
    )
    assert tx.entity_id == ENTITY_A


@pytest.mark.asyncio
async def test_validate_accounts_legacy_tx_accepts_any_account() -> None:
    """A NULL-scoped (legacy) transaction has NO ownership check — any
    account combination validates exactly as before F19."""
    a = make_account("1000", entity_id=ENTITY_A)
    b = make_account("3000", entity_id=ENTITY_B, category="Equity", type_="Equity")
    db = _creation_db([a, b])

    tx = await TransactionService.create_transaction(
        db,
        _tx_create(
            [
                PostingCreate(account_id=a.id, debit_amount=1000000, credit_amount=0),
                PostingCreate(account_id=b.id, debit_amount=0, credit_amount=1000000),
            ]
        ),
    )
    assert tx.entity_id is None


# ---------------------------------------------------------------------------
# Routes — 409 on ambiguity + entity_id pass-through
# ---------------------------------------------------------------------------


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clear_overrides() -> None:
    yield
    app.dependency_overrides.clear()


def _ambiguous_get_active_entity() -> AsyncMock:
    return AsyncMock(side_effect=AmbiguousEntityError([ENTITY_A, ENTITY_B]))


def test_report_route_ambiguous_scope_returns_409(client: TestClient) -> None:
    with patch(
        "src.services.entity_service.get_active_entity",
        _ambiguous_get_active_entity(),
    ):
        response = client.get(
            "/api/v1/reports/formance/pl",
            params={"period_start": "2026-01-01", "period_end": "2026-12-31"},
        )

    assert response.status_code == 409
    body = response.json()
    assert body["type"] == "ambiguous_entity"
    assert "explicit entity_id" in body["message"]


def test_message_route_ambiguous_scope_returns_409(client: TestClient) -> None:
    with (
        patch(
            "src.services.entity_service.get_active_entity",
            _ambiguous_get_active_entity(),
        ),
        patch(
            "src.routes.orchestrator.orchestrate_message", new_callable=AsyncMock
        ) as mock,
    ):
        response = client.post(
            "/api/v1/orchestrator/message", json={"message": "pay the supplier"}
        )

    assert response.status_code == 409
    assert response.json()["type"] == "ambiguous_entity"
    mock.assert_not_awaited()


def test_approve_route_ambiguous_scope_returns_409(client: TestClient) -> None:
    with (
        patch(
            "src.services.entity_service.get_active_entity",
            _ambiguous_get_active_entity(),
        ),
        patch(
            "src.routes.orchestrator.approve_entry", new_callable=AsyncMock
        ) as mock,
    ):
        response = client.post(
            "/api/v1/orchestrator/approve",
            json={"transaction_id": str(uuid.uuid4())},
        )

    assert response.status_code == 409
    assert response.json()["type"] == "ambiguous_entity"
    mock.assert_not_awaited()


def test_reject_route_ambiguous_scope_returns_409(client: TestClient) -> None:
    with (
        patch(
            "src.services.entity_service.get_active_entity",
            _ambiguous_get_active_entity(),
        ),
        patch(
            "src.routes.orchestrator.reject_entry", new_callable=AsyncMock
        ) as mock,
    ):
        response = client.post(
            "/api/v1/orchestrator/reject",
            json={"transaction_id": str(uuid.uuid4())},
        )

    assert response.status_code == 409
    assert response.json()["type"] == "ambiguous_entity"
    mock.assert_not_awaited()


def test_pending_route_ambiguous_scope_returns_409(client: TestClient) -> None:
    with (
        patch(
            "src.services.entity_service.get_active_entity",
            _ambiguous_get_active_entity(),
        ),
        patch(
            "src.routes.orchestrator.TransactionService.list_transactions",
            new_callable=AsyncMock,
        ) as mock,
    ):
        response = client.get("/api/v1/orchestrator/pending")

    assert response.status_code == 409
    assert response.json()["type"] == "ambiguous_entity"
    mock.assert_not_awaited()


def test_message_route_passes_entity_id_through(client: TestClient) -> None:
    result = OrchestrateResult.failure("clarification", "which bank account?")
    with (
        patch(
            "src.routes.orchestrator.require_active_entity",
            new_callable=AsyncMock,
            return_value=make_entity(ENTITY_A),
        ),
        patch(
            "src.routes.orchestrator.resolve_entity_scope",
            new_callable=AsyncMock,
            return_value=ENTITY_A,
        ) as resolve,
        patch(
            "src.routes.orchestrator.orchestrate_message", new_callable=AsyncMock
        ) as mock,
    ):
        mock.return_value = result
        response = client.post(
            "/api/v1/orchestrator/message",
            json={"message": "pay the supplier", "entity_id": str(ENTITY_A)},
        )

    assert response.status_code == 200
    resolve.assert_awaited_once()
    assert resolve.await_args.args[1] == ENTITY_A
    assert mock.await_args.kwargs["entity_id"] == ENTITY_A


def test_approve_route_passes_resolved_entity_id_through(
    client: TestClient,
) -> None:
    tx = make_draft()
    tx.status = "posted"
    from src.services.orchestrator import PostResult

    with (
        patch(
            "src.routes.orchestrator.resolve_entity_scope",
            new_callable=AsyncMock,
            return_value=ENTITY_A,
        ),
        patch(
            "src.routes.orchestrator.approve_entry", new_callable=AsyncMock
        ) as mock,
    ):
        mock.return_value = PostResult(
            transaction=TransactionService._transaction_to_response(tx),
            parity_ok=True,
            parity_report=ParityReport(ok=True, summary="OK"),
        )
        response = client.post(
            "/api/v1/orchestrator/approve", json={"transaction_id": str(tx.id)}
        )

    assert response.status_code == 200
    assert mock.await_args.kwargs["entity_id"] == ENTITY_A


def test_pending_route_scopes_listing_to_resolved_entity(
    client: TestClient,
) -> None:
    with (
        patch(
            "src.routes.orchestrator.resolve_entity_scope",
            new_callable=AsyncMock,
            return_value=ENTITY_A,
        ),
        patch(
            "src.routes.orchestrator.TransactionService.list_transactions",
            new_callable=AsyncMock,
        ) as mock,
    ):
        mock.return_value = ([], 0)
        response = client.get("/api/v1/orchestrator/pending")

    assert response.status_code == 200
    assert mock.await_args.kwargs["entity_id"] == ENTITY_A


def test_report_route_passes_explicit_entity_id_to_engine(
    client: TestClient,
) -> None:
    with (
        patch(
            "src.routes.formance_reports.require_active_entity",
            new_callable=AsyncMock,
            return_value=make_entity(ENTITY_A),
        ),
        patch(
            "src.routes.formance_reports.get_ledger_for_entity",
            new_callable=AsyncMock,
            return_value="entity-acme123",
        ),
        patch(
            "src.routes.formance_reports.generate_pl", new_callable=AsyncMock
        ) as generate,
        patch(
            "src.routes.formance_reports.verify_against_postgres",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        generate.return_value = {
            "report": "profit_and_loss",
            "period": {"start": "2026-01-01", "end": "2026-12-31"},
            "sections": {},
            "totals": {"revenue": 0},
            "profit": 0,
            "identity_ok": True,
        }
        response = client.get(
            "/api/v1/reports/formance/pl",
            params={
                "period_start": "2026-01-01",
                "period_end": "2026-12-31",
                "entity_id": str(ENTITY_A),
            },
        )

    assert response.status_code == 200
    assert generate.await_args.args[1] == "entity-acme123"
    assert generate.await_args.kwargs["entity_id"] == ENTITY_A


# ---------------------------------------------------------------------------
# Resolution pass-through & degradation (extra F19 coverage)
# ---------------------------------------------------------------------------


def test_message_route_single_active_resolution_passes_entity_id(
    client: TestClient,
) -> None:
    entity = make_entity()
    result = OrchestrateResult.failure("clarification", "which bank account?")
    with (
        patch(
            "src.services.entity_service.get_active_entity",
            AsyncMock(return_value=entity),
        ),
        patch(
            "src.routes.orchestrator.orchestrate_message", new_callable=AsyncMock
        ) as mock,
    ):
        mock.return_value = result
        response = client.post(
            "/api/v1/orchestrator/message", json={"message": "pay the supplier"}
        )

    assert response.status_code == 200
    assert mock.await_args.kwargs["entity_id"] == entity.id


def test_report_route_single_active_uses_its_ledger_and_scope(
    client: TestClient,
) -> None:
    entity = make_entity(ledger_name="entity-acme123")
    with (
        patch(
            "src.services.entity_service.get_active_entity",
            AsyncMock(return_value=entity),
        ),
        patch(
            "src.routes.formance_reports.get_ledger_for_entity",
            new_callable=AsyncMock,
            return_value="entity-acme123",
        ),
        patch(
            "src.routes.formance_reports.generate_pl", new_callable=AsyncMock
        ) as generate,
        patch(
            "src.routes.formance_reports.verify_against_postgres",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        generate.return_value = {
            "report": "profit_and_loss",
            "period": {"start": "2026-01-01", "end": "2026-12-31"},
            "sections": {},
            "totals": {"revenue": 0},
            "profit": 0,
            "identity_ok": True,
        }
        response = client.get(
            "/api/v1/reports/formance/pl",
            params={"period_start": "2026-01-01", "period_end": "2026-12-31"},
        )

    assert response.status_code == 200
    assert generate.await_args.args[1] == "entity-acme123"
    assert generate.await_args.kwargs["entity_id"] == entity.id


def test_pending_route_legacy_scope_passes_none(client: TestClient) -> None:
    with (
        patch(
            "src.routes.orchestrator.resolve_entity_scope",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "src.routes.orchestrator.TransactionService.list_transactions",
            new_callable=AsyncMock,
        ) as mock,
    ):
        mock.return_value = ([], 0)
        response = client.get("/api/v1/orchestrator/pending")

    assert response.status_code == 200
    assert mock.await_args.kwargs["entity_id"] is None


def test_report_route_explicit_entity_skips_active_lookup(
    client: TestClient,
) -> None:
    with (
        patch(
            "src.services.entity_service.get_active_entity", new_callable=AsyncMock
        ) as mock_active,
        patch(
            "src.routes.formance_reports.require_active_entity",
            new_callable=AsyncMock,
            return_value=make_entity(ENTITY_A),
        ),
        patch(
            "src.routes.formance_reports.get_ledger_for_entity",
            new_callable=AsyncMock,
            return_value="entity-acme123",
        ),
        patch(
            "src.routes.formance_reports.generate_pl", new_callable=AsyncMock
        ) as generate,
        patch(
            "src.routes.formance_reports.verify_against_postgres",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        generate.return_value = {
            "report": "profit_and_loss",
            "period": {"start": "2026-01-01", "end": "2026-12-31"},
            "sections": {},
            "totals": {"revenue": 0},
            "profit": 0,
            "identity_ok": True,
        }
        response = client.get(
            "/api/v1/reports/formance/pl",
            params={
                "period_start": "2026-01-01",
                "period_end": "2026-12-31",
                "entity_id": str(ENTITY_A),
            },
        )

    assert response.status_code == 200
    mock_active.assert_not_awaited()
    assert generate.await_args.kwargs["entity_id"] == ENTITY_A


@pytest.mark.asyncio
async def test_approve_entry_unknown_entity_falls_back_to_main(monkeypatch) -> None:
    """A stamped transaction whose entity row is GONE mirrors to 'main'
    (get_ledger_for_entity degradation) while the parity scope stays the
    tx's entity_id — consistent, never a crash."""
    monkeypatch.setenv("FORNANCE_LEDGER_URL", "http://formance:3068")
    ghost_id = uuid.uuid4()
    draft = make_draft(entity_id=ghost_id)
    db = AsyncMock()
    db.execute.side_effect = [
        make_result(scalar=draft),
        make_result(scalar=None),
        make_result(scalar=None),  # get_ledger_for_entity -> unknown -> 'main'
        make_result(scalars=[]),
        make_result(scalar=None),  # parity ledger lookup -> 'main'
    ]
    parity = ParityReport(ok=True, summary="Ledger parity OK")

    with (
        patch(
            "src.services.transaction_service.formance_client.create_transaction",
            AsyncMock(return_value={"txid": 1}),
        ),
        patch(
            "src.services.orchestrator.verify_parity",
            AsyncMock(return_value=parity),
        ) as mock_parity,
    ):
        result = await approve_entry(db, draft.id)

    assert result.parity_ok is True
    assert mock_parity.await_args.args[1] == "main"
    assert mock_parity.await_args.kwargs["entity_id"] == ghost_id


@pytest.mark.asyncio
async def test_entity_status_parity_is_entity_scoped(monkeypatch) -> None:
    """get_entity_status runs verify_parity on the entity's ledger WITH the
    entity's scope (Contract F19 — the entities status endpoint)."""
    from src.services.setup_service import get_entity_status

    monkeypatch.setenv("FORNANCE_LEDGER_URL", "http://formance:3068")
    entity = make_entity(ledger_name="entity-acme123")
    db = AsyncMock()
    db.execute.return_value = MagicMock()
    db.execute.return_value.scalar_one_or_none.return_value = 0

    parity = ParityReport(ok=True, summary="Ledger parity OK")
    with (
        patch(
            "src.services.setup_service.EntityService.get_entity",
            AsyncMock(return_value=entity),
        ),
        patch(
            "src.services.setup_service.BankService.list_accounts",
            AsyncMock(return_value=[]),
        ),
        patch(
            "src.services.setup_service.TransactionService.list_transactions",
            AsyncMock(return_value=([], 0)),
        ),
        patch(
            "src.services.setup_service.formance_client.health_check",
            AsyncMock(return_value=True),
        ),
        patch(
            "src.services.setup_service.verify_parity",
            AsyncMock(return_value=parity),
        ) as mock_parity,
    ):
        status = await get_entity_status(db, entity.id)

    assert status.parity == "ok"
    mock_parity.assert_awaited_once()
    assert mock_parity.await_args.kwargs["ledger"] == "entity-acme123"
    assert mock_parity.await_args.kwargs["entity_id"] == entity.id
