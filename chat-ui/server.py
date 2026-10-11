"""Chat UI server for Agentic Accounting — serves the HTML frontend and
forwards the browser to the accounting API's real pipeline.

Contract F23: the mock world is GONE.  There is exactly ONE selector
(entity), ONE chat pipeline (the orchestrator), ONE report engine
(Formance + Postgres cross-check) and ONE plain-English explanation
source (the LLM over the real reports, via /api/books).
"""

from __future__ import annotations

import json
import logging
import os
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

API_BASE_URL = os.getenv("API_BASE_URL", "http://accounting-api:8000")

logger = logging.getLogger(__name__)

app = FastAPI(title="Agentic Accounting Chat UI", version="0.2.0")

UI_DIR = Path(__file__).parent


@app.get("/")
async def root():
    """Serve the chat UI from disk."""
    index_path = UI_DIR / "index.html"
    if not index_path.exists():
        return JSONResponse(
            status_code=503,
            content={
                "status": "error",
                "type": "ui_missing",
                "message": "chat-ui/index.html not found next to server.py",
            },
        )
    content = index_path.read_text()
    headers = {
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
    }
    return HTMLResponse(content=content, headers=headers)


@app.get("/api/health")
async def health():
    """Check if the accounting API backend is reachable."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{API_BASE_URL}/health")
            return {
                "status": "ok",
                "backend": "connected" if resp.status_code == 200 else "unhealthy",
            }
    except Exception:
        return {"status": "ok", "backend": "unreachable"}


@app.get("/api/account")
async def account():
    """Return backend connection info."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{API_BASE_URL}/health")
            return {
                "backend_configured": resp.status_code == 200,
                "api_base_url": API_BASE_URL,
            }
    except Exception:
        return {"backend_configured": False, "api_base_url": API_BASE_URL}


# ---------------------------------------------------------------------------
# LLM Configuration — stored in memory, initialized from env
# ---------------------------------------------------------------------------
LLM_CONFIG = {
    "api_url": os.getenv("LLM_API_URL", "https://api.deepseek.com/v1/chat/completions"),
    "model": os.getenv("LLM_MODEL", "deepseek-v4-pro"),
    "api_key": os.getenv("LLM_API_KEY", ""),
}


@app.get("/api/llm-config")
async def get_llm_config():
    """Return current LLM configuration (key masked)."""
    return {
        "api_url": LLM_CONFIG["api_url"],
        "model": LLM_CONFIG["model"],
        "api_key": LLM_CONFIG["api_key"][:8] + "***" if LLM_CONFIG["api_key"] else "",
        "has_key": bool(LLM_CONFIG["api_key"]),
    }


@app.post("/api/llm-config")
async def update_llm_config(data: dict):
    """Update LLM configuration. Persists to env for the API bridge."""
    global LLM_CONFIG
    for field in ("api_url", "model", "api_key"):
        if field in data and data[field]:
            LLM_CONFIG[field] = data[field]
    # Push to accounting API so the LLM chat can use the new key
    if LLM_CONFIG["api_key"]:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(
                    f"{API_BASE_URL}/api/v1/chat/llm-config",
                    json={
                        "api_url": LLM_CONFIG["api_url"],
                        "model": LLM_CONFIG["model"],
                        "api_key": LLM_CONFIG["api_key"],
                    },
                )
        except Exception:
            pass
    return {"status": "ok", "config": {k: v for k, v in LLM_CONFIG.items() if k != "api_key"}}


@app.post("/api/settings")
async def update_settings(data: dict):
    """Update API backend URL (runtime override)."""
    global API_BASE_URL
    new_url = (data.get("api_base_url") or "").strip()
    if new_url:
        API_BASE_URL = new_url
        return {"status": "ok", "updated": ["api_base_url"], "message": "Settings saved. Reconnect to use new URL."}
    return {"status": "ok", "updated": [], "message": "No settings changed."}


# ---------------------------------------------------------------------------
# Orchestrator forwarding (Contract F7) — real pipeline only, NO mock fallback.
#
# These endpoints mirror the accounting API's /api/v1/orchestrator/* routes.
# Requests are forwarded over httpx with status codes and JSON passed through
# untouched.  If the upstream API is unreachable the response is 502 JSON.
#
# Test seam: assign an httpx.AsyncClient (e.g. one built on httpx.MockTransport)
# to server._upstream_client — the endpoints use it instead of a real socket.
# ---------------------------------------------------------------------------
_upstream_client: httpx.AsyncClient | None = None

