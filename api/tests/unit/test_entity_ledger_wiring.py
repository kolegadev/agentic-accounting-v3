"""Contract F12 unit tests — entity-ledger wiring (post / approve / reports / status).

Covers the four verifier gaps closed by F12 at the unit level:
  - ``post_transaction`` / ``reverse_transaction`` pass the ledger through to
    ``_post_to_formance`` (mock recorder captures the ledger name);
  - ``approve_entry`` resolves the active entity's ledger and uses it for the
    post AND the post-approve parity check;
  - the report route passes the resolved ledger to every ``generate_*`` call;
  - the entity status parity check runs on the entity's own ledger;
  - NO entity → everything resolves to ``"main"`` (backward compat).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.index import app
from src.models.entity import Entity
from src.models.transaction import Posting, Transaction
from src.services.ledger_parity import ParityReport
from src.services.orchestrator import approve_entry
from src.services.setup_service import get_entity_status
from src.services.transaction_service import TransactionService

NOW = datetime(2026, 10, 10, 12, 0, 0, tzinfo=UTC)
ENTITY_ID = uuid.uuid4()
USER_ID = uuid.uuid4()

PL_REPORT = {
    "report": "profit_and_loss",
    "period": {"start": "2026-01-01", "end": "2026-12-31"},
    "sections": {},
    "totals": {"revenue": 0},
    "profit": 0,
    "identity_ok": True,
}


def _result(*, scalar=None, scalars=None):
    """MagicMock mimicking an AsyncResult for one execute call."""
    m = MagicMock()
    m.scalar_one_or_none.return_value = scalar
    m.scalars.return_value.all.return_value = scalars if scalars is not None else []
    return m


def _draft_transaction() -> Transaction:
    tx_id = uuid.uuid4()
    tx = Transaction(
        id=tx_id,
        reference=None,
        description="Opening balances",
        currency="GBP",
        status="draft",
        effective_date=date(2026, 4, 6),
        total_amount=1000000,
        created_at=NOW,
        updated_at=NOW,
    )
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


def make_entity(ledger_name: str = "entity-abc123") -> Entity:
    return Entity(
        id=ENTITY_ID,
        name="Acme Holdings Ltd",
        legal_type="limited_company",
        framework="uk-gaap",
        fiscal_year_start="04-06",
        vat_registered=True,
        vat_scheme="standard",
        currency="GBP",
        ledger_name=ledger_name,
        created_by=USER_ID,
        is_active=True,
        created_at=NOW,
        updated_at=NOW,
    )


# ---------------------------------------------------------------------------
# post_transaction / reverse_transaction — ledger pass-through
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_transaction_passes_ledger_to_formance() -> None:
    """The ledger argument is passed through to _post_to_formance verbatim."""
    draft = _draft_transaction()
    db = AsyncMock()
    db.execute.side_effect = [
        _result(scalar=draft),  # post_transaction load
        _result(scalar=None),  # _generate_je_reference
    ]

    with patch(
        "src.services.transaction_service.TransactionService._post_to_formance",
        new_callable=AsyncMock,
    ) as mock_post:
        await TransactionService.post_transaction(db, draft.id, ledger="entity-abc123")
        mock_post.assert_awaited_once()
        assert mock_post.await_args.kwargs["ledger"] == "entity-abc123"

    # The default (no ledger) resolves to "main" — backward compatible.
    db.execute.side_effect = [
        _result(scalar=_draft_transaction()),
        _result(scalar=None),
    ]
    with patch(
        "src.services.transaction_service.TransactionService._post_to_formance",
        new_callable=AsyncMock,
    ) as mock_post:
        await TransactionService.post_transaction(db, uuid.uuid4())
        assert mock_post.await_args.kwargs["ledger"] == "main"


@pytest.mark.asyncio
async def test_reverse_transaction_passes_ledger_to_formance() -> None:
    """The ledger argument is passed through on reversals too."""
    original = _draft_transaction()
    original.status = "posted"
    original.reference = "JE-2026-0001"
    db = AsyncMock()
    db.add = MagicMock()
    db.execute.side_effect = [
        _result(scalar=original),  # reverse_transaction load
        _result(scalar=None),  # _generate_je_reference
    ]

    with patch(
        "src.services.transaction_service.TransactionService._post_to_formance",
        new_callable=AsyncMock,
    ) as mock_post:
        await TransactionService.reverse_transaction(db, original.id, ledger="entity-abc123")
        mock_post.assert_awaited_once()
        assert mock_post.await_args.kwargs["ledger"] == "entity-abc123"


# ---------------------------------------------------------------------------
# approve_entry — entity-ledger resolution for post + parity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approve_entry_resolves_entity_ledger_for_post_and_parity(
    monkeypatch,
) -> None:
    """approve_entry posts to the TRANSACTION's own entity ledger and runs
    the post-approve parity on that same ledger with the tx's entity scope
    (Contract F19 — never a global guess)."""
    monkeypatch.setenv("FORNANCE_LEDGER_URL", "http://formance:3068")
    draft = _draft_transaction()
    draft.entity_id = ENTITY_ID
    entity = make_entity("entity-abc123")
    db = AsyncMock()
    db.execute.side_effect = [
        _result(scalar=draft),  # post_transaction load
        _result(scalar=None),  # _generate_je_reference
        _result(scalar=entity),  # get_ledger_for_entity (post mirror)
        _result(scalars=[]),  # _post_to_formance account-code lookup
        _result(scalar=entity),  # get_ledger_for_entity (parity ledger)
    ]
    mock_create = AsyncMock(return_value={"txid": 1})
    parity = ParityReport(ok=True, summary="Ledger parity OK")

    with (
        patch(
            "src.services.transaction_service.formance_client.create_transaction",
            mock_create,
        ),
        patch(
            "src.services.orchestrator.verify_parity",
            AsyncMock(return_value=parity),
        ) as mock_parity,
    ):
        result = await approve_entry(db, draft.id)

    assert result.parity_ok is True
    mock_create.assert_called_once()
    assert mock_create.call_args.kwargs["ledger"] == "entity-abc123"
    mock_parity.assert_awaited_once()
    assert mock_parity.await_args.args[1] == "entity-abc123"
    assert mock_parity.await_args.kwargs["entity_id"] == ENTITY_ID


@pytest.mark.asyncio
async def test_approve_entry_no_entity_uses_main(monkeypatch) -> None:
    """A NULL-scoped transaction resolves both post and parity to 'main' —
    WITHOUT any entity lookup (the legacy scope never consults entities)."""
    monkeypatch.setenv("FORNANCE_LEDGER_URL", "http://formance:3068")
    draft = _draft_transaction()
    db = AsyncMock()
    db.execute.side_effect = [
        _result(scalar=draft),
        _result(scalar=None),
        _result(scalars=[]),
    ]
    mock_create = AsyncMock(return_value={"txid": 1})
    parity = ParityReport(ok=True, summary="Ledger parity OK")

    with (
        patch(
            "src.services.transaction_service.formance_client.create_transaction",
            mock_create,
        ),
        patch(
            "src.services.orchestrator.verify_parity",
            AsyncMock(return_value=parity),
        ) as mock_parity,
    ):
        result = await approve_entry(db, draft.id)

    assert result.parity_ok is True
    assert mock_create.call_args.kwargs["ledger"] == "main"
    mock_parity.assert_awaited_once()
    assert mock_parity.await_args.args[1] == "main"
    assert mock_parity.await_args.kwargs["entity_id"] is None


@pytest.mark.asyncio
async def test_approve_entry_explicit_ledger_wins(monkeypatch) -> None:
    """An explicit ledger skips resolution entirely."""
    monkeypatch.setenv("FORNANCE_LEDGER_URL", "http://formance:3068")
    draft = _draft_transaction()
    db = AsyncMock()
    # Only the post flow queries are expected — no ledger-resolution query:
    # had resolution run, it would consume the draft result and fail.
    db.execute.side_effect = [
        _result(scalar=draft),
        _result(scalar=None),
        _result(scalars=[]),
    ]
    mock_create = AsyncMock(return_value={"txid": 1})
    parity = ParityReport(ok=True, summary="Ledger parity OK")

    with (
        patch(
            "src.services.transaction_service.formance_client.create_transaction",
            mock_create,
        ),
        patch(
            "src.services.orchestrator.verify_parity",
            AsyncMock(return_value=parity),
        ) as mock_parity,
    ):
        await approve_entry(db, draft.id, ledger="custom-ledger")

    assert mock_create.call_args.kwargs["ledger"] == "custom-ledger"
    mock_parity.assert_awaited_once()
    assert mock_parity.await_args.args[1] == "custom-ledger"


@pytest.mark.asyncio
async def test_approve_entry_never_consults_get_active_ledger(
    monkeypatch,
) -> None:
    """The ledger comes from the TRANSACTION's own entity (per-row
    resolution) — get_active_ledger is never called (Contract F19)."""
    monkeypatch.setenv("FORNANCE_LEDGER_URL", "http://formance:3068")
    draft = _draft_transaction()
    draft.entity_id = ENTITY_ID
    entity = make_entity("entity-abc123")
    db = AsyncMock()
    db.execute.side_effect = [
        _result(scalar=draft),
        _result(scalar=None),
        _result(scalar=entity),
        _result(scalars=[]),
        _result(scalar=entity),
    ]
    parity = ParityReport(ok=True, summary="Ledger parity OK")

    with (
        patch(
            "src.services.transaction_service.formance_client.create_transaction",
            AsyncMock(return_value={"txid": 1}),
        ) as mock_create,
        patch(
            "src.services.orchestrator.verify_parity",
            AsyncMock(return_value=parity),
        ) as mock_parity,
        patch(
            "src.services.entity_service.get_active_ledger",
            AsyncMock(return_value="entity-abc123"),
        ) as mock_resolve,
    ):
        result = await approve_entry(db, draft.id)

    assert result.parity_ok is True
    mock_resolve.assert_not_awaited()
    assert mock_create.call_args.kwargs["ledger"] == "entity-abc123"
    assert mock_parity.await_args.args[1] == "entity-abc123"
    assert mock_parity.await_args.kwargs["entity_id"] == ENTITY_ID


# ---------------------------------------------------------------------------
# Report route — resolved ledger passed to the engine
# ---------------------------------------------------------------------------


@pytest.fixture
def report_client() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def clear_overrides() -> None:
    yield
    app.dependency_overrides.clear()


def test_report_route_passes_entity_ledger_to_engine(report_client: TestClient) -> None:
    with (
        patch(
            "src.routes.formance_reports._resolve_scope",
            new_callable=AsyncMock,
            return_value=(ENTITY_ID, "entity-abc123"),
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
        generate.return_value = PL_REPORT
        response = report_client.get(
            "/api/v1/reports/formance/pl",
            params={"period_start": "2026-01-01", "period_end": "2026-12-31"},
        )

    assert response.status_code == 200
    generate.assert_awaited_once()
    assert generate.await_args.args[1] == "entity-abc123"
    assert generate.await_args.kwargs["entity_id"] == ENTITY_ID


def test_report_route_no_entity_uses_main(report_client: TestClient) -> None:
    with (
        patch(
            "src.routes.formance_reports._resolve_scope",
            new_callable=AsyncMock,
            return_value=(None, "main"),
        ),
        patch(
            "src.routes.formance_reports.generate_bs", new_callable=AsyncMock
        ) as generate,
        patch(
            "src.routes.formance_reports.verify_against_postgres",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        generate.return_value = {
            "report": "balance_sheet",
            "as_at": "2026-03-31",
            "sections": {},
            "totals": {"assets": 0, "liabilities": 0, "equity": 0},
            "identity_ok": True,
        }
        response = report_client.get("/api/v1/reports/formance/balance-sheet")

    assert response.status_code == 200
    generate.assert_awaited_once()
    assert generate.await_args.args[1] == "main"
    assert generate.await_args.kwargs["entity_id"] is None


@pytest.mark.parametrize(
    "report_type,params,engine",
    [
        ("pl", {"period_start": "2026-01-01", "period_end": "2026-12-31"}, "generate_pl"),
        ("cash-flow", {"period_start": "2026-01-01", "period_end": "2026-12-31"}, "generate_cash_flow"),
        ("funds", {"period_start": "2026-01-01", "period_end": "2026-12-31"}, "generate_funds"),
        ("balance-sheet", {"as_at": "2026-03-31"}, "generate_bs"),
        ("trial-balance", {"as_at": "2026-03-31"}, "generate_tb"),
    ],
)
def test_every_report_type_passes_the_resolved_ledger(
    report_client: TestClient, report_type: str, params: dict, engine: str
) -> None:
    """Every generate_* call receives the resolved entity ledger + scope."""
    generic = {"report": report_type, "identity_ok": True}
    with (
        patch(
            "src.routes.formance_reports._resolve_scope",
            new_callable=AsyncMock,
            return_value=(ENTITY_ID, "entity-abc123"),
        ),
        patch(
            f"src.routes.formance_reports.{engine}", new_callable=AsyncMock
        ) as generate,
        patch(
            "src.routes.formance_reports.verify_against_postgres",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        generate.return_value = generic
        response = report_client.get(
            f"/api/v1/reports/formance/{report_type}", params=params
        )

    assert response.status_code == 200
    generate.assert_awaited_once()
    assert generate.await_args.args[1] == "entity-abc123"
    assert generate.await_args.kwargs["entity_id"] == ENTITY_ID


# ---------------------------------------------------------------------------
# Entity status — parity on the entity's own ledger
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_entity_status_parity_uses_entity_ledger(monkeypatch) -> None:
    """get_entity_status runs verify_parity against the entity's ledger."""
    monkeypatch.setenv("FORNANCE_LEDGER_URL", "http://formance:3068")
    entity = make_entity("entity-acme123")
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
