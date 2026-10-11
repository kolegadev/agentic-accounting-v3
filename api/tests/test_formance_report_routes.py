# /Users/johnpellew/agentic-accounting-v3/api/tests/test_formance_report_routes.py

"""Contract F10 — API routes for the Formance-derived reports (thin wrappers).

FastAPI TestClient against the accounting API with the F9 engine functions
mocked (AsyncMock) at the route module seam.  No Formance ledger, Postgres
query, or cross-check run happens here: the mocks return canned engine
outputs and diff lists, and the route must shape the contract response.
"""

from __future__ import annotations

import uuid
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.index import app
from src.services.formance_reports import ReportEngineError

PERIOD_START = date(2026, 1, 1)
PERIOD_END = date(2026, 12, 31)

PL_REPORT = {
    "report": "profit_and_loss",
    "period": {"start": "2026-01-01", "end": "2026-12-31"},
    "sections": {
        "revenue": [{"code": "4000", "name": "Sales", "amount": 500000}],
    },
    "totals": {"revenue": 500000},
    "profit": 12345,
    "identity_ok": True,
}

BS_REPORT = {
    "report": "balance_sheet",
    "as_at": "2026-03-31",
    "sections": {},
    "totals": {"assets": 100, "liabilities": 50, "equity": 50},
    "identity_ok": True,
}

TB_REPORT = {
    "report": "trial_balance",
    "as_at": "2026-03-31",
    "accounts": [{"code": "1000", "name": "Bank", "balance": 100, "debit": 100, "credit": 0}],
    "total_debits": 100,
    "total_credits": 100,
    "identity_ok": True,
}

CF_REPORT = {
    "report": "cash_flow",
    "period": {"start": "2026-01-01", "end": "2026-12-31"},
    "sections": {},
    "reconciliation": {"net_change_in_cash": 0},
    "identity_ok": True,
}

FUNDS_REPORT = {
    "report": "funds",
    "period": {"start": "2026-01-01", "end": "2026-12-31"},
    "sources": [],
    "applications": [],
    "identity_ok": True,
}

NO_PG_CROSS_CHECK = {"diffs": [], "ok": None, "note": "no Postgres equivalent"}

AGING_AS_AT = date(2026, 3, 31)

# Contract F37 — the engine payloads the route maps into the response.
AR_AGING = {
    "report": "aged_receivables",
    "as_at": AGING_AS_AT,
    "items": [
        {
            "ref": "INV-2001",
            "date": date(2026, 2, 1),
            "original_pence": 120000,
            "paid_pence": 50000,
            "outstanding_pence": 70000,
            "days_overdue": 58,
        }
    ],
    "buckets": [
        {"bucket": "0-30", "count": 0, "total_pence": 0},
        {"bucket": "31-60", "count": 1, "total_pence": 70000},
        {"bucket": "61-90", "count": 0, "total_pence": 0},
        {"bucket": "90+", "count": 0, "total_pence": 0},
    ],
    "total_outstanding_pence": 70000,
    "ledger_balance": {
        "code": "1100",
        "name": "Trade Debtors",
        "balance_pence": 70000,
    },
    "reconciled": True,
    "note": "Ledger-derived aging: invoice moments (debits to 1100) FIFO-"
    "matched against customer receipts (credits to 1100) from the same "
    "posted ledger the Balance Sheet reads.",
}

AP_AGING = {
    "report": "aged_payables",
    "as_at": AGING_AS_AT,
    "items": [
        {
            "ref": "INV-3001",
            "date": date(2026, 2, 1),
            "original_pence": 108000,
            "paid_pence": 0,
            "outstanding_pence": 108000,
            "days_overdue": 58,
        }
    ],
    "buckets": [
        {"bucket": "0-30", "count": 0, "total_pence": 0},
        {"bucket": "31-60", "count": 1, "total_pence": 108000},
        {"bucket": "61-90", "count": 0, "total_pence": 0},
        {"bucket": "90+", "count": 0, "total_pence": 0},
    ],
    "total_outstanding_pence": 108000,
    "ledger_balance": {
        "code": "2000",
        "name": "Trade Creditors",
        "balance_pence": -108000,
    },
    "reconciled": True,
    "note": "Ledger-derived aging: supplier-invoice moments (credits to "
    "2000) FIFO-matched against supplier payments (debits to 2000) from "
    "the same posted ledger the Balance Sheet reads.",
}

