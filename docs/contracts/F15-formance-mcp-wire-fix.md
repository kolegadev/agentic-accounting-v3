# CONTRACT F15 — Formance MCP wire-encoding fix + live MCP test

## Goal

The F14 verifier proved `formance-mcp/src/formance_mcp/client.py` cannot
talk to the real Formance ledger: its plain decimal-string postings get
400 VALIDATION (and credit-nature sources get 400 INSUFFICIENT_FUND). Fix
the MCP service's client to the same wire contract the API client now uses
(F14: Numscript, minor-unit integer amounts, `allowing unbounded
overdraft` on sources, cursor-embedded envelopes), prove it with a LIVE
test against the real ledger on thebrick, and keep the MCP tools' public
input/output shapes unchanged for AI clients (decimal strings in, decimal
strings out — conversion happens on the wire only).

## Deliverables

1. **`formance-mcp/src/formance_mcp/client.py`** — mirror the F14 wire
   contract (read `api/src/services/formance_client.py` for the exact
   Numscript builder, envelope normalisation, and 204/400 create_ledger
   tolerance; the MCP client stays self-contained — no api/ imports, but
   the wire contract must be IDENTICAL):
   - `create_transaction`: accept postings with decimal-string amounts
     (the MCP tool's public shape, unchanged); validate (existing regex);
     convert each amount to minor-unit integer EXACTLY (Decimal×100→int,
     no float); emit Numscript `send [<asset> <int>] (source = @<source>
     allowing unbounded overdraft / destination = @<destination>)`, one
     send per posting; keep Idempotency-Key, reference, metadata.
   - `list_transactions`: normalise the cursor-embedded envelope
     (`{cursor:{pageSize,hasMore,next,data}}` → `{cursor:{hasMore,next},
     data:[...]}`), convert minor-unit amounts back to decimal strings
     exactly; cursor-only follow-up pages (no pageSize companion).
   - `get_balances`: normalise `{address:{asset:int}}` rows to
     `{address, balances:{asset: major-str}}`; request pageSize=1000.
   - `create_ledger` (if present) or tool equivalent: tolerate 204 and
     400 LEDGER_ALREADY_EXISTS.
   - Any 4xx/5xx → structured tool errors exactly as today.
2. **`formance-mcp/tests/test_tools.py`** — update/extend to pin the REAL
   wire contract (Numscript body shape, minor-unit ints, overdraft flag,
   envelope normalisation) with mocked httpx; existing 48 tests stay green
   or are updated to the real contract.
3. **`formance-mcp/tests/live/test_live_mcp.py`** — `skipif` no
   `FORNANCE_LEDGER_URL`/unreachable. Live round-trip on thebrick:
   create a ledger, `create_transaction` with a credit-nature source
   (e.g. source @4000 → destination @1000, £100.00) through the REAL
   tools, `get_balances` shows +10000 on 1000 and −10000 on 4000,
   `list_transactions` returns the tx with decimal-string amounts.
4. **`docs/live-e2e.md`** — add the F15 section: what was fixed, the live
   result, and remove/update the F14 "out of scope" note. Also correct the
   pagination pageSize claim per the F14 verifier's finding.
5. Local suites:
   - `cd formance-mcp && .venv/bin/python -m pytest -q` → 0 failed
     (48 + new, live test skipped).
   - `cd api && .venv/bin/python -m pytest tests/ -q --no-header -p no:cacheprovider --timeout=30`
     → unchanged (1171 passed / 9 skipped).
   - chat-ui → 14 passed (untouched).

## Execution notes

- ssh `thebrick`: containers may still be running from F14
  (`docker ps` — `docker compose up -d formance-postgres formance-ledger`
  from `/tmp/aa-live` if not). Sync the updated repo tree to
  `/tmp/aa-live` (rsync excluding .venv/.git). The formance-mcp venv is at
  `/tmp/aa-live/formance-mcp/.venv` (created in F14 if it existed; else
  `python3 -m venv .venv && .venv/bin/pip install -e . pytest`).
- Run: `FORNANCE_LEDGER_URL=http://127.0.0.1:3068
  .venv/bin/python -m pytest tests/live -v --timeout=120` from
  `formance-mcp/`.
- Record the live result in the report and in docs/live-e2e.md.

## Boundaries

- ONLY `formance-mcp/**` + `docs/live-e2e.md` + this contract.
- NO changes to `api/**` (the F14 client is the reference — do not touch
  it), constructors, catalog, UI, gateway.

## Success criteria

1. Live MCP test passes against the real ledger on thebrick (evidence:
   pytest output).
2. Local suites: formance-mcp ≥ 48+new green (live skipped), api 1171
   unchanged, chat-ui 14.
3. Diff scope = allowed paths only.
4. Verifier confirms: the MCP wire contract is byte-compatible with the
   F14 API client's (same Numscript shape, same envelope normalisation),
   and public tool shapes (decimal strings) are unchanged.
