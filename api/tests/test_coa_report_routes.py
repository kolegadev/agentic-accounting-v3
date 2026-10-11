# /Users/johnpellew/agentic-accounting-v3/api/tests/test_coa_report_routes.py

"""Contract F39 — the chart-of-accounts Formance report route.

FastAPI TestClient against the accounting API with the scope resolver,
CoaService listing, Formance client calls and the Postgres usage check
mocked at the route module seam.  No live ledger or Postgres query runs
here: the mocks return canned rows and the route must shape the contract
envelope (accounts ordered as the service returns them, each carrying
id/code/name/category/type/vat_rate/balance/used).
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from src.index import app
from src.routes.formance_reports import VALID_REPORT_TYPES
from src.validators.account import AccountResponse
from tests.conftest import NOW

SCOPE_ID = uuid.uuid4()
LEDGER = "acme-ledger"


def make_account(
    code: str,
    name: str = "Bank Current Account",
    category: str = "Asset",
    type_: str = "Bank",
    vat_rate: str | None = None,
) -> AccountResponse:
    return AccountResponse(
        id=uuid.uuid4(),
        code=code,
        name=name,
        category=category,
        type=type_,
        vat_rate=vat_rate,
        is_active=True,
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.fixture
def client() -> TestClient:
    """Return a FastAPI TestClient."""
    return TestClient(app)


def _patch_scope(scope_id= SCOPE_ID, ledger: str = LEDGER):
    return patch(
        "src.routes.formance_reports._resolve_scope",
        new_callable=AsyncMock,
        return_value=(scope_id, ledger),
    )


# ---------------------------------------------------------------------------
# Report type registration
# ---------------------------------------------------------------------------


def test_chart_of_accounts_is_a_valid_report_type() -> None:
    assert "chart-of-accounts" in VALID_REPORT_TYPES


def test_unknown_report_type_message_lists_chart_of_accounts(
    client: TestClient,
) -> None:
    response = client.get("/api/v1/reports/formance/not-a-report")

    assert response.status_code == 422
    body = response.json()
    assert body["type"] == "invalid_report_type"
    assert "chart-of-accounts" in body["message"]


# ---------------------------------------------------------------------------
# Envelope shape
# ---------------------------------------------------------------------------


def test_chart_of_accounts_envelope_shape(client: TestClient) -> None:
    account = make_account("1000", "Bank Current Account", vat_rate="20%")
    balances = {"data": [{"address": "1000", "balances": {"GBP/2": "1234.56"}}]}

    with (
        _patch_scope(),
        patch(
            "src.routes.formance_reports.CoaService.list_accounts",
            new_callable=AsyncMock,
        ) as list_accounts,
        patch(
            "src.routes.formance_reports.formance_client.get_balances",
            new_callable=AsyncMock,
        ) as get_balances,
        patch(
            "src.routes.formance_reports.formance_client."
            "list_account_transactions",
            new_callable=AsyncMock,
        ) as list_tx,
        patch(
            "src.routes.formance_reports.TransactionService.list_for_account",
            new_callable=AsyncMock,
        ) as pg,
    ):
        list_accounts.return_value = [account]
        get_balances.return_value = balances
        list_tx.return_value = {"data": []}
        pg.return_value = {"items": [], "count": 0}
        response = client.get("/api/v1/reports/formance/chart-of-accounts")

    assert response.status_code == 200
    body = response.json()
    assert body["report_type"] == "chart-of-accounts"
    assert set(body) >= {"report_type", "as_at", "report", "cross_check"}

    rows = body["report"]["accounts"]
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == str(account.id)
    assert row["code"] == "1000"
    assert row["name"] == "Bank Current Account"
    assert row["category"] == "Asset"
    assert row["type"] == "Bank"
    assert row["vat_rate"] == "20%"
    assert row["balance"] == "1234.56"
    assert row["balances"] == {"GBP/2": "1234.56"}
    assert row["used"] is False

    assert body["cross_check"]["ok"] is None
    assert "source of record" in body["cross_check"]["note"]


def test_chart_of_accounts_missing_balance_degrades_to_none(
    client: TestClient,
) -> None:
    account = make_account("5210", "Marketing")

    with (
        _patch_scope(),
        patch(
            "src.routes.formance_reports.CoaService.list_accounts",
            new_callable=AsyncMock,
        ) as list_accounts,
        patch(
            "src.routes.formance_reports.formance_client.get_balances",
            new_callable=AsyncMock,
        ) as get_balances,
        patch(
            "src.routes.formance_reports.formance_client."
            "list_account_transactions",
            new_callable=AsyncMock,
        ) as list_tx,
        patch(
            "src.routes.formance_reports.TransactionService.list_for_account",
            new_callable=AsyncMock,
        ) as pg,
    ):
        list_accounts.return_value = [account]
        get_balances.return_value = {"data": []}
        list_tx.return_value = {"data": []}
        pg.return_value = {"items": [], "count": 0}
        response = client.get("/api/v1/reports/formance/chart-of-accounts")

    assert response.status_code == 200
    row = response.json()["report"]["accounts"][0]
    assert row["balance"] is None
    assert row["balances"] == {}


def test_chart_of_accounts_preserves_service_order(client: TestClient) -> None:
    """The service returns accounts ordered by code; the route keeps that."""
    first = make_account("5210", "Marketing")
    second = make_account("1000", "Bank")

    with (
        _patch_scope(),
        patch(
            "src.routes.formance_reports.CoaService.list_accounts",
            new_callable=AsyncMock,
        ) as list_accounts,
        patch(
            "src.routes.formance_reports.formance_client.get_balances",
            new_callable=AsyncMock,
        ) as get_balances,
        patch(
            "src.routes.formance_reports.formance_client."
            "list_account_transactions",
            new_callable=AsyncMock,
        ) as list_tx,
        patch(
            "src.routes.formance_reports.TransactionService.list_for_account",
            new_callable=AsyncMock,
        ) as pg,
    ):
        list_accounts.return_value = [second, first]
        get_balances.return_value = {"data": []}
        list_tx.return_value = {"data": []}
        pg.return_value = {"items": [], "count": 0}
        response = client.get("/api/v1/reports/formance/chart-of-accounts")

    assert response.status_code == 200
    codes = [row["code"] for row in response.json()["report"]["accounts"]]
    assert codes == ["1000", "5210"]


# ---------------------------------------------------------------------------
# Entity scope + ledger wiring
# ---------------------------------------------------------------------------


def test_chart_of_accounts_scopes_listing_and_reads_scope_ledger(
    client: TestClient,
) -> None:
    first = make_account("1000", "Bank")
    second = make_account("5210", "Marketing")

    with (
        _patch_scope(SCOPE_ID, LEDGER),
        patch(
            "src.routes.formance_reports.CoaService.list_accounts",
            new_callable=AsyncMock,
        ) as list_accounts,
        patch(
            "src.routes.formance_reports.formance_client.get_balances",
            new_callable=AsyncMock,
        ) as get_balances,
        patch(
            "src.routes.formance_reports.formance_client."
            "list_account_transactions",
            new_callable=AsyncMock,
        ) as list_tx,
        patch(
            "src.routes.formance_reports.TransactionService.list_for_account",
            new_callable=AsyncMock,
        ) as pg,
    ):
        list_accounts.return_value = [first, second]
        get_balances.return_value = {"data": []}
        list_tx.return_value = {"data": []}
        pg.return_value = {"items": [], "count": 0}
        response = client.get(
            "/api/v1/reports/formance/chart-of-accounts",
            params={"entity_id": str(SCOPE_ID)},
        )

    assert response.status_code == 200
    list_accounts.assert_awaited_once()
    assert list_accounts.await_args.kwargs.get("entity_id") == SCOPE_ID
    get_balances.assert_awaited_once()
    assert get_balances.await_args.args[0] == LEDGER
    assert list_tx.await_count == 2
    assert list_tx.await_args.args[0] == LEDGER


# ---------------------------------------------------------------------------
# The "used" flag
# ---------------------------------------------------------------------------


def test_used_flag_true_when_postgres_has_transactions(
    client: TestClient,
) -> None:
    used = make_account("1000", "Bank")
    fresh = make_account("5210", "Marketing")

    def pg_side_effect(db, code, **kwargs):
        if code == "1000":
            return {
                "code": code,
                "items": [{"transaction_id": "t1", "balance_pence": 100}],
                "count": 1,
            }
        return {"code": code, "items": [], "count": 0}

    with (
        _patch_scope(),
        patch(
            "src.routes.formance_reports.CoaService.list_accounts",
            new_callable=AsyncMock,
        ) as list_accounts,
        patch(
            "src.routes.formance_reports.formance_client.get_balances",
            new_callable=AsyncMock,
        ) as get_balances,
        patch(
            "src.routes.formance_reports.formance_client."
            "list_account_transactions",
            new_callable=AsyncMock,
        ) as list_tx,
        patch(
            "src.routes.formance_reports.TransactionService.list_for_account",
            new_callable=AsyncMock,
        ) as pg,
    ):
        list_accounts.return_value = [used, fresh]
        get_balances.return_value = {"data": []}
        list_tx.return_value = {"data": []}
        pg.side_effect = pg_side_effect
        response = client.get("/api/v1/reports/formance/chart-of-accounts")

    assert response.status_code == 200
    rows = {row["code"]: row for row in response.json()["report"]["accounts"]}
    assert rows["1000"]["used"] is True
    assert rows["5210"]["used"] is False
    # The Formance usage check only ran for the account Postgres cleared.
    assert list_tx.await_count == 1
    assert list_tx.await_args.args[1] == "5210"


def test_used_flag_true_when_only_formance_has_postings(
    client: TestClient,
) -> None:
    account = make_account("1000", "Bank")

    with (
        _patch_scope(),
        patch(
            "src.routes.formance_reports.CoaService.list_accounts",
            new_callable=AsyncMock,
        ) as list_accounts,
        patch(
            "src.routes.formance_reports.formance_client.get_balances",
            new_callable=AsyncMock,
        ) as get_balances,
        patch(
            "src.routes.formance_reports.formance_client."
            "list_account_transactions",
            new_callable=AsyncMock,
        ) as list_tx,
        patch(
            "src.routes.formance_reports.TransactionService.list_for_account",
            new_callable=AsyncMock,
        ) as pg,
    ):
        list_accounts.return_value = [account]
        get_balances.return_value = {"data": []}
        pg.return_value = {"items": [], "count": 0}
        list_tx.return_value = {
            "data": [{"txid": 1, "postings": [], "metadata": {}}]
        }
        response = client.get("/api/v1/reports/formance/chart-of-accounts")

    assert response.status_code == 200
    row = response.json()["report"]["accounts"][0]
    assert row["used"] is True


def test_chart_of_accounts_survives_formance_balance_failure(
    client: TestClient,
) -> None:
    """A down ledger degrades to empty balances — never a 500."""
    account = make_account("1000", "Bank")

    with (
        _patch_scope(),
        patch(
            "src.routes.formance_reports.CoaService.list_accounts",
            new_callable=AsyncMock,
        ) as list_accounts,
        patch(
            "src.routes.formance_reports.formance_client.get_balances",
            new_callable=AsyncMock,
        ) as get_balances,
        patch(
            "src.routes.formance_reports.formance_client."
            "list_account_transactions",
            new_callable=AsyncMock,
        ) as list_tx,
        patch(
            "src.routes.formance_reports.TransactionService.list_for_account",
            new_callable=AsyncMock,
        ) as pg,
    ):
        list_accounts.return_value = [account]
        get_balances.side_effect = httpx.ConnectError("connection refused")
        pg.return_value = {"items": [], "count": 0}
        list_tx.side_effect = httpx.ConnectError("connection refused")
        response = client.get("/api/v1/reports/formance/chart-of-accounts")

    assert response.status_code == 200
    row = response.json()["report"]["accounts"][0]
    assert row["balance"] is None
    assert row["used"] is False
