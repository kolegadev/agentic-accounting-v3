# CONTRACT F23 — Unify the UI on the Real Pipeline (remove the mock world)

## Goal

The live click-audit + Phase 0 research proved the UI runs two parallel
worlds: the v2 mock-company dashboard (mock companies, permanently-mock
"Instant reports", a second WS chat pipeline) beside the real entity /
orchestrator / Formance pipeline. This contract REMOVES the mock world so
there is exactly ONE selector (entity), ONE chat pipeline (orchestrator),
ONE report engine (Formance + Postgres cross-check), and ONE plain-English
explanation source (LLM over the real reports). Fix the prompt date
grounding (drafts were dated 2024-01-01 because the prompt never says
"today").

## Changes (exact)

### A. API (`api/`)

1. `api/src/services/orchestrator.py` — the prompt rules gain the current
   date: `"3. Dates are ISO 8601 (YYYY-MM-DD). Today is {today}."` where
   today = `datetime.now(timezone.utc).date()` injected at prompt build
   time. (Fixes the 2024-01-01 date hallucination that pushed posted
   transactions outside the report period.)
2. `api/src/routes/entities.py` — `GET /api/v1/entities/` becomes PUBLIC
   (drop the `get_current_user` dependency on the LIST route only; the
   local-trust model per the v2 design). `GET /{id}` and `/{id}/status`
   keep auth.

### B. chat-ui server (`chat-ui/server.py`)

3. Fix `/api/entities` proxy to call `/api/v1/entities/` (trailing slash —
   the current call 307s and the proxy returns an empty body, which is why
   the dropdown never populated).
4. NEW `GET /api/books?entity_id=<uuid>` — fetches the REAL reports for
   the entity (upstream `/api/v1/reports/formance/pl` and
   `/api/v1/reports/formance/balance-sheet` with the entity_id), then asks
   DeepSeek (env `LLM_API_URL`/`LLM_API_KEY`/`LLM_MODEL`, same as the API)
   for a plain-English explanation of the books, returns
   `{status: "ok", markdown: "...", sources: {...}}`; upstream or LLM
   failure → 502 `{status:"error", type:"upstream_unreachable"|"llm_error"}`
   (no mock fallback).
5. DELETE the mock world server-side: `/api/companies*`, `/api/dashboard`,
   `/api/reports/{report_type}`, the `MOCK_COMPANIES`/company store, the
   `_build_report_from_company`/`_transform_api_report` helpers, and the
   `/ws/chat` WebSocket bridge (both the WS handler and the HTTP fallback
   bridge) — nothing in the rebuilt UI calls them. Keep: `/api/health`,
   `/api/account`, `/api/settings`, `/api/llm-config` (GET/POST), the four
   orchestrator proxies, `/api/entities`, `/api/reports/formance/{type}`.

### C. chat-ui page (`chat-ui/index.html`) — rebuild to the real pipeline

6. REMOVE: the company switcher (`co-*` ids + `switchCompany`/`loadCompanies
   FromAPI`/`addCompany`/company store JS), the "What's happening now"
   state grid, the "Instant reports" grid + report modal + `loadReportFrom
   API` + all local mock report math (`renderReportLocal` et al.), the WS
   chat bridge JS (connect/`onmessage` translation/interpretation card),
   the `debug()`/`_log` debug panel and every console.log of the debug
   lines, and the mentor-tip rotator if it was mock-fed.
7. KEEP + WIRE (the whole page becomes): topbar with ONLY the entity
   dropdown (`#entity-select`, `#entity-label`) populated from the fixed
   `/api/entities`; the chat input + orchestrator message/preview/approve/
   reject pane (unchanged, F7/F20 wiring); "Pending drafts" pane
   (entity-scoped, unchanged); the "Formance reports" pane (unchanged,
   F10); NEW "Books in plain English" panel (`#books-panel`, `#books-output`)
   that fetches `/api/books?entity_id=...` on load AND on every entity
   switch, rendering the returned markdown (simple `\n`→`<br>`/bullet
   renderer — no raw HTML injection).
8. `switchEntity()` now refreshes: pending drafts, Formance report,
   books panel, and clears the preview — one scope, full fan-out. Add a
   request-generation token (a monotonically increasing `reqGen` counter
   checked before DOM writes) to `switchEntity`/`loadFormanceReport`/
   `loadBooks` so an out-of-order response can never paint stale scope.
9. `loadPendingDraft` fixed to be lossless: reuse the already-fetched
   pending list entry (preserving `formance_preview` + account_class)
   instead of refetching-and-rebuilding a stripped preview.

### D. Tests

10. `chat-ui/tests/test_server.py` — update for the removed endpoints
    (drop their tests if any; there were none for dashboard/companies but
    remove references), ADD: `/api/entities` calls `/api/v1/entities/`
    (trailing slash asserted), `/api/books` (entity_id forwarded; 502 on
    upstream failure; LLM failure → llm_error; success shape).
11. `api/tests/test_chatui_contract.py` — remove assertions for deleted
    ids/strings (`co-*`, `reports-grid`, `ledger-grid`, WS/`debug-panel`),
    keep + extend: `#entity-select`, `#entity-label`, the orchestrator
    wiring strings, the Formance report wiring, ADD `#books-panel`,
    `#books-output`, `'/api/books'`, and assert the OLD mock identifiers
    are ABSENT (`"brightwork"` not in html, `/api/companies` not in html,
    `/api/dashboard` not in html, `/ws/chat` not in html).
12. `api/tests/unit/test_orchestrator.py` — the prompt contains
    `Today is` + a real date (regex `Today is 20\d\d-\d\d-\d\d`).
13. `api/tests/test_entity_routes.py` — GET /entities works WITHOUT a
    token (update the F21 test accordingly).
14. Suites: api ≥ 1280, chat-ui ≥ 19 (net after removals/additions),
    formance-mcp 60 — all green.

## Boundaries

- ALLOWED: `api/src/services/orchestrator.py` (prompt date only),
  `api/src/routes/entities.py` (public list), `chat-ui/server.py`,
  `chat-ui/index.html`, `chat-ui/tests/test_server.py`,
  `api/tests/test_chatui_contract.py`, `api/tests/unit/test_orchestrator.py`,
  `api/tests/test_entity_routes.py`, this contract.
- NOT: constructors, catalog, ledger_parity, report engine, clients,
  Formance MCP, models, migrations.

## Success criteria

1. Suites green (above).
2. `git status --porcelain -uall` = allowed paths only.
3. After deploy + restart, a browser re-audit shows: ONE company selector
   (entity); every panel re-scopes on switch; Instant/mock reports gone;
   Books in plain English matches the Formance reports for the same entity;
   chat preview dates are today's date (JE-YYYY current year); no debug
   logs; no `/api/companies`, `/api/dashboard`, `/api/reports/{type}` or
   `/ws/chat` calls anywhere.
