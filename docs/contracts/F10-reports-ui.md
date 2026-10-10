# CONTRACT F10 — Reports into the API + chat-ui pane

## Goal

Surface the F9 Formance-derived reports to the user: thin REST routes on the
accounting API, forwarding endpoints in the chat-ui, and a reports pane in
the three-column UI that renders any of the 5 reports (with the
verify_against_postgres cross-check result shown alongside).

## Part A — API routes (`api/src/routes/formance_reports.py`)

Registered in `api/src/index.py` (one include):

- `GET /api/v1/reports/formance/{report_type}?period_start=&period_end=&as_at=`
  `report_type` ∈ {pl, balance-sheet, trial-balance, cash-flow, funds}
  → 200 `{report_type, report: {...}, identities: {...}, cross_check: {diffs:
  [...], ok: bool}}` — calls the F9 engine; for P&L/BS/TB runs
  `verify_against_postgres` and includes the diff list (CF/funds:
  `cross_check: {diffs: [], ok: null, note: "no Postgres equivalent"}`).
  Param validation: pl/cash-flow/funds require period_start+period_end;
  balance-sheet/trial-balance require as_at (default today). Invalid type →
  422; ReportEngineError → 422 {type, message}; no report is ever returned
  with broken identities (engine raises first).

## Part B — chat-ui

1. `chat-ui/server.py` — one forwarding endpoint
   `GET /api/reports/formance/{report_type}` → proxies to
   `${API_BASE_URL}/api/v1/reports/formance/{report_type}` with query params
   pass-through; 502 on upstream failure; NO mock fallback.
2. `chat-ui/index.html` — a reports pane with stable DOM ids:
   `#reports-pane`, `#report-type-select`, `#report-period-start`,
   `#report-period-end`, `#report-as-at`, `#report-fetch`, `#report-output`,
   `#report-crosscheck`. Select fetches the chosen report (period dates for
   flow reports, as-at for balance reports), renders the report lines as a
   table (£X,XXX.XX from pence), and shows the cross-check result
   (green "Postgres agrees" / red diff list). Existing panes untouched.

## Tests

1. `api/tests/test_formance_report_routes.py` (NEW) — TestClient with the F9
   engine functions mocked: per-report happy path (report + identities +
   cross_check shapes), 422 on invalid type, 422 on missing period params,
   422 ReportEngineError mapping, CF/funds cross_check note shape.
2. `chat-ui/tests/test_server.py` — add tests for the new forwarding
   endpoint (path/query/status pass-through, 502).
3. `api/tests/test_chatui_contract.py` — add the 8 new DOM ids + the
   `/api/reports/formance/` endpoint reference to the static contract.

## Boundaries

- NO changes to: formance_reports engine, orchestrator, constructors,
  catalog, existing routes' behaviour, chat_service, gateway.
- ALLOWED: new route file + index.py include, chat-ui/server.py additions,
  chat-ui/index.html additions, test additions, this contract.

## Success criteria

1. `cd api && .venv/bin/python -m pytest tests/ -q --no-header -p no:cacheprovider --timeout=30`
   → 0 failed, 0 errors, count ≥ 1047.
2. `cd chat-ui && .venv/bin/python -m pytest -q` → 0 failed.
3. `cd formance-mcp && .venv/bin/python -m pytest -q` → 48 passed.
4. `git status --porcelain -uall` shows only the allowed paths.