_ORCHESTRATOR_ROUTES = {
    "message": {"method": "POST", "path": "/api/v1/orchestrator/message"},
    "approve": {"method": "POST", "path": "/api/v1/orchestrator/approve"},
    "reject": {"method": "POST", "path": "/api/v1/orchestrator/reject"},
    "pending": {"method": "GET", "path": "/api/v1/orchestrator/pending"},
}


def _get_upstream_client() -> httpx.AsyncClient:
    """Return the injectable upstream client (tests replace this seam)."""
    global _upstream_client
    if _upstream_client is None:
        _upstream_client = httpx.AsyncClient(timeout=10.0)
    return _upstream_client


async def _forward_orchestrator(
    action: str,
    body: dict | None,
    params: dict | None = None,
) -> JSONResponse:
    """Forward one orchestrator call to the accounting API.

    Status code and JSON body pass through untouched; upstream
    connection failures become a 502 JSON error.  ``params`` carries the
    browser's query string (e.g. ``entity_id`` — Contract F20) through to
    the upstream route untouched.
    """
    spec = _ORCHESTRATOR_ROUTES[action]
    url = f"{API_BASE_URL}{spec['path']}"
    try:
        client = _get_upstream_client()
        if spec["method"] == "GET":
            resp = await client.get(url, params=params)
        else:
            resp = await client.post(url, json=body, params=params)
    except httpx.HTTPError as exc:
        return JSONResponse(
            status_code=502,
            content={
                "status": "error",
                "type": "upstream_unreachable",
                "message": f"Accounting API unreachable: {exc}",
            },
        )
    try:
        payload = resp.json()
    except ValueError:
        payload = {"detail": resp.text}
    return JSONResponse(status_code=resp.status_code, content=payload)


@app.post("/api/orchestrator/message")
async def orchestrator_message(data: dict):
    """POST → /api/v1/orchestrator/message (LLM classify + construct preview)."""
    return await _forward_orchestrator("message", data)


@app.post("/api/orchestrator/approve")
async def orchestrator_approve(data: dict, request: Request):
    """POST → /api/v1/orchestrator/approve (post the approved draft)."""
    return await _forward_orchestrator(
        "approve", data, params=dict(request.query_params)
    )


@app.post("/api/orchestrator/reject")
async def orchestrator_reject(data: dict, request: Request):
    """POST → /api/v1/orchestrator/reject (discard the draft)."""
    return await _forward_orchestrator(
        "reject", data, params=dict(request.query_params)
    )


@app.get("/api/orchestrator/pending")
async def orchestrator_pending(request: Request):
    """GET → /api/v1/orchestrator/pending (drafts awaiting approval)."""
    return await _forward_orchestrator(
        "pending", None, params=dict(request.query_params)
    )


# ---------------------------------------------------------------------------
# Entities forwarding (Contract F20/F23) — real pipeline only, NO mock fallback.
#
# GET /api/entities proxies to the accounting API's /api/v1/entities/ so the
# UI's entity switcher is fed by the real multi-entity list.  Contract F23:
# the trailing slash matters — the slash-less URL 307-redirects and the old
# proxy returned an empty body, which is why the dropdown never populated.
# Status and JSON pass through untouched; an unreachable upstream is 502.
# Reuses the same injectable _upstream_client test seam as the F7 routes.
# ---------------------------------------------------------------------------


@app.get("/api/entities")
async def entities():
    """GET → /api/v1/entities/ (list accounting entities for the switcher)."""
    url = f"{API_BASE_URL}/api/v1/entities/"
    try:
        client = _get_upstream_client()
        resp = await client.get(url)
    except httpx.HTTPError as exc:
        return JSONResponse(
            status_code=502,
            content={
                "status": "error",
                "type": "upstream_unreachable",
                "message": f"Accounting API unreachable: {exc}",
            },
        )
    try:
        payload = resp.json()
    except ValueError:
        payload = {"detail": resp.text}
    return JSONResponse(status_code=resp.status_code, content=payload)


# ---------------------------------------------------------------------------
# Admin forwarding (Contract F27) — owner-token injection + proxies for
# entity creation/update, the global contacts list and the users list.
# Real pipeline only, NO mock fallback.
#
# The owner logs in ONCE (lazy, cached — first admin-proxied request wins)
# with OWNER_EMAIL/OWNER_PASSWORD and the token rides as the Authorization
# header on every admin-proxied call.  A failed login logs a warning and
# degrades to EMPTY headers: the upstream answers 401 and the UI shows that
# message.  Contacts are global + unauthenticated upstream, so they forward
# WITHOUT a token.  Status/JSON pass through untouched; an unreachable
# upstream is 502 upstream_unreachable.  Same injectable _upstream_client
# test seam as the F7 routes.
# ---------------------------------------------------------------------------

