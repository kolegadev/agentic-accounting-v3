# CONTRACT F14 — Live Formance e2e (real ledger, real payloads)

## Goal

Run the v3 pipeline against the REAL Formance ledger for the first time
(on thebrick, which has Docker) and settle the last unverified assumptions:
real HTTP shapes from `create_ledger`, `list_transactions`, `get_balances`,
and whether the F13 residuals (trailing-zero >2dp strings; zero-volume
addresses omitted from balances) actually occur. Deliver a repeatable live
suite + runbook, and fix `ledger_parity` if the real payloads prove the
strict assumptions wrong.

## Environment

- thebrick (ssh `thebrick`): Docker 29.1.3 running, 16 cores. Use ONLY
  `formance-postgres` + `formance-ledger` from the repo's docker-compose
  (the API side runs on a real aiosqlite ORM — no Postgres docker needed).
- The API-side DB is the proven aiosqlite ORM pattern; the REAL component
  under test is the Formance HTTP API + our client + mirror + parity +
  reports.

## Deliverables

1. **`api/tests/live/test_live_formance_e2e.py`** — every test decorated
   `@pytest.mark.skipif(not FORNANCE_LEDGER_URL or unreachable, reason=...)`
   (skip locally, run on thebrick). Tests, in order:
   a. `create_ledger` a fresh `entity-live-<rand>` ledger → real 200/409.
   b. Entity setup on real aiosqlite ORM (COA template, bank, opening
      balances draft) → approve via `orchestrator.approve_entry` →
      REAL mirror lands in Formance → `verify_parity(ledger)` ok=True.
   c. Post a supplier-invoice (VAT), a sales-invoice (VAT), and a
      depreciation journal through the constructors/pipeline → approve each
      → `verify_parity` ok=True after every post.
   d. All 5 F9 reports over the live ledger: identities hold +
      `verify_against_postgres` empty diffs for P&L/BS/TB.
   e. SHAPE CAPTURE: assert the real `list_transactions` + `get_balances`
      payload shapes against the client's parsing (cursor object, metadata
      pass-through, per-address/per-asset balances). This empirically
      resolves F13 residuals I3 (trailing zeros >2dp) and I4 (zero-volume
      addresses omitted).
2. **`scripts/live-e2e.sh`** — runbook: `docker compose up -d
   formance-postgres formance-ledger`, wait for health, set
   `FORNANCE_LEDGER_URL=http://127.0.0.1:3068`, run `pytest tests/live -v`,
   print teardown instructions. Document the whole flow in
   `docs/live-e2e.md`.
3. **`ledger_parity` fix IF NEEDED** — if (e) proves I3/I4 occur with real
   Formance (trailing zeros must be accepted; omitted addresses may be
   legitimately zero), adjust the strict parsing accordingly WITH unit-test
   updates in `test_ledger_parity.py`, and record the finding in
   `docs/live-e2e.md`. If the assumptions hold, state that explicitly —
   no change.
4. Local suite stays green (live tests skip):
   `cd api && .venv/bin/python -m pytest tests/ -q --no-header -p no:cacheprovider --timeout=30`
   → 0 failed, 0 errors, ≥ 1165.

## Execution notes for the generator

- You have ssh access to `thebrick`. Ship the working tree (rsync/tar,
  exclude .venv/.git) to `/tmp/aa-live` there; create a venv (python3.12,
  pip install `api` deps incl. `-e api` and pytest/pytest-asyncio/
  aiosqlite — check `python3 --version` and use pip; add bcrypt==4.0.1 and
  cryptography<50 pins like the local env).
- `docker compose up -d formance-postgres formance-ledger` from the repo
  root on thebrick; poll `curl -s http://127.0.0.1:3068/_info` until 200.
- Expect REAL-API surprises: verify `create_ledger` route (`POST /v2/{ledger}`),
  `list_transactions` cursor shape, balances shape — fix our client ONLY if
  the contract allows (client fixes = same-class as ledger_parity fixes:
  allowed, must be minimal and unit-tested; do NOT change F1 pairing).
- Record every discrepancy + fix in `docs/live-e2e.md`.

## Boundaries

- ALLOWED: new `api/tests/live/` file, `scripts/live-e2e.sh`,
  `docs/live-e2e.md`, and — only if (e) proves it — minimal
  `ledger_parity.py` + `formance_client.py` fixes with unit tests.
- NOT allowed: F1 pairing logic, constructors, catalog, orchestrator
  behaviour, report engine internals, UI, gateway, Formance MCP.

## Success criteria

1. The live suite PASSES on thebrick against the real ledger (evidence in
   the report: pytest output + captured real payload shapes).
2. Local suite ≥ 1165 green with the live tests skipped.
3. `docs/live-e2e.md` documents: how to run, what real shapes were observed,
   and the I3/I4 verdicts.
4. Diff scope = the allowed paths only.
