# /Users/johnpellew/agentic-accounting-v3/chat-ui/tests/test_server.py

"""Contract F7 — chat-ui server forwarding endpoints.

FastAPI TestClient against the chat-ui app with the upstream httpx client
replaced by a MockTransport at the module seam (server._upstream_client).
No live accounting API is needed: the mock transport records each request
and returns canned responses, and the forwarding endpoints must pass the
status code and JSON straight through — with a 502 when upstream fails.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

# chat-ui/server.py is a plain module next to this repo's chat-ui directory.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
import pytest
from fastapi.testclient import TestClient

import server

MESSAGE_BODY = {"message": "Supplier invoice INV-1 for £120"}
TX_BODY = {"transaction_id": "8f0b3c9a-1111-4a2b-9c3d-000000000001"}


@pytest.fixture
def client() -> TestClient:
    """Return a FastAPI TestClient for the chat-ui app."""
    return TestClient(server.app)


@pytest.fixture
def upstream(monkeypatch):
    """Replace the upstream client with a MockTransport recording requests.

    Returns the captured requests list.
    """
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "ok": True,
                "method": request.method,
                "path": request.url.path,
                "body": request.content.decode(),
            },
        )

    monkeypatch.setattr(
        server,
        "_upstream_client",
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return captured


def _set_transport(monkeypatch, handler) -> None:
    """Point the seam at a MockTransport with the given handler."""
    monkeypatch.setattr(
        server,
        "_upstream_client",
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


# ---------------------------------------------------------------------------
# Forwarding — path / method / body / status / JSON pass-through
# ---------------------------------------------------------------------------


def test_message_forwards_path_body_and_json(client: TestClient, upstream) -> None:
    resp = client.post("/api/orchestrator/message", json=MESSAGE_BODY)

    assert resp.status_code == 200
    echo = resp.json()
    assert echo["method"] == "POST"
    assert echo["path"] == "/api/v1/orchestrator/message"
    assert json.loads(echo["body"]) == MESSAGE_BODY
    req = upstream[0]
    assert req.method == "POST"
    assert req.url.path == "/api/v1/orchestrator/message"
    assert json.loads(req.content) == MESSAGE_BODY


def test_approve_forwards_path_and_body(client: TestClient, upstream) -> None:
    resp = client.post("/api/orchestrator/approve", json=TX_BODY)

    assert resp.status_code == 200
    echo = resp.json()
    assert echo["method"] == "POST"
    assert echo["path"] == "/api/v1/orchestrator/approve"
    assert json.loads(echo["body"]) == TX_BODY
    assert upstream[0].url.path == "/api/v1/orchestrator/approve"


def test_reject_forwards_path_and_body(client: TestClient, upstream) -> None:
    resp = client.post("/api/orchestrator/reject", json=TX_BODY)

    assert resp.status_code == 200
    echo = resp.json()
    assert echo["method"] == "POST"
    assert echo["path"] == "/api/v1/orchestrator/reject"
    assert json.loads(echo["body"]) == TX_BODY
    assert upstream[0].url.path == "/api/v1/orchestrator/reject"


def test_pending_forwards_get_without_body(client: TestClient, upstream) -> None:
    resp = client.get("/api/orchestrator/pending")

    assert resp.status_code == 200
    echo = resp.json()
    assert echo["method"] == "GET"
    assert echo["path"] == "/api/v1/orchestrator/pending"
    assert echo["body"] == ""
    req = upstream[0]
    assert req.method == "GET"
    assert req.url.path == "/api/v1/orchestrator/pending"


def test_pending_forwards_entity_id_query_param(
    client: TestClient, upstream
) -> None:
    """Contract F20: the browser's ?entity_id= passes through to the
    upstream orchestrator route untouched."""
    resp = client.get(
        "/api/orchestrator/pending",
        params={"entity_id": "8f0b3c9a-2222-4a2b-9c3d-000000000002"},
    )

    assert resp.status_code == 200
    assert upstream[0].url.path == "/api/v1/orchestrator/pending"
    assert (
        upstream[0].url.params.get("entity_id")
        == "8f0b3c9a-2222-4a2b-9c3d-000000000002"
    )


def test_approve_forwards_entity_id_query_param(
    client: TestClient, upstream
) -> None:
    resp = client.post(
        "/api/orchestrator/approve",
        json=TX_BODY,
        params={"entity_id": "8f0b3c9a-3333-4a2b-9c3d-000000000003"},
    )

    assert resp.status_code == 200
    assert upstream[0].url.path == "/api/v1/orchestrator/approve"
    assert (
        upstream[0].url.params.get("entity_id")
        == "8f0b3c9a-3333-4a2b-9c3d-000000000003"
    )
    assert json.loads(upstream[0].content) == TX_BODY


# ---------------------------------------------------------------------------
# Pass-through of upstream error status + JSON (no mock fallback)
# ---------------------------------------------------------------------------


def test_upstream_error_status_and_json_passthrough(client: TestClient, monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422, json={"type": "invalid_facts", "message": "vat_rate must be an enum"}
        )

    _set_transport(monkeypatch, handler)
    resp = client.post("/api/orchestrator/message", json=MESSAGE_BODY)

    assert resp.status_code == 422
    assert resp.json() == {"type": "invalid_facts", "message": "vat_rate must be an enum"}


def test_upstream_not_found_passthrough(client: TestClient, monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "Transaction not found"})

    _set_transport(monkeypatch, handler)
    resp = client.post("/api/orchestrator/approve", json=TX_BODY)

    assert resp.status_code == 404
    assert resp.json() == {"detail": "Transaction not found"}


def test_upstream_non_json_body_wrapped_as_detail(client: TestClient, monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"plain upstream text")

    _set_transport(monkeypatch, handler)
    resp = client.get("/api/orchestrator/pending")

    assert resp.status_code == 200
    assert resp.json() == {"detail": "plain upstream text"}


# ---------------------------------------------------------------------------
# Upstream failure → 502 JSON (never a mock fallback)
# ---------------------------------------------------------------------------


def test_message_upstream_unreachable_returns_502(client: TestClient, monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    _set_transport(monkeypatch, handler)
    resp = client.post("/api/orchestrator/message", json=MESSAGE_BODY)

    assert resp.status_code == 502
    body = resp.json()
    assert body["status"] == "error"
    assert body["type"] == "upstream_unreachable"
    assert "connection refused" in body["message"]


def test_pending_upstream_unreachable_returns_502(client: TestClient, monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    _set_transport(monkeypatch, handler)
    resp = client.get("/api/orchestrator/pending")

    assert resp.status_code == 502
    body = resp.json()
    assert body["type"] == "upstream_unreachable"
    assert "no route to host" in body["message"]


# ---------------------------------------------------------------------------
# Reports forwarding (Contract F10) — GET with query-param pass-through
# ---------------------------------------------------------------------------


def test_reports_forwards_path_and_query_params(client: TestClient, upstream) -> None:
    resp = client.get(
        "/api/reports/formance/pl",
        params={"period_start": "2026-01-01", "period_end": "2026-12-31"},
    )

    assert resp.status_code == 200
    echo = resp.json()
    assert echo["method"] == "GET"
    assert echo["path"] == "/api/v1/reports/formance/pl"
    req = upstream[0]
    assert req.method == "GET"
    assert req.url.path == "/api/v1/reports/formance/pl"
    assert req.url.params.get("period_start") == "2026-01-01"
    assert req.url.params.get("period_end") == "2026-12-31"


def test_reports_forwards_balance_sheet_as_at(client: TestClient, upstream) -> None:
    resp = client.get(
        "/api/reports/formance/balance-sheet", params={"as_at": "2026-03-31"}
    )

    assert resp.status_code == 200
    echo = resp.json()
    assert echo["path"] == "/api/v1/reports/formance/balance-sheet"
    assert upstream[0].url.params.get("as_at") == "2026-03-31"


def test_reports_no_query_params_passthrough(client: TestClient, upstream) -> None:
    resp = client.get("/api/reports/formance/trial-balance")

    assert resp.status_code == 200
    assert upstream[0].url.path == "/api/v1/reports/formance/trial-balance"
    assert dict(upstream[0].url.params) == {}


def test_reports_upstream_error_status_and_json_passthrough(
    client: TestClient, monkeypatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422, json={"type": "missing_period_params", "message": "requires period_start"}
        )

    _set_transport(monkeypatch, handler)
    resp = client.get("/api/reports/formance/pl")

    assert resp.status_code == 422
    assert resp.json() == {
        "type": "missing_period_params",
        "message": "requires period_start",
    }


def test_reports_upstream_unreachable_returns_502(
    client: TestClient, monkeypatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    _set_transport(monkeypatch, handler)
    resp = client.get(
        "/api/reports/formance/cash-flow",
        params={"period_start": "2026-01-01", "period_end": "2026-12-31"},
    )

    assert resp.status_code == 502
    body = resp.json()
    assert body["status"] == "error"
    assert body["type"] == "upstream_unreachable"
    assert "connection refused" in body["message"]


# ---------------------------------------------------------------------------
# Entities forwarding (Contract F20/F23) — GET → /api/v1/entities/ (WITH the
# trailing slash — the slash-less URL 307-redirects and returned an empty
# body), no mock fallback.
# ---------------------------------------------------------------------------


def test_entities_forwards_path_and_json(client: TestClient, upstream) -> None:
    resp = client.get("/api/entities")

    assert resp.status_code == 200
    echo = resp.json()
    assert echo["method"] == "GET"
    assert echo["path"] == "/api/v1/entities/"
    req = upstream[0]
    assert req.method == "GET"
    assert req.url.path == "/api/v1/entities/"


def test_entities_upstream_error_status_and_json_passthrough(
    client: TestClient, monkeypatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500, json={"type": "internal", "message": "entities table missing"}
        )

    _set_transport(monkeypatch, handler)
    resp = client.get("/api/entities")

    assert resp.status_code == 500
    assert resp.json() == {"type": "internal", "message": "entities table missing"}


def test_entities_upstream_unreachable_returns_502(
    client: TestClient, monkeypatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    _set_transport(monkeypatch, handler)
    resp = client.get("/api/entities")

    assert resp.status_code == 502
    body = resp.json()
    assert body["status"] == "error"
    assert body["type"] == "upstream_unreachable"
    assert "connection refused" in body["message"]
    # NO mock fallback — an unreachable backend is a hard 502, never
    # fabricated company/entity data.
    assert "companies" not in body


# ---------------------------------------------------------------------------
# Books in plain English (Contract F23) — GET /api/books fetches the REAL
# Formance reports for the entity, then an LLM explanation.  entity_id is
# forwarded on BOTH upstream calls; upstream failure → 502
# upstream_unreachable; LLM failure → 502 llm_error.  No mock fallback.
# ---------------------------------------------------------------------------

BOOKS_ENTITY_ID = "8f0b3c9a-4444-4a2b-9c3d-000000000004"

PL_BODY = {
    "report_type": "pl",
    "report": {"sections": {"revenue": []}, "profit": 120000},
}
BS_BODY = {
    "report_type": "balance-sheet",
    "report": {
        "totals": {"assets": 500000, "liabilities_and_equity": 500000}
    },
}


def _books_handler(captured: list) -> callable:
    """MockTransport handler serving the pl + balance-sheet reports."""

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.path.endswith("/pl"):
            return httpx.Response(200, json=PL_BODY)
        if request.url.path.endswith("/balance-sheet"):
            return httpx.Response(200, json=BS_BODY)
        return httpx.Response(404, json={"detail": "not found"})

    return handler


def test_books_forwards_entity_id_and_returns_llm_markdown(
    client: TestClient, monkeypatch
) -> None:
    captured: list[httpx.Request] = []
    _set_transport(monkeypatch, _books_handler(captured))
    monkeypatch.setattr(
        server,
        "_llm_explain_books",
        AsyncMock(return_value="**Healthy** business.\n- Revenue is up"),
    )

    resp = client.get("/api/books", params={"entity_id": BOOKS_ENTITY_ID})

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["markdown"] == "**Healthy** business.\n- Revenue is up"
    assert "pl" in body["sources"]
    assert "balance_sheet" in body["sources"]

    paths = [r.url.path for r in captured]
    assert "/api/v1/reports/formance/pl" in paths
    assert "/api/v1/reports/formance/balance-sheet" in paths
    assert len(captured) == 2
    for req in captured:
        assert req.url.params.get("entity_id") == BOOKS_ENTITY_ID


def test_books_llm_receives_the_real_report_bodies(
    client: TestClient, monkeypatch
) -> None:
    _set_transport(monkeypatch, _books_handler([]))
    mock_llm = AsyncMock(return_value="fine")
    monkeypatch.setattr(server, "_llm_explain_books", mock_llm)

    resp = client.get("/api/books", params={"entity_id": BOOKS_ENTITY_ID})

    assert resp.status_code == 200
    assert mock_llm.await_count == 1
    pl_arg, bs_arg = mock_llm.await_args.args
    assert pl_arg == PL_BODY
    assert bs_arg == BS_BODY


def test_books_upstream_unreachable_returns_502(
    client: TestClient, monkeypatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    _set_transport(monkeypatch, handler)
    resp = client.get("/api/books", params={"entity_id": BOOKS_ENTITY_ID})

    assert resp.status_code == 502
    body = resp.json()
    assert body["status"] == "error"
    assert body["type"] == "upstream_unreachable"
    assert "connection refused" in body["message"]
    # NO mock fallback — the response carries no fabricated numbers.
    assert "markdown" not in body


def test_books_upstream_report_error_returns_502(
    client: TestClient, monkeypatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422, json={"type": "missing_period_params", "message": "requires period_start"}
        )

    _set_transport(monkeypatch, handler)
    resp = client.get("/api/books", params={"entity_id": BOOKS_ENTITY_ID})

    assert resp.status_code == 502
    body = resp.json()
    assert body["status"] == "error"
    assert body["type"] == "upstream_unreachable"
    assert "pl=422" in body["message"]


def test_books_llm_failure_returns_llm_error_502(
    client: TestClient, monkeypatch
) -> None:
    _set_transport(monkeypatch, _books_handler([]))
    monkeypatch.setattr(
        server,
        "_llm_explain_books",
        AsyncMock(side_effect=server._BooksLLMError("LLM API error: boom")),
    )

    resp = client.get("/api/books", params={"entity_id": BOOKS_ENTITY_ID})

    assert resp.status_code == 502
    body = resp.json()
    assert body["status"] == "error"
    assert body["type"] == "llm_error"
    assert "boom" in body["message"]


def test_books_missing_entity_id_returns_422(client: TestClient) -> None:
    resp = client.get("/api/books")

    assert resp.status_code == 422
    body = resp.json()
    assert body["status"] == "error"
    assert body["type"] == "missing_entity_id"