OWNER_EMAIL = os.getenv("OWNER_EMAIL", "john@kolega.ai")
OWNER_PASSWORD = os.getenv("OWNER_PASSWORD", "TestAccounting123!")

_admin_token: str | None = None
_admin_login_tried: bool = False


async def _admin_login() -> str | None:
    """Log in with the owner credentials ONCE and cache the access token.

    The first call performs POST /api/v1/auth/login; every later call
    returns the cached token (or None).  A failed login logs a warning and
    returns None — no further attempts are made.
    """
    global _admin_token, _admin_login_tried
    if _admin_token is not None or _admin_login_tried:
        return _admin_token
    _admin_login_tried = True
    try:
        client = _get_upstream_client()
        resp = await client.post(
            f"{API_BASE_URL}/api/v1/auth/login",
            json={"email": OWNER_EMAIL, "password": OWNER_PASSWORD},
        )
        payload = resp.json()
    except httpx.HTTPError as exc:
        logger.warning("Owner login failed (upstream unreachable): %s", exc)
        return None
    except ValueError as exc:
        logger.warning("Owner login failed (non-JSON response): %s", exc)
        return None
    token = payload.get("access_token") if resp.status_code == 200 else None
    if not token:
        logger.warning(
            "Owner login failed (HTTP %s): %s", resp.status_code, payload
        )
        return None
    _admin_token = token
    return token


async def _admin_auth_headers() -> dict[str, str]:
    """Authorization header carrying the owner token; EMPTY on login failure."""
    token = await _admin_login()
    return {"Authorization": f"Bearer {token}"} if token else {}


def _admin_unreachable(exc: httpx.HTTPError) -> JSONResponse:
    """502 JSON for a transport failure reaching the accounting API."""
    return JSONResponse(
        status_code=502,
        content={
            "status": "error",
            "type": "upstream_unreachable",
            "message": f"Accounting API unreachable: {exc}",
        },
    )


def _admin_passthrough(resp: httpx.Response) -> JSONResponse:
    """Mirror the upstream status + JSON (non-JSON bodies become detail)."""
    try:
        payload = resp.json()
    except ValueError:
        payload = {"detail": resp.text}
    return JSONResponse(status_code=resp.status_code, content=payload)


@app.post("/api/entities")
async def create_entity(data: dict, request: Request):
    """POST → /api/v1/entities/ (body + admin header; query string forwarded)."""
    url = f"{API_BASE_URL}/api/v1/entities/"
    headers = await _admin_auth_headers()
    try:
        client = _get_upstream_client()
        resp = await client.post(
            url, json=data, headers=headers, params=dict(request.query_params)
        )
    except httpx.HTTPError as exc:
        return _admin_unreachable(exc)
    return _admin_passthrough(resp)


@app.patch("/api/entities/{entity_id}")
async def update_entity(entity_id: str, data: dict):
    """PATCH → /api/v1/entities/{entity_id} (body + admin header)."""
    url = f"{API_BASE_URL}/api/v1/entities/{entity_id}"
    headers = await _admin_auth_headers()
    try:
        client = _get_upstream_client()
        resp = await client.patch(url, json=data, headers=headers)
    except httpx.HTTPError as exc:
        return _admin_unreachable(exc)
    return _admin_passthrough(resp)


@app.get("/api/contacts")
async def list_contacts(request: Request):
    """GET → /api/v1/contacts/ (global, unauthenticated — no token)."""
    url = f"{API_BASE_URL}/api/v1/contacts/"
    try:
        client = _get_upstream_client()
        resp = await client.get(url, params=dict(request.query_params))
    except httpx.HTTPError as exc:
        return _admin_unreachable(exc)
    return _admin_passthrough(resp)


@app.post("/api/contacts")
async def create_contact(data: dict):
    """POST → /api/v1/contacts/ (global, unauthenticated — no token)."""
    url = f"{API_BASE_URL}/api/v1/contacts/"
    try:
        client = _get_upstream_client()
        resp = await client.post(url, json=data)
    except httpx.HTTPError as exc:
        return _admin_unreachable(exc)
    return _admin_passthrough(resp)


@app.get("/api/users")
async def list_users():
    """GET → /api/v1/auth/users (admin header)."""
    url = f"{API_BASE_URL}/api/v1/auth/users"
    headers = await _admin_auth_headers()
    try:
        client = _get_upstream_client()
        resp = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        return _admin_unreachable(exc)
    return _admin_passthrough(resp)