AGING_CROSS_CHECK = {
    "ok": None,
    "diffs": [],
    "note": (
        "aging is ledger-derived (the same posted ledger the Balance Sheet "
        "reads) and reconciles to the trial-balance balance by "
        "construction — there is no separate Postgres equivalent to diff "
        "against"
    ),
}


@pytest.fixture
def client() -> TestClient:
    """Return a FastAPI TestClient."""
    return TestClient(app)


# ---------------------------------------------------------------------------
# Happy paths — report + identities + cross_check shapes
# ---------------------------------------------------------------------------


def test_pl_happy_path_report_identities_cross_check(client: TestClient) -> None:
    with (
        patch(
            "src.routes.formance_reports.generate_pl", new_callable=AsyncMock
        ) as generate,
        patch(
            "src.routes.formance_reports.verify_against_postgres",
            new_callable=AsyncMock,
        ) as verify,
    ):
        generate.return_value = PL_REPORT
        verify.return_value = []
        response = client.get(
            "/api/v1/reports/formance/pl",
            params={"period_start": "2026-01-01", "period_end": "2026-12-31"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["report_type"] == "pl"
    assert body["report"] == PL_REPORT
    assert body["identities"] == {"identity_ok": True}
    assert body["cross_check"] == {"diffs": [], "ok": True}

    generate.assert_awaited_once()
    assert generate.await_args.kwargs["period_start"] == PERIOD_START
    assert generate.await_args.kwargs["period_end"] == PERIOD_END
    verify.assert_awaited_once()
    assert verify.await_args.args[1] == PL_REPORT
    assert verify.await_args.args[2] == "pl"


def test_balance_sheet_happy_path_with_as_at(client: TestClient) -> None:
    with (
        patch(
            "src.routes.formance_reports.generate_bs", new_callable=AsyncMock
        ) as generate,
        patch(
            "src.routes.formance_reports.verify_against_postgres",
            new_callable=AsyncMock,
        ) as verify,
    ):
        generate.return_value = BS_REPORT
        verify.return_value = []
        response = client.get(
            "/api/v1/reports/formance/balance-sheet",
            params={"as_at": "2026-03-31"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["report_type"] == "balance-sheet"
    assert body["report"] == BS_REPORT
    assert body["identities"] == {"identity_ok": True}
    assert body["cross_check"] == {"diffs": [], "ok": True}
    generate.assert_awaited_once()
    assert generate.await_args.kwargs["as_at"] == date(2026, 3, 31)
    verify.assert_awaited_once()
    assert verify.await_args.args[2] == "balance-sheet"


def test_balance_sheet_defaults_as_at_to_today(client: TestClient) -> None:
    with (
        patch(
            "src.routes.formance_reports.generate_bs", new_callable=AsyncMock
        ) as generate,
        patch(
            "src.routes.formance_reports.verify_against_postgres",
            new_callable=AsyncMock,
        ) as verify,
    ):
        generate.return_value = BS_REPORT
        verify.return_value = []
        response = client.get("/api/v1/reports/formance/balance-sheet")

    assert response.status_code == 200
    assert generate.await_args.kwargs["as_at"] == date.today()


def test_balance_sheet_passes_fiscal_period_start_when_entity_resolves(
    client: TestClient,
) -> None:
    """Contract F24: with a resolved entity the balance sheet receives the
    fiscal-year-derived period_start for its profit-for-period line."""
    entity_id = uuid.uuid4()
    entity = MagicMock()
    entity.fiscal_year_start = "01-01"
    with (
        patch(
            "src.routes.formance_reports._resolve_scope",
            new_callable=AsyncMock,
        ) as resolve,
        patch(
            "src.routes.formance_reports.EntityService.get_entity",
            new_callable=AsyncMock,
        ) as get_entity,
        patch(
            "src.routes.formance_reports.generate_bs", new_callable=AsyncMock
        ) as generate,
        patch(
            "src.routes.formance_reports.verify_against_postgres",
            new_callable=AsyncMock,
        ) as verify,
    ):
        resolve.return_value = (entity_id, "entity-abc123")
        get_entity.return_value = entity
        generate.return_value = BS_REPORT
        verify.return_value = []
        response = client.get(
            "/api/v1/reports/formance/balance-sheet",
            params={"as_at": "2026-03-31"},
        )

    assert response.status_code == 200
    kwargs = generate.await_args.kwargs
    assert kwargs["as_at"] == date(2026, 3, 31)
    assert kwargs["entity_id"] == entity_id
    # "01-01" fiscal start for a window ending 2026-03-31 → 2026-01-01.
    assert kwargs["period_start"] == date(2026, 1, 1)
    get_entity.assert_awaited_once()


def test_balance_sheet_no_entity_passes_none_period_start(
    client: TestClient,
) -> None:
    """Contract F24: no entity scope → period_start None (inception) —
    the engine measures profit from the start of the ledger."""
    with (
        patch(
            "src.routes.formance_reports._resolve_scope",
            new_callable=AsyncMock,
        ) as resolve,
        patch(
            "src.routes.formance_reports.generate_bs", new_callable=AsyncMock
        ) as generate,
        patch(
            "src.routes.formance_reports.verify_against_postgres",
            new_callable=AsyncMock,
        ) as verify,
    ):
        resolve.return_value = (None, "main")
        generate.return_value = BS_REPORT
        verify.return_value = []
        response = client.get("/api/v1/reports/formance/balance-sheet")

    assert response.status_code == 200
    kwargs = generate.await_args.kwargs
    assert kwargs["entity_id"] is None
    assert kwargs["as_at"] == date.today()
    assert kwargs["period_start"] is None


def test_trial_balance_happy_path_with_as_at(client: TestClient) -> None:
    with (
        patch(
            "src.routes.formance_reports.generate_tb", new_callable=AsyncMock
        ) as generate,
        patch(
            "src.routes.formance_reports.verify_against_postgres",
            new_callable=AsyncMock,
        ) as verify,
    ):
        generate.return_value = TB_REPORT
        verify.return_value = []
        response = client.get(
            "/api/v1/reports/formance/trial-balance",
            params={"as_at": "2026-03-31"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["report_type"] == "trial-balance"
    assert body["report"] == TB_REPORT
    assert body["cross_check"] == {"diffs": [], "ok": True}
    generate.assert_awaited_once()
    assert generate.await_args.kwargs["as_at"] == date(2026, 3, 31)
    # Contract F24: the trial balance gets NO period_start (unaffected).
    assert "period_start" not in generate.await_args.kwargs
    verify.assert_awaited_once()
    assert verify.await_args.args[2] == "trial-balance"


def test_cross_check_diffs_mark_report_not_ok(client: TestClient) -> None:
    diffs = [
        {
            "report_type": "pl",
            "line": "revenue",
            "engine": 100100,
            "postgres": 100000,
            "delta": 100,
        }
    ]
    with (
        patch(
            "src.routes.formance_reports.generate_pl", new_callable=AsyncMock
        ) as generate,
        patch(
            "src.routes.formance_reports.verify_against_postgres",
            new_callable=AsyncMock,
        ) as verify,
    ):
        generate.return_value = PL_REPORT
        verify.return_value = diffs
        response = client.get(
            "/api/v1/reports/formance/pl",
            params={"period_start": "2026-01-01", "period_end": "2026-12-31"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["cross_check"] == {"diffs": diffs, "ok": False}


def test_cash_flow_cross_check_note_and_no_verify(client: TestClient) -> None:
    with (
        patch(
            "src.routes.formance_reports.generate_cash_flow",
            new_callable=AsyncMock,
        ) as generate,
        patch(
            "src.routes.formance_reports.verify_against_postgres",
            new_callable=AsyncMock,
        ) as verify,
    ):
        generate.return_value = CF_REPORT
        response = client.get(
            "/api/v1/reports/formance/cash-flow",
            params={"period_start": "2026-01-01", "period_end": "2026-12-31"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["report_type"] == "cash-flow"
    assert body["report"] == CF_REPORT
    assert body["cross_check"] == NO_PG_CROSS_CHECK
    generate.assert_awaited_once()
    verify.assert_not_awaited()


def test_funds_cross_check_note_and_no_verify(client: TestClient) -> None:
    with (
        patch(
            "src.routes.formance_reports.generate_funds", new_callable=AsyncMock
        ) as generate,
        patch(
            "src.routes.formance_reports.verify_against_postgres",
            new_callable=AsyncMock,
        ) as verify,
    ):
        generate.return_value = FUNDS_REPORT
        response = client.get(
            "/api/v1/reports/formance/funds",
            params={"period_start": "2026-01-01", "period_end": "2026-12-31"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["report_type"] == "funds"
    assert body["cross_check"] == NO_PG_CROSS_CHECK
    generate.assert_awaited_once()
    verify.assert_not_awaited()


# ---------------------------------------------------------------------------
# 422 mappings
# ---------------------------------------------------------------------------


def test_invalid_report_type_returns_422(client: TestClient) -> None:
    response = client.get("/api/v1/reports/formance/aged-receivable")

    assert response.status_code == 422
    body = response.json()
    assert body["type"] == "invalid_report_type"
    # Contract F30: the message lists the aging types automatically.
    assert "aged-receivables" in body["message"]
    assert "aged-payables" in body["message"]


@pytest.mark.parametrize(
    ("report_type", "generator_attr", "params", "expected_start", "expected_end"),
    [
        ("pl", "generate_pl", {}, "fiscal", "today"),
        ("pl", "generate_pl", {"period_start": "2026-01-01"}, "2026-01-01", "today"),
        ("cash-flow", "generate_cash_flow", {"period_end": "2026-12-31"}, "fiscal", "2026-12-31"),
        ("funds", "generate_funds", {}, "fiscal", "today"),
    ],
)
def test_flow_reports_default_to_fiscal_year_window(
    client: TestClient,
    report_type: str,
    generator_attr: str,
    params: dict[str, str],
    expected_start: str,
    expected_end: str,
) -> None:
    """Flow reports missing one or both dates default the MISSING value to
    the entity's fiscal year to date (UK 6-April convention with no
    entity) — never a 422. This is what the Books-in-plain-English pane
    relies on."""
    with (
        patch(
            f"src.routes.formance_reports.{generator_attr}", new_callable=AsyncMock
        ) as generate,
        patch(
            "src.routes.formance_reports.verify_against_postgres",
            new_callable=AsyncMock,
        ) as verify,
    ):
        generate.return_value = PL_REPORT
        verify.return_value = []
        response = client.get(f"/api/v1/reports/formance/{report_type}", params=params)

    assert response.status_code == 200
    kwargs = generate.await_args.kwargs
    fiscal_start = date(date.today().year, 4, 6)
    if fiscal_start > date.today():
        fiscal_start = date(date.today().year - 1, 4, 6)
    want_start = fiscal_start if expected_start == "fiscal" else date.fromisoformat(expected_start)
    want_end = date.today() if expected_end == "today" else date.fromisoformat(expected_end)
    assert kwargs["period_start"] == want_start
    assert kwargs["period_end"] == want_end


def test_report_engine_error_maps_to_422(client: TestClient) -> None:
    with (
        patch(
            "src.routes.formance_reports.generate_pl", new_callable=AsyncMock
        ) as generate,
        patch(
            "src.routes.formance_reports.verify_against_postgres",
            new_callable=AsyncMock,
        ) as verify,
    ):
        generate.side_effect = ReportEngineError(
            "P&L section misassembled: revenue assembled 1 != raw flow total 2"
        )
        response = client.get(
            "/api/v1/reports/formance/pl",
            params={"period_start": "2026-01-01", "period_end": "2026-12-31"},
        )

    assert response.status_code == 422
    body = response.json()
    assert body["type"] == "report_engine_error"
    assert "P&L section misassembled" in body["message"]
    verify.assert_not_awaited()


# ---------------------------------------------------------------------------
# Contract F37 — aged receivables / aged payables (ledger-derived engine)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("report_type", "generator_attr", "payload", "ledger_code", "expected_item"),
    [
        (
            "aged-receivables",
            "generate_ar_aging",
            AR_AGING,
            "1100",
            {
                "ref": "INV-2001",
                "date": "2026-02-01",
                "original_pence": 120000,
                "paid_pence": 50000,
                "outstanding_pence": 70000,
                "days_overdue": 58,
            },
        ),
        (
            "aged-payables",
            "generate_ap_aging",
            AP_AGING,
            "2000",
            {
                "ref": "INV-3001",
                "date": "2026-02-01",
                "original_pence": 108000,
                "paid_pence": 0,
                "outstanding_pence": 108000,
                "days_overdue": 58,
            },
        ),
    ],
)
def test_aging_report_shape(
    client: TestClient,
    report_type: str,
    generator_attr: str,
    payload: dict,
    ledger_code: str,
    expected_item: dict,
) -> None:
    """Contract F37: as_at only (no period params), the LEDGER-DERIVED engine
    generator feeds the F30-shaped response, and the old Postgres aging
    runners plus the engine dispatch are never reached."""
    with (
        patch(
            "src.routes.formance_reports._resolve_scope", new_callable=AsyncMock
        ) as resolve,
        patch(
            f"src.routes.formance_reports.{generator_attr}", new_callable=AsyncMock
        ) as generate,
        patch(
            "src.services.report_service.ReportService.run_ar_aging",
            new_callable=AsyncMock,
        ) as legacy_runner,
        patch(
            "src.routes.formance_reports.generate_pl", new_callable=AsyncMock
        ) as generate_pl,
    ):
        resolve.return_value = (None, "main")
        generate.return_value = payload
        response = client.get(
            f"/api/v1/reports/formance/{report_type}",
            params={"as_at": "2026-03-31"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["report_type"] == report_type
    assert body["as_at"] == "2026-03-31"
    assert set(body["report"]) == {
        "items",
        "buckets",
        "total_outstanding_pence",
        "ledger_balance",
        "reconciled",
        "note",
    }
    assert body["report"]["items"] == [expected_item]
    assert body["report"]["buckets"] == payload["buckets"]
    assert body["report"]["total_outstanding_pence"] == (
        payload["total_outstanding_pence"]
    )
    assert body["report"]["ledger_balance"] == payload["ledger_balance"]
    assert body["report"]["ledger_balance"]["code"] == ledger_code
    assert body["report"]["reconciled"] is True
    assert body["report"]["note"] == payload["note"]
    # The note no longer claims the aging is invoice-derived.
    assert "invoice-derived" not in body["report"]["note"]
    assert body["cross_check"] == AGING_CROSS_CHECK

    generate.assert_awaited_once()
    assert generate.await_args.args[1] == "main"
    assert generate.await_args.kwargs == {"as_at": AGING_AS_AT, "entity_id": None}
    legacy_runner.assert_not_awaited()
    generate_pl.assert_not_awaited()


def test_aging_entity_scope_reaches_generator(client: TestClient) -> None:
    entity_id = uuid.uuid4()
    with (
        patch(
            "src.routes.formance_reports._resolve_scope", new_callable=AsyncMock
        ) as resolve,
        patch(
            "src.routes.formance_reports.generate_ar_aging", new_callable=AsyncMock
        ) as generate,
    ):
        resolve.return_value = (entity_id, "entity-abc123")
        generate.return_value = AR_AGING
        response = client.get(
            "/api/v1/reports/formance/aged-receivables",
            params={"as_at": "2026-03-31"},
        )

    assert response.status_code == 200
    assert generate.await_args.args[1] == "entity-abc123"
    assert generate.await_args.kwargs == {
        "as_at": AGING_AS_AT,
        "entity_id": entity_id,
    }


def test_aging_engine_error_maps_to_422(client: TestClient) -> None:
    """A ledger that does not reconcile (the engine's TB-equality assert)
    surfaces as 422 — never a quietly wrong aging."""
    with (
        patch(
            "src.routes.formance_reports._resolve_scope", new_callable=AsyncMock
        ) as resolve,
        patch(
            "src.routes.formance_reports.generate_ap_aging", new_callable=AsyncMock
        ) as generate,
    ):
        resolve.return_value = (None, "main")
        generate.side_effect = ReportEngineError(
            "aged_payables does not reconcile to the trial balance: item "
            "outstanding 100 != |2000 balance| 200"
        )
        response = client.get(
            "/api/v1/reports/formance/aged-payables",
            params={"as_at": "2026-03-31"},
        )

    assert response.status_code == 422
    body = response.json()
    assert body["type"] == "report_engine_error"
    assert "does not reconcile" in body["message"]


def test_aging_defaults_as_at_to_today(client: TestClient) -> None:
    with (
        patch(
            "src.routes.formance_reports._resolve_scope", new_callable=AsyncMock
        ) as resolve,
        patch(
            "src.routes.formance_reports.generate_ar_aging", new_callable=AsyncMock
        ) as generate,
    ):
        resolve.return_value = (None, "main")
        generate.return_value = AR_AGING
        response = client.get("/api/v1/reports/formance/aged-receivables")

    assert response.status_code == 200
    assert response.json()["as_at"] == date.today().isoformat()
    assert generate.await_args.kwargs["as_at"] == date.today()
