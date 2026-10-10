# Formance MCP

MCP server in front of the [Formance Ledger](https://formance.com) HTTP API,
built on the official `mcp` Python SDK (MCPServer, SDK >= 2.3). Self-contained: it ships its
own httpx client and never imports from `api/`.

## Install

```bash
cd formance-mcp
uv venv .venv --python 3.12
uv pip install --python .venv -e ".[dev]"
```

## Run

```bash
# stdio (default)
FORNANCE_LEDGER_URL=http://localhost:3068 .venv/bin/formance-mcp

# streamable-http
MCP_TRANSPORT=streamable-http .venv/bin/formance-mcp
```

## Configuration (env)

| Variable             | Default                  | Purpose                              |
| -------------------- | ------------------------ | ------------------------------------ |
| `FORNANCE_LEDGER_URL`| `http://localhost:3068`  | Base URL of the Formance ledger API  |
| `FORMANCE_AUTH_TOKEN`| (unset)                  | Optional `Authorization` bearer token|
| `MCP_TRANSPORT`      | `stdio`                  | `stdio` or `streamable-http`         |

## Tools

1. `health` — Formance `_info` + version.
2. `create_transaction(ledger, postings, reference?, timestamp?, metadata?,
   idempotency_key?)` — commits a transaction; postings use decimal-string
   amounts; sends `Idempotency-Key` when provided; returns the committed
   transaction.
3. `get_transaction(ledger, txid)`
4. `list_transactions(ledger, page_size=100, after?)` — cursor pagination,
   follows `cursor.hasMore` to the end.
5. `get_balances(ledger, address?)` — preserves per-address/per-asset
   structure.
6. `list_ledgers()`
7. `run_script(ledger, plain)` — POST `/{ledger}/script` with Numscript
   body `{plain}`.

## Amount validation

Amounts are decimal strings end-to-end, validated against
`^\d+(\.\d{1,2})?$`. Anything else (floats, 3dp, garbage) is rejected with a
structured tool error — never coerced.

## Errors

Every Formance HTTP error (4xx/5xx/connect failure) becomes a structured MCP
tool error (`isError: true`, message includes status + body). No unhandled
exceptions leak to the client.

## Tests

```bash
.venv/bin/python -m pytest -q
```
