# CONTRACT F7 — Chat-UI Rewiring (two-column preview/approval pane)

## Goal

Expose the F6 orchestrator over REST and wire the existing two-column chat-ui
to it: left pane chat, right pane shows the constructed entry preview
(legs, amounts, skill badge, Formance preview) with Approve / Reject —
the user confirms before anything posts.

## Part A — API routes (api/)

`api/src/routes/orchestrator.py`, registered in `api/src/index.py`:

- `POST /api/v1/orchestrator/message` {message, history?, session_id?}
  → 200 {status: "preview"|"clarification"|"error", preview: {...},
       error: {...}} — thin wrapper over `orchestrate_message`.
- `POST /api/v1/orchestrator/approve` {transaction_id}
  → 200 {status: "posted", transaction, parity_ok, parity_report}
  (TransactionNotFound/NotDraft → 404/422 with {detail}).
- `POST /api/v1/orchestrator/reject` {transaction_id}
  → 200 {status: "discarded"}.
- `GET /api/v1/orchestrator/pending` → list of draft transactions with legs
  (previews awaiting approval; only drafts, newest first).

Rules: routes are thin (no business logic); orchestrator errors map to
422 with {type, message}; service exceptions to 404/422; LLM not called for
approve/reject/pending.

## Part B — chat-ui

1. `chat-ui/server.py` — new forwarding endpoints (mirror the API paths):
   `POST /api/orchestrator/message`, `POST /api/orchestrator/approve`,
   `POST /api/orchestrator/reject`, `GET /api/orchestrator/pending`.
   They forward to `${API_BASE_URL}/api/v1/orchestrator/*` via httpx and
   pass through status codes + JSON. NO mock fallback for these (real
   pipeline only) — upstream unreachable → 502 JSON error.
2. `chat-ui/index.html` — right pane gains the preview/approval UI:
   - send a chat message → POST message endpoint → on preview: render
     skill badge, legs table (account code+name, class, debit/credit in
     £X,XXX.XX), total, Formance postings preview
   - Approve button → approve endpoint → show POSTED result + parity status
   - Reject button → reject endpoint → clear preview
   - on clarification/error → show message in the pane
   - pending drafts load on page refresh (GET pending)
   - new DOM ids must be stable for the static contract test:
     `#orchestrator-preview`, `#preview-legs`, `#preview-total`,
     `#preview-approve`, `#preview-reject`, `#preview-status`,
     `#pending-drafts`
   Existing dashboard/report features must keep working (do not break the
   other panes or the mock-company dashboard).
3. `chat-ui/tests/test_server.py` (NEW) — FastAPI TestClient against
   chat-ui server with the upstream httpx client mocked (inject a transport
   at the module seam): forwards path/body/status for all 4 endpoints,
   passes through error JSON, 502 on upstream failure. No live API needed.
4. `api/tests/test_orchestrator_routes.py` (NEW) — TestClient against the
   accounting API with orchestrator service functions mocked (AsyncMock):
   message preview/error/clarification mapping, approve/reject/pending
   happy paths + 404/422 error paths.

## Static DOM contract test

`api/tests/test_chatui_contract.py` — reads `chat-ui/index.html` and asserts
the 7 required DOM ids above exist, and the JS references the 4 orchestrator
endpoints (`/api/orchestrator/message|approve|reject|pending`). Prevents
UI drift without a browser harness.

## Boundaries

- NO changes to: orchestrator service, constructors, transaction_service,
  gateway, Formance MCP, chat_service (the old WS chat flow stays intact
  and untouched).
- ALLOWED: new route file + app registration (`api/src/index.py` one-line
  include), chat-ui/server.py additions, chat-ui/index.html additions,
  the three new test files, this contract.

## Success criteria

1. `cd api && .venv/bin/python -m pytest tests/ -q --no-header -p no:cacheprovider --timeout=30`
   → 0 failed, 0 errors, count ≥ 963.
2. `cd chat-ui && .venv/bin/python -m pytest -q` → 0 failed (create the
   chat-ui venv if none exists: `uv venv .venv --python 3.12` and install
   chat-ui/requirements.txt + httpx + pytest).
3. `cd formance-mcp && .venv/bin/python -m pytest -q` → still 48 passed.
4. `git status --porcelain -uall` shows only the allowed paths above.
