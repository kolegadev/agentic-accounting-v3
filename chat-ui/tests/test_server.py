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
# Entities forwarding (Contract F20) — GET → /api/v1/entities, no mock
# ---------------------------------------------------------------------------


def test_entities_forwards_path_and_json(client: TestClient, upstream) -> None:
    resp = client.get("/api/entities")

    assert resp.status_code == 200
    echo = resp.json()
    assert echo["method"] == "GET"
    assert echo["path"] == "/api/v1/entities"
    req = upstream[0]
    assert req.method == "GET"
    assert req.url.path == "/api/v1/entities"


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