# ---------------------------------------------------------------------------
# Reports forwarding (Contract F10) — real pipeline only, NO mock fallback.
#
# GET /api/reports/formance/{report_type} proxies to the accounting API's
# /api/v1/reports/formance/{report_type} with query params passed through
# untouched.  If the upstream API is unreachable the response is 502 JSON.
# Reuses the same injectable _upstream_client test seam as the F7 routes.
# ---------------------------------------------------------------------------


@app.get("/api/reports/formance/{report_type}")
async def forward_formance_report(report_type: str, request: Request):
    """GET → /api/v1/reports/formance/{report_type} (query params pass through)."""
    url = f"{API_BASE_URL}/api/v1/reports/formance/{report_type}"
    try:
        client = _get_upstream_client()
        resp = await client.get(url, params=dict(request.query_params))
    except httpx.HTTPError as exc:
        return JSONResponse(
            status_code=502,
            content={
                "status": "error",
                "type": "upstream_unreachable",
                "message": f"Accounting API unreachable: {exc}",
            },
        )
    try:
        payload = resp.json()
    except ValueError:
        payload = {"detail": resp.text}
    return JSONResponse(status_code=resp.status_code, content=payload)


# ---------------------------------------------------------------------------
# Books in plain English (Contract F23) — real pipeline only, NO mock fallback.
#
# GET /api/books?entity_id=<uuid> fetches the REAL Formance P&L and balance
# sheet for the entity, then asks the LLM (env LLM_API_URL / LLM_API_KEY /
# LLM_MODEL, same convention as the API — falling back to the runtime
# LLM_CONFIG) for a plain-English explanation.  Upstream or LLM failure is a
# 502 {status:"error", type:"upstream_unreachable"|"llm_error"} — there is
# no fabricated fallback data anywhere in this server.
# ---------------------------------------------------------------------------


class _BooksLLMError(Exception):
    """The LLM explanation call failed (transport or response shape)."""


def _pounds(pence) -> str:
    """Exact pence -> pounds string (integer pence in, two-decimal string out)."""
    try:
        return str((Decimal(int(pence)) / 100).quantize(Decimal("0.01")))
    except (ValueError, TypeError, InvalidOperation, ArithmeticError):
        return "0.00"


def _books_pounds_summary(pl: dict, bs: dict) -> dict:
    """Deterministic pence->pounds summary of the two raw reports.

    The LLM NEVER does money math: every amount it sees is already in
    pounds sterling with two decimals.
    """
    pl_r = pl.get("report", {}) if isinstance(pl, dict) else {}
    bs_r = bs.get("report", {}) if isinstance(bs, dict) else {}

    def pl_lines() -> list[dict]:
        out: list[dict] = []
        for sec, lines in (pl_r.get("sections") or {}).items():
            for line in lines or []:
                out.append({
                    "section": sec,
                    "name": line.get("name"),
                    "amount_pounds": _pounds(line.get("amount", 0)),
                })
        return out

    def bs_lines() -> list[dict]:
        out: list[dict] = []
        for sec, lines in (bs_r.get("sections") or {}).items():
            for line in lines or []:
                out.append({
                    "section": sec,
                    "name": line.get("name"),
                    "code": line.get("code"),
                    "amount_pounds": _pounds(line.get("amount", 0)),
                })
        return out

    totals = bs_r.get("totals") or {}
    pl_totals = pl_r.get("totals") or {}
    return {
        "period": pl_r.get("period"),
        "as_at": bs_r.get("as_at"),
        "profit_or_loss_pounds": _pounds(pl_r.get("profit", 0)),
        "revenue_total_pounds": _pounds(pl_totals.get("revenue", 0)),
        "expenses_total_pounds": _pounds(
            pl_totals.get("expenses", pl_totals.get("direct_costs", 0))
        ),
        "pl_lines": pl_lines(),
        "bs_lines": bs_lines(),
        "bs_totals_pounds": {k: _pounds(v) for k, v in totals.items()},
        "vat_net_pounds": _pounds(bs_r.get("vat_net", 0)),
    }


