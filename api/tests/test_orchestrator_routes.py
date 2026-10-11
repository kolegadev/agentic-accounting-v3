# /Users/johnpellew/agentic-accounting-v3/api/tests/test_orchestrator_routes.py

"""Contract F7 — API routes for the orchestrator (thin wrappers).

FastAPI TestClient against the accounting API with the orchestrator service
functions mocked (AsyncMock) at the route module seam.  No LLM, database,
or ledger is touched.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from src.index import app
from src.models.transaction import Posting, Transaction
from src.services.ledger_parity import ParityReport
from src.services.orchestrator import (
    OrchestrateResult,
    PostResult,
    PreviewLeg,
    RejectResult,
)
from src.services.transaction_service import (
    TransactionNotDraftError,
    TransactionNotFoundError,
    TransactionService,
    UnbalancedTransactionError,
)

NOW = datetime(2026, 10, 10, 12, 0, 0, tzinfo=UTC)
TX_ID = uuid.uuid4()


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def client() -> TestClient:
    """Return a FastAPI TestClient."""
    return TestClient(app)


def _legs() -> list[PreviewLeg]:
    return [
        PreviewLeg(
            code="5000",
            name="Office Expenses",
            account_class="Expense",
            debit_pence=10000,
            credit_pence=0,
            description="Printer paper",
        ),
        PreviewLeg(
            code="2000",
            name="Trade Creditors",
            account_class="Liability",
            debit_pence=0,
            credit_pence=10000,
            description="Printer paper",
        ),
    ]


def _draft_transaction() -> Transaction:
    tx_id = uuid.uuid4()
    tx = Transaction(
        id=tx_id,
        reference=None,
        description="Supplier invoice — test",
        currency="GBP",
        status="draft",
        effective_date=date(2026, 10, 1),
        total_amount=10000,
        created_at=NOW,
        updated_at=NOW,
    )
    tx.postings = [
        Posting(
            id=uuid.uuid4(),
            transaction_id=tx_id,
            account_id=uuid.uuid4(),
            debit_amount=10000,
            credit_amount=0,
            created_at=NOW,
        ),
        Posting(
            id=uuid.uuid4(),
            transaction_id=tx_id,
            account_id=uuid.uuid4(),
            debit_amount=0,
            credit_amount=10000,
            created_at=NOW,
        ),
    ]
    return tx


def _parity_ok() -> ParityReport:
    return ParityReport(ok=True, summary="Ledger parity OK: 1 transactions verified")


# ---------------------------------------------------------------------------
# POST /api/v1/orchestrator/message
# ---------------------------------------------------------------------------


def test_message_preview_returns_200_with_preview(client: TestClient) -> None:
    result = OrchestrateResult.preview(
        transaction_id=TX_ID,
        skill_id="supplier-invoice",
        legs=_legs(),
        total_pence=10000,
        formance_preview=[
            {"source": "2000", "destination": "5000", "amount": "100.00", "asset": "GBP/2"}
        ],
    )
    with patch(
        "src.routes.orchestrator.orchestrate_message", new_callable=AsyncMock
    ) as mock:
        mock.return_value = result
        response = client.post(
            "/api/v1/orchestrator/message",
            json={"message": "Supplier invoice for £100"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "preview"
    preview = body["preview"]
    assert preview["transaction_id"] == str(TX_ID)
    assert preview["skill_id"] == "supplier-invoice"
    assert preview["total_pence"] == 10000
    assert len(preview["legs"]) == 2
    assert preview["legs"][0]["code"] == "5000"
    assert preview["legs"][0]["name"] == "Office Expenses"
    assert preview["legs"][0]["account_class"] == "Expense"
    assert preview["legs"][0]["debit_pence"] == 10000
    assert preview["legs"][0]["credit_pence"] == 0
    assert preview["legs"][1]["credit_pence"] == 10000
    assert preview["formance_preview"][0]["amount"] == "100.00"
    mock.assert_awaited_once()
    assert mock.await_args.args[1] == "Supplier invoice for £100"


def test_message_preview_forwards_contact(client: TestClient) -> None:
    contact = {
        "id": str(uuid.uuid4()),
        "name": "Brand New Customer Ltd",
        "created": True,
    }
    result = OrchestrateResult.preview(
        transaction_id=TX_ID,
        skill_id="sales-invoice",
        legs=_legs(),
        total_pence=10000,
        formance_preview=[],
        contact=contact,
    )
    with patch(
        "src.routes.orchestrator.orchestrate_message", new_callable=AsyncMock
    ) as mock:
        mock.return_value = result
        response = client.post(
            "/api/v1/orchestrator/message",
            json={"message": "Invoice the new customer"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "preview"
    assert body["preview"]["contact"] == contact


def test_message_answer_returns_200_with_data(client: TestClient) -> None:
    result = OrchestrateResult.answer(
        "trial_balance",
        {
            "trial_balance": [
                {
                    "code": "1000",
                    "name": "Business Current",
                    "class": "Asset (bank)",
                    "balance": 10000,
                    "debit": 10000,
                    "credit": 0,
                }
            ],
            "total_debits": 10000,
            "total_credits": 10000,
            "as_at": "2026-10-10",
        },
    )
    with patch(
        "src.routes.orchestrator.orchestrate_message", new_callable=AsyncMock
    ) as mock:
        mock.return_value = result
        response = client.post(
            "/api/v1/orchestrator/message",
            json={"message": "what is my trial balance"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "answer"
    assert body["query_type"] == "trial_balance"
    assert body["data"]["total_debits"] == 10000
    assert body["contact"] is None
    mock.assert_awaited_once()


def test_message_passes_history_to_orchestrator(client: TestClient) -> None:
    history = [{"role": "user", "content": "prior message"}]
    result = OrchestrateResult.failure("clarification", "Which bank account?")
    with patch(
        "src.routes.orchestrator.orchestrate_message", new_callable=AsyncMock
    ) as mock:
        mock.return_value = result
        client.post(
            "/api/v1/orchestrator/message",
            json={"message": "pay the supplier", "history": history, "session_id": "s-1"},
        )
    assert mock.await_args.kwargs["history"] == history


def test_message_clarification_returns_200(client: TestClient) -> None:
    result = OrchestrateResult.failure("clarification", "Which bank account should the money leave?")
    with patch(
        "src.routes.orchestrator.orchestrate_message", new_callable=AsyncMock
    ) as mock:
        mock.return_value = result
        response = client.post(
            "/api/v1/orchestrator/message", json={"message": "pay the supplier"}
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "clarification"
    assert body["error"]["type"] == "clarification"
    assert "bank account" in body["error"]["message"]


def test_message_structured_error_maps_to_422(client: TestClient) -> None:
    result = OrchestrateResult.failure("llm_unreachable", "LLM API error: connection refused")
    with patch(
        "src.routes.orchestrator.orchestrate_message", new_callable=AsyncMock
    ) as mock:
        mock.return_value = result
        response = client.post(
            "/api/v1/orchestrator/message", json={"message": "record an expense"}
        )

    assert response.status_code == 422
    body = response.json()
    assert body["type"] == "llm_unreachable"
    assert "connection refused" in body["message"]


def test_message_error_without_payload_still_maps_to_422(client: TestClient) -> None:
    result = OrchestrateResult(status="error")
    with patch(
        "src.routes.orchestrator.orchestrate_message", new_callable=AsyncMock
    ) as mock:
        mock.return_value = result
        response = client.post(
            "/api/v1/orchestrator/message", json={"message": "anything"}
        )

    assert response.status_code == 422
    body = response.json()
    assert body["type"] == "invalid_response"


def test_message_missing_field_is_422_validation(client: TestClient) -> None:
    response = client.post("/api/v1/orchestrator/message", json={})
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# POST /api/v1/orchestrator/approve
# ---------------------------------------------------------------------------


def test_approve_posts_and_returns_parity_fields(client: TestClient) -> None:
    tx = _draft_transaction()
    tx.status = "posted"
    tx.reference = "JE-2026-0001"
    tx_model = TransactionService._transaction_to_response(tx)
    result = PostResult(transaction=tx_model, parity_ok=True, parity_report=_parity_ok())
    with patch(
        "src.routes.orchestrator.approve_entry", new_callable=AsyncMock
    ) as mock:
        mock.return_value = result
        response = client.post(
            "/api/v1/orchestrator/approve", json={"transaction_id": str(TX_ID)}
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "posted"
    assert body["parity_ok"] is True
    assert body["parity_report"]["ok"] is True
    assert body["transaction"]["reference"] == "JE-2026-0001"
    assert body["transaction"]["status"] == "posted"
    assert len(body["transaction"]["postings"]) == 2
    mock.assert_awaited_once()
    assert mock.await_args.args[1] == TX_ID


def test_approve_parity_drift_still_returns_200(client: TestClient) -> None:
    tx = _draft_transaction()
    tx.status = "posted"
    tx_model = TransactionService._transaction_to_response(tx)
    drift = ParityReport(
        ok=False,
        missing_in_formance=[{"kind": "missing_mirror", "transaction_id": str(TX_ID)}],
        summary="Ledger parity FAILED: 1 missing",
    )
    result = PostResult(transaction=tx_model, parity_ok=False, parity_report=drift)
    with patch(
        "src.routes.orchestrator.approve_entry", new_callable=AsyncMock
    ) as mock:
        mock.return_value = result
        response = client.post(
            "/api/v1/orchestrator/approve", json={"transaction_id": str(TX_ID)}
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "posted"
    assert body["parity_ok"] is False
    assert len(body["parity_report"]["missing_in_formance"]) == 1


def test_approve_missing_transaction_returns_404(client: TestClient) -> None:
    with patch(
        "src.routes.orchestrator.approve_entry", new_callable=AsyncMock
    ) as mock:
        mock.side_effect = TransactionNotFoundError(TX_ID)
        response = client.post(
            "/api/v1/orchestrator/approve", json={"transaction_id": str(TX_ID)}
        )

    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


def test_approve_not_draft_returns_422(client: TestClient) -> None:
    with patch(
        "src.routes.orchestrator.approve_entry", new_callable=AsyncMock
    ) as mock:
        mock.side_effect = TransactionNotDraftError(TX_ID, "posted")
        response = client.post(
            "/api/v1/orchestrator/approve", json={"transaction_id": str(TX_ID)}
        )

    assert response.status_code == 422
    assert "draft" in response.json()["detail"]


def test_approve_unbalanced_returns_422(client: TestClient) -> None:
    with patch(
        "src.routes.orchestrator.approve_entry", new_callable=AsyncMock
    ) as mock:
        mock.side_effect = UnbalancedTransactionError(10000, 9000)
        response = client.post(
            "/api/v1/orchestrator/approve", json={"transaction_id": str(TX_ID)}
        )

    assert response.status_code == 422
    assert "unbalanced" in response.json()["detail"]


def test_approve_invalid_transaction_id_is_422_validation(client: TestClient) -> None:
    response = client.post(
        "/api/v1/orchestrator/approve", json={"transaction_id": "not-a-uuid"}
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# POST /api/v1/orchestrator/reject
# ---------------------------------------------------------------------------


def test_reject_discards_draft(client: TestClient) -> None:
    result = RejectResult(transaction_id=TX_ID)
    with patch(
        "src.routes.orchestrator.reject_entry", new_callable=AsyncMock
    ) as mock:
        mock.return_value = result
        response = client.post(
            "/api/v1/orchestrator/reject", json={"transaction_id": str(TX_ID)}
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "discarded"
    assert body["transaction_id"] == str(TX_ID)
    mock.assert_awaited_once()
    assert mock.await_args.args[1] == TX_ID


def test_reject_missing_transaction_returns_404(client: TestClient) -> None:
    with patch(
        "src.routes.orchestrator.reject_entry", new_callable=AsyncMock
    ) as mock:
        mock.side_effect = TransactionNotFoundError(TX_ID)
        response = client.post(
            "/api/v1/orchestrator/reject", json={"transaction_id": str(TX_ID)}
        )

    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


def test_reject_not_draft_returns_422(client: TestClient) -> None:
    with patch(
        "src.routes.orchestrator.reject_entry", new_callable=AsyncMock
    ) as mock:
        mock.side_effect = TransactionNotDraftError(TX_ID, "posted")
        response = client.post(
            "/api/v1/orchestrator/reject", json={"transaction_id": str(TX_ID)}
        )

    assert response.status_code == 422
    assert "draft" in response.json()["detail"]


# ---------------------------------------------------------------------------
# GET /api/v1/orchestrator/pending
# ---------------------------------------------------------------------------


def test_pending_lists_drafts_with_legs_newest_first(client: TestClient) -> None:
    older = _draft_transaction()
    newer = _draft_transaction()
    with patch(
        "src.routes.orchestrator.TransactionService.list_transactions",
        new_callable=AsyncMock,
    ) as mock:
        mock.return_value = ([newer, older], 2)
        response = client.get("/api/v1/orchestrator/pending")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert len(body["transactions"]) == 2
    assert body["transactions"][0]["id"] == str(newer.id)
    assert body["transactions"][0]["status"] == "draft"
    assert len(body["transactions"][0]["postings"]) == 2
    assert body["transactions"][0]["postings"][0]["debit_amount"] == 10000
    mock.assert_awaited_once()
    assert mock.await_args.kwargs["status"] == "draft"


def test_pending_empty_returns_empty_list(client: TestClient) -> None:
    with patch(
        "src.routes.orchestrator.TransactionService.list_transactions",
        new_callable=AsyncMock,
    ) as mock:
        mock.return_value = ([], 0)
        response = client.get("/api/v1/orchestrator/pending")

    assert response.status_code == 200
    body = response.json()
    assert body == {"transactions": [], "total": 0}
