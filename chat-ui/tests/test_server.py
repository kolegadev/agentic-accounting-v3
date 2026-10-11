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
import logging
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


# ---------------------------------------------------------------------------
# Admin forwarding (Contract F27) — owner token injection + proxies for
# entity creation/update, global contacts and the users list.
#
# The owner logs in ONCE (lazy, cached); the token rides as the
# Authorization header on the admin-proxied calls.  Contacts forward
# WITHOUT a token.  A failed login degrades to empty headers (the upstream
# 401 passes through).  Transport failure → 502 upstream_unreachable.
# ---------------------------------------------------------------------------

ADMIN_ENTITY_BODY = {
    "name": "Acme Holdings Ltd",
    "legal_type": "limited_company",
    "framework": "uk-gaap",
    "fiscal_year_start": "04-06",
    "vat_registered": True,
    "vat_number": "GB123456789",
    "vat_scheme": "standard",
    "setup": {
        "template_name": "uk_limited_company_vat",
        "bank_accounts": [{"name": "Barclays", "opening_balance": 300000}],
        "opening_balances": {
            "opening_date": "2026-04-06",
            "equity_account_code": "3000",
            "assets": [{"code": "1000", "balance_pence": 300000}],
        },
    },
}

ENTITY_PATCH_ID = "8f0b3c9a-5555-4a2b-9c3d-000000000005"