async def _llm_explain_books(pl: dict, bs: dict) -> str:
    """Ask the configured LLM to explain the entity's books in plain English."""
    llm_url = os.getenv("LLM_API_URL") or LLM_CONFIG["api_url"]
    llm_key = os.getenv("LLM_API_KEY") or LLM_CONFIG["api_key"]
    llm_model = os.getenv("LLM_MODEL") or LLM_CONFIG["model"]

    headers: dict[str, str] = {"Content-Type": "application/json"}
    if llm_key:
        headers["Authorization"] = f"Bearer {llm_key}"

    system_prompt = (
        "You explain the books of a UK small business in plain English for "
        "a non-accountant owner. Use ONLY the figures supplied — never "
        "invent numbers. IMPORTANT: every amount in the supplied summary is "
        "ALREADY in pounds sterling (GBP) with two decimals — do not "
        "multiply, divide, or convert any figure. Keep it short: a 2-3 "
        "sentence summary, then 3-5 '- ' bullets with the key figures "
        "(**bold**), then one sentence of practical advice."
    )
    summary = _books_pounds_summary(pl, bs)
    user_message = (
        "Books summary (all amounts already in pounds sterling):\n"
        + json.dumps(summary, indent=2)
    )
    payload = {
        "model": llm_model,
        "temperature": 0,
        "max_tokens": 1200,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(llm_url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
            content = data["choices"][0]["message"]["content"]
            return str(content).strip()
    except httpx.HTTPError as exc:
        raise _BooksLLMError(f"LLM API error: {exc}") from exc
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise _BooksLLMError(
            f"LLM returned an unexpected response format: {exc}"
        ) from exc


@app.get("/api/books")
async def books(entity_id: str = ""):
    """Explain the entity's REAL books in plain English (Contract F23).

    Fetches `/api/v1/reports/formance/pl` and
    `/api/v1/reports/formance/balance-sheet` for the entity from the
    accounting API, then asks the LLM to explain them.  No mock fallback:
    upstream failure → 502 upstream_unreachable, LLM failure → 502
    llm_error.
    """
    entity_id = (entity_id or "").strip()
    if not entity_id:
        return JSONResponse(
            status_code=422,
            content={
                "status": "error",
                "type": "missing_entity_id",
                "message": "entity_id query parameter is required",
            },
        )

    params = {"entity_id": entity_id}
    pl_url = f"{API_BASE_URL}/api/v1/reports/formance/pl"
    bs_url = f"{API_BASE_URL}/api/v1/reports/formance/balance-sheet"
    try:
        client = _get_upstream_client()
        pl_resp = await client.get(pl_url, params=params)
        bs_resp = await client.get(bs_url, params=params)
    except httpx.HTTPError as exc:
        return JSONResponse(
            status_code=502,
            content={
                "status": "error",
                "type": "upstream_unreachable",
                "message": f"Accounting API unreachable: {exc}",
            },
        )
    if pl_resp.status_code != 200 or bs_resp.status_code != 200:
        return JSONResponse(
            status_code=502,
            content={
                "status": "error",
                "type": "upstream_unreachable",
                "message": (
                    "Accounting API reports failed: "
                    f"pl={pl_resp.status_code}, balance_sheet={bs_resp.status_code}"
                ),
            },
        )
    try:
        pl = pl_resp.json()
        bs = bs_resp.json()
    except ValueError:
        return JSONResponse(
            status_code=502,
            content={
                "status": "error",
                "type": "upstream_unreachable",
                "message": "Accounting API returned non-JSON reports",
            },
        )

    try:
        markdown = await _llm_explain_books(pl, bs)
    except _BooksLLMError as exc:
        return JSONResponse(
            status_code=502,
            content={"status": "error", "type": "llm_error", "message": str(exc)},
        )
    except Exception as exc:  # noqa: BLE001 — never leak a traceback to the UI
        return JSONResponse(
            status_code=502,
            content={
                "status": "error",
                "type": "llm_error",
                "message": f"LLM explanation failed: {exc.__class__.__name__}: {exc}",
            },
        )

    return {
        "status": "ok",
        "markdown": markdown,
        "sources": {
            "pl": f"{pl_url}?entity_id={entity_id}",
            "balance_sheet": f"{bs_url}?entity_id={entity_id}",
        },
    }


# ---------------------------------------------------------------------------
# Server launcher
# ---------------------------------------------------------------------------

def start_server(host: str = "0.0.0.0", port: int = 3000):
    """Start the uvicorn server. index.html must sit next to server.py."""
    ui_path = UI_DIR / "index.html"
    if not ui_path.exists():
        raise FileNotFoundError(
            f"{ui_path} missing — deploy chat-ui/index.html alongside server.py"
        )

    print(f"🚀 Agentic Accounting Chat UI starting at http://{host}:{port}")
    print("Press Ctrl+C to stop.")

    uvicorn.run(
        "server:app",
        host=host,
        port=port,
        log_level="warning",
    )


if __name__ == "__main__":
    start_server()
