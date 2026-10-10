# CONTRACT F5 — Formance MCP Server (current MCP protocol)

## Goal

A self-contained MCP server in front of the Formance ledger HTTP API, built
on the OFFICIAL `mcp` Python SDK (FastMCP) so it speaks the current MCP
protocol — replacing the dead-end hand-rolled gateway pattern (v2's gateway
is pinned at protocolVersion 0.1.0 and cannot be used with modern clients).

## Scope decision (stated assumption)

ADDITIVE. The v2 gateway/ (accounting tools, SSE 0.1.0) is left untouched in
this feature; the ledger-facing MCP surface is new code under `formance-mcp/`.
Gateway consolidation is a later feature (F7).

## Deliverables

```
formance-mcp/
  pyproject.toml          # deps: mcp[cli]>=1.x, httpx, pydantic; pytest dev
  src/formance_mcp/
    __init__.py
    server.py             # FastMCP server + tool defs + env config
    client.py             # thin httpx Formance HTTP client (self-contained)
  tests/
    test_tools.py         # unit tests, mocked httpx
    test_protocol.py      # in-memory MCP client round-trip
  Dockerfile
  README.md
```

## Tools (AI-friendly schemas, all with docstrings)

1. `health` → Formance `_info` + version.
2. `create_transaction(ledger, postings, reference?, timestamp?, metadata?,
   idempotency_key?)` — postings = list of {source, destination, amount
   (DECIMAL STRING, required, never float), asset}; sends `Idempotency-Key`
   header when provided; returns the committed transaction (txid).
3. `get_transaction(ledger, txid)`.
4. `list_transactions(ledger, page_size=100, after?)` — cursor pagination,
   follows `cursor.hasMore`.
5. `get_balances(ledger, address?)`.
6. `list_ledgers()` (GET /_info or /v2 route as the Formance instance
   exposes; tolerate `GET /` with a JSON listing when available).
7. `run_script(ledger, plain: str)` — POST `/{ledger}/script` with Numscript
   body {plain}; returns result/error.

## Hard rules

- Amounts are decimal STRINGS end-to-end (pass through untouched, validate
  with Decimal regex `^\d+(\.\d{1,2})?$` — reject anything else with a clear
  tool error, never coerce with float).
- Every Formance HTTP error (4xx/5xx/connect failure) → structured MCP tool
  error (`isError: true`, message includes status + body), NEVER an
  unhandled exception.
- Config via env: `FORNANCE_LEDGER_URL` (default `http://localhost:3068`),
  `MCP_TRANSPORT` (default `stdio`; `streamable-http` supported),
  optional `FORMANCE_AUTH_TOKEN` → Authorization header.
- `get_balances` must preserve per-address/per-asset structure; no lossy
  merging (F2's parity tool consumes these shapes).

## Protocol proof (test_protocol.py)

Using the `mcp` SDK's own in-memory client facilities
(`mcp.shared.memory.create_connected_server_and_client_session` or the
version-equivalent), prove a REAL protocol round-trip:
initialize → tools/list (all 7 tools present with input schemas) →
tools/call `health` → valid result. Assert the negotiated protocolVersion is
NOT "0.1.0" (i.e. the SDK's current version).

## Success criteria

1. `cd formance-mcp && .venv/bin/python -m pytest -q` → 0 failed.
2. Repo suite unchanged and green:
   `cd api && .venv/bin/python -m pytest tests/ -q --no-header -p no:cacheprovider --timeout=30`
   → 933 passed, 0 failed.
3. `git diff --name-only` / untracked = only `formance-mcp/**` +
   `docs/contracts/F5-formance-mcp.md`.
4. No changes to `api/**`, `gateway/**`, `chat-ui/**`, `skills/**`.