@pytest.fixture
def admin_upstream(monkeypatch):
    """Seam transport serving the owner login + echoing admin-proxied calls.

    Resets the module-level login cache so each test starts cold.
    """
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.path == "/api/v1/auth/login":
            return httpx.Response(
                200, json={"access_token": "tok-owner-1", "user": {}}
            )
        return httpx.Response(
            200,
            json={
                "ok": True,
                "method": request.method,
                "path": request.url.path,
                "auth": request.headers.get("authorization", ""),
                "body": request.content.decode(),
            },
        )

    monkeypatch.setattr(server, "_admin_token", None)
    monkeypatch.setattr(server, "_admin_login_tried", False)
    monkeypatch.setattr(
        server,
        "_upstream_client",
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return captured


def _reset_admin_login(monkeypatch) -> None:
    monkeypatch.setattr(server, "_admin_token", None)
    monkeypatch.setattr(server, "_admin_login_tried", False)


def test_owner_login_uses_default_demo_credentials() -> None:
    """The demo defaults are wired: john@kolega.ai / TestAccounting123!."""
    assert server.OWNER_EMAIL == "john@kolega.ai"
    assert server.OWNER_PASSWORD == "TestAccounting123!"


def test_owner_login_uses_env_credentials(
    client: TestClient, admin_upstream, monkeypatch
) -> None:
    """The login call carries OWNER_EMAIL/OWNER_PASSWORD from env."""
    monkeypatch.setattr(server, "OWNER_EMAIL", "owner@example.com")
    monkeypatch.setattr(server, "OWNER_PASSWORD", "s3cret-pass")
    client.get("/api/users")

    logins = [r for r in admin_upstream if r.url.path == "/api/v1/auth/login"]
    assert len(logins) == 1
    assert json.loads(logins[0].content) == {
        "email": "owner@example.com",
        "password": "s3cret-pass",
    }


def test_post_entities_forwards_body_and_admin_header(
    client: TestClient, admin_upstream
) -> None:
    resp = client.post("/api/entities", json=ADMIN_ENTITY_BODY)

    assert resp.status_code == 200
    reqs = [r for r in admin_upstream if r.url.path == "/api/v1/entities/"]
    assert len(reqs) == 1
    req = reqs[0]
    assert req.method == "POST"
    assert json.loads(req.content) == ADMIN_ENTITY_BODY
    assert req.headers.get("authorization") == "Bearer tok-owner-1"


def test_patch_entities_forwards_body_and_admin_header(
    client: TestClient, admin_upstream
) -> None:
    patch_body = {"name": "Acme Renamed Ltd"}
    resp = client.patch(f"/api/entities/{ENTITY_PATCH_ID}", json=patch_body)

    assert resp.status_code == 200
    reqs = [
        r
        for r in admin_upstream
        if r.url.path == f"/api/v1/entities/{ENTITY_PATCH_ID}"
    ]
    assert len(reqs) == 1
    req = reqs[0]
    assert req.method == "PATCH"
    assert json.loads(req.content) == patch_body
    assert req.headers.get("authorization") == "Bearer tok-owner-1"


def test_post_entities_forwards_query_string(
    client: TestClient, admin_upstream
) -> None:
    resp = client.post(
        "/api/entities", json=ADMIN_ENTITY_BODY, params={"dry_run": "1"}
    )

    assert resp.status_code == 200
    reqs = [r for r in admin_upstream if r.url.path == "/api/v1/entities/"]
    assert len(reqs) == 1
    assert reqs[0].url.params.get("dry_run") == "1"


def test_get_contacts_forwards_without_token(
    client: TestClient, admin_upstream
) -> None:
    resp = client.get("/api/contacts")

    assert resp.status_code == 200
    reqs = [r for r in admin_upstream if r.url.path == "/api/v1/contacts/"]
    assert len(reqs) == 1
    assert reqs[0].method == "GET"
    assert "authorization" not in reqs[0].headers
    # No owner login happens for the unauthenticated contacts proxy.
    logins = [r for r in admin_upstream if r.url.path == "/api/v1/auth/login"]
    assert logins == []


def test_post_contacts_forwards_body_without_token(
    client: TestClient, admin_upstream
) -> None:
    body = {"name": "Acme Corp", "type": "customer", "email": "ap@acme.test"}
    resp = client.post("/api/contacts", json=body)

    assert resp.status_code == 200
    reqs = [r for r in admin_upstream if r.url.path == "/api/v1/contacts/"]
    assert len(reqs) == 1
    assert reqs[0].method == "POST"
    assert json.loads(reqs[0].content) == body
    assert "authorization" not in reqs[0].headers


def test_get_users_forwards_admin_header(client: TestClient, admin_upstream) -> None:
    resp = client.get("/api/users")

    assert resp.status_code == 200
    reqs = [r for r in admin_upstream if r.url.path == "/api/v1/auth/users"]
    assert len(reqs) == 1
    assert reqs[0].method == "GET"
    assert reqs[0].headers.get("authorization") == "Bearer tok-owner-1"


def test_owner_login_called_once_across_requests(
    client: TestClient, admin_upstream
) -> None:
    """F27: the token is fetched ONCE and cached across admin calls."""
    client.post("/api/entities", json=ADMIN_ENTITY_BODY)
    client.get("/api/users")
    client.patch(f"/api/entities/{ENTITY_PATCH_ID}", json={"name": "X Ltd"})

    logins = [r for r in admin_upstream if r.url.path == "/api/v1/auth/login"]
    assert len(logins) == 1


def test_failed_owner_login_degrades_to_empty_headers(
    client: TestClient, monkeypatch, caplog
) -> None:
    """A failed login logs a warning and the admin call forwards WITHOUT a
    token — the upstream 401 passes through to the UI."""
    _reset_admin_login(monkeypatch)
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.path == "/api/v1/auth/login":
            return httpx.Response(401, json={"detail": "Invalid credentials"})
        return httpx.Response(401, json={"detail": "Not authenticated"})

    _set_transport(monkeypatch, handler)
    with caplog.at_level(logging.WARNING):
        resp = client.post("/api/entities", json=ADMIN_ENTITY_BODY)

    assert resp.status_code == 401
    assert resp.json() == {"detail": "Not authenticated"}
    entity_reqs = [r for r in captured if r.url.path == "/api/v1/entities/"]
    assert len(entity_reqs) == 1
    assert "authorization" not in entity_reqs[0].headers
    assert any("Owner login failed" in r.message for r in caplog.records)


def test_admin_proxy_upstream_unreachable_returns_502(
    client: TestClient, monkeypatch
) -> None:
    _reset_admin_login(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    _set_transport(monkeypatch, handler)
    resp = client.post("/api/entities", json=ADMIN_ENTITY_BODY)

    assert resp.status_code == 502
    body = resp.json()
    assert body["status"] == "error"
    assert body["type"] == "upstream_unreachable"
    assert "connection refused" in body["message"]


def test_contacts_proxy_upstream_unreachable_returns_502(
    client: TestClient, monkeypatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    _set_transport(monkeypatch, handler)
    resp = client.get("/api/contacts")

    assert resp.status_code == 502
    body = resp.json()
    assert body["status"] == "error"
    assert body["type"] == "upstream_unreachable"
    assert "no route to host" in body["message"]


def test_admin_proxy_upstream_error_passthrough(
    client: TestClient, monkeypatch
) -> None:
    """Upstream 409/422 JSON passes through untouched (no mock fallback)."""
    _reset_admin_login(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/auth/login":
            return httpx.Response(200, json={"access_token": "tok-owner-1"})
        return httpx.Response(
            409, json={"type": "duplicate_name", "message": "already exists"}
        )

    _set_transport(monkeypatch, handler)
    resp = client.post("/api/entities", json=ADMIN_ENTITY_BODY)

    assert resp.status_code == 409
    assert resp.json() == {"type": "duplicate_name", "message": "already exists"}


def test_books_pounds_summary_converts_pence_to_pounds_exactly() -> None:
    """The books LLM must never see pence: every amount becomes a
    two-decimal pounds string (the pence-as-pounds display bug)."""
    pl = {
        "report": {
            "period": {"start": "2026-04-06", "end": "2026-10-11"},
            "profit": -5000,
            "totals": {"revenue": 0, "expenses": 5000},
            "sections": {
                "expenses": [{"name": "Office Supplies", "amount": 5000}]
            },
        }
    }
    bs = {
        "report": {
            "as_at": "2026-10-11",
            "vat_net": -1000,
            "totals": {
                "assets": 1000000,
                "liabilities": 5000,
                "equity": 995000,
                "liabilities_and_equity": 1000000,
            },
            "sections": {
                "bank": [{"name": "Bank Current Account", "code": "1000", "amount": 1000000}]
            },
        }
    }
    summary = server._books_pounds_summary(pl, bs)
    assert summary["profit_or_loss_pounds"] == "-50.00"
    assert summary["revenue_total_pounds"] == "0.00"
    assert summary["expenses_total_pounds"] == "50.00"
    assert summary["vat_net_pounds"] == "-10.00"
    assert summary["bs_totals_pounds"] == {
        "assets": "10000.00",
        "liabilities": "50.00",
        "equity": "9950.00",
        "liabilities_and_equity": "10000.00",
    }
    assert summary["pl_lines"][0]["amount_pounds"] == "50.00"
    assert summary["bs_lines"][0]["amount_pounds"] == "10000.00"


def test_books_pounds_summary_degrades_to_zero_on_garbage() -> None:
    """Unparseable amounts degrade to '0.00' — never crash the books pane."""
    summary = server._books_pounds_summary(
        {"report": {"profit": "not-a-number"}}, {"report": {"vat_net": None}}
    )
    assert summary["profit_or_loss_pounds"] == "0.00"
    assert summary["vat_net_pounds"] == "0.00"
