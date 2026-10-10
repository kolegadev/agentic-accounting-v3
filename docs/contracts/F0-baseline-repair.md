# CONTRACT F0 — Baseline Repair (green suite on this machine)

## Goal

The full v3 test suite runs green on this macOS machine (no docker, no brew,
no local Redis, no local Ollama/LLM, no local Katra server).

## Current state (measured)

- Baseline log: `/tmp/pytest-baseline.log` (run with `--timeout=30`)
- After bcrypt pin (already committed): 23 failing tests, 9 auth errors FIXED
- Remaining clusters (see log for tracebacks):
  1. `tests/integration/test_chat_ws.py` (5 WS tests + 1 registry test) — Redis
     connect failure (localhost:6379) and LLM 404 (localhost:11434)
  2. `tests/integration/test_invoice_crud.py` (2) — AccountResponse
     ValidationError (stale fixtures)
  3. `tests/test_formatting_regression.py` (2) — shells out to `docker exec`
  4. `tests/unit/test_chat_service.py` (5) — LLM 404 (2) + AttributeError
     `format_response` (3, stale API)
  5. `tests/unit/test_invoice_service.py::TestSendInvoice::test_send_invoice_success` (1)
  6. `tests/unit/test_katra_client.py` (6) — network/mock issues
  7. `tests/unit/test_recurring_service.py::test_resume_template_past_date` (1)

## Boundaries

- **Production code (`api/src/**`) must NOT change.** The one thing you may
  not do is edit production code to make a test pass.
- Stale tests referencing refactored APIs (e.g. `ChatService.format_response`,
  which moved to `src/services/formatting.py`) MUST be updated to call the
  current API — never restore dead production code.
- Environment-dependent tests: add `pytest.mark.skipif(...)` with a clear
  `reason` — never delete them:
  - docker-dependent → `shutil.which("docker") is None`
  - live-LLM integration intent → skip if no reachable LLM endpoint; BUT where
    the test's intent is *routing behaviour*, MOCK the LLM call instead of
    skipping (httpx mocks / AsyncMock) — the behaviour is what we protect.
  - live-Katra / live-Redis → mock the network layer; skip only if the test's
    explicit intent is live-infrastructure integration.
- **Never weaken an assertion** to make a test pass. If a failure exposes a
  genuine production bug (not a stale test, not environment), do NOT fix it —
  report it in `known_limitations` with file:line and root cause.

## Success criteria

1. `cd /Users/johnpellew/agentic-accounting-v3/api && .venv/bin/python -m
   pytest tests/ -q --no-header -p no:cacheprovider --timeout=30` exits 0 with
   **0 failed, 0 errors**.
2. Every skip has a `reason` string.
3. `git diff --name-only` (vs commit 9584a71) touches ONLY files under
   `api/tests/` (plus nothing else).
4. No assertion was weakened: the diff shows only updated APIs, added mocks,
   added skipif markers.

## Test command

```
cd /Users/johnpellew/agentic-accounting-v3/api
.venv/bin/python -m pytest tests/ -q --no-header -p no:cacheprovider --timeout=30
```
