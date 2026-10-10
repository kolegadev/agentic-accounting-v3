# Live Formance e2e — runbook and findings (Contracts F14 + F15)

This document records how to run the v3 pipeline against the REAL Formance
ledger (first time, on a Docker host), what the real HTTP payloads look like,
the empirical verdicts on the F13 residuals **I3** (trailing-zero >2dp
amount strings) and **I4** (zero-volume addresses omitted from balances),
and the F15 fix that brought the Formance MCP service onto the same wire
contract.

**Result: the live suite PASSES (5/5) against `ghcr.io/formancehq/ledger`
v2.3.21 on thebrick, 2026-10-10. I3 and I4 are NOT observed — the F13
strict-parsing assumptions hold and `ledger_parity.py` needed no changes.
`formance_client.py` was reworked as the single wire-format adapter (details
below), with its unit tests updated. The F15 MCP live round-trip also PASSES
(1/1) against the same ledger — see the F15 section.**

## How to run

All commands are from the repo root shipped to the Docker host (e.g.
`thebrick`). One command does everything:

```bash
# 1. Ship the working tree (local machine -> Docker host)
rsync -az --exclude '.git' --exclude '.venv' --exclude '*/__pycache__' \
    --exclude '*.pyc' --exclude 'chat-ui/.venv' --exclude 'formance-mcp/.venv' \
    /Users/johnpellew/agentic-accounting-v3/ thebrick:/tmp/aa-live/

# 2. On the Docker host: venv, compose up (Formance only), health poll, live suite
ssh thebrick 'cd /tmp/aa-live && ./scripts/live-e2e.sh'
```

What `scripts/live-e2e.sh` does, step by step (run each manually if you
prefer):

1. **Venv**: `python3 -m venv api/.venv && api/.venv/bin/pip install -q -e 'api[dev]'`
   (python ≥ 3.11; `aiosqlite`, `pytest-timeout`, `bcrypt==4.0.1`,
   `cryptography<50` pins come from `api/pyproject.toml`; on Ubuntu 24.04
   install `python3.12-venv` first if `ensurepip` is missing).
2. **Stack**: `docker compose up -d formance-postgres formance-ledger` —
   ONLY the Formance services; the API side runs on the real aiosqlite ORM,
   no Postgres docker needed.
3. **Health**: poll `curl -sf http://127.0.0.1:3068/_info` up to 120 s.
4. **Run**:
   `FORNANCE_LEDGER_URL=http://127.0.0.1:3068 .venv/bin/python -m pytest tests/live -v --timeout=120`
5. **Capture**: the shape-capture test writes `live-shapes.json` (env
   `LIVE_CAPTURE_OUT`) at the repo root.

### Teardown

```bash
docker compose stop formance-ledger formance-postgres
docker compose rm -f formance-ledger formance-postgres
rm -rf /tmp/aa-live          # when inspection is done
```

The F14 live run left the containers **running** (`accounting-formance`,
`accounting-formance-pg`) and `/tmp/aa-live` in place for inspection.

## Environment used

- Host: `thebrick` (Ubuntu 24.04, Docker 29.1.3, python 3.12.3).
- Ledger image: `ghcr.io/formancehq/ledger:latest` (v2.3.21, postgres driver).
- Port: `3068` on the host. **The repo's `docker-compose.yml` originally had
  no host port mapping for `formance-ledger`** — the F14 scope needs host
  access, so a minimal `ports: ["${FORNANCE_PORT:-3068}:3068"]` mapping was
  added (the only compose change). Two stale containers from an earlier
  attempt (same names, no port mapping) were stopped/removed to make room;
  their data volumes were left untouched.

## Observed real payload shapes (v2.3.21)

### `POST /v2/{ledger}` (create ledger)

- First call: **204** (empty body). Repeat call: **400**
  `{"errorCode":"LEDGER_ALREADY_EXISTS","errorMessage":"ledger already exists"}`
  — NOT the 409 the client originally tolerated. The client now treats
  400/409 as idempotent success.
- Recorded in the capture: `create_ledger_first_status: 204`,
  `create_ledger_repeat_status: 400`.

### `POST /{ledger}/transactions` (create transaction)

Two real-API constraints forced a wire-format change:

1. **Amounts are minor-unit JSON integers** (e.g. `1000000` = £10,000.00 in
   `GBP/2`). The old major-unit decimal string `"10000.00"` is rejected with
   400 `VALIDATION` ("invalid transaction format").
2. **Non-negative balance enforcement**: any posting that would take a
   non-`world` account below zero is rejected with 400 `INSUFFICIENT_FUND`
   (probe: `source @3000` from zero balance). Only `world` may go negative;
   the v1 "mapping" escape hatch was removed in v2 (formancehq/stack#1597),
   and the only alternative is Numscript **unbounded overdraft**.

So the client now submits every mirror as Numscript — one `send` per posting,
every source allowed to overdraw — which preserves F1 pairing (same
source/destination/asset/amount semantics) while letting credit-nature
accounts hold the negative balances double-entry requires:

```
send [GBP/2 2000000] (
	source = @3000 allowing unbounded overdraft
	destination = @1000
)
send [GBP/2 400000] (
	source = @3000 allowing unbounded overdraft
	destination = @1320
)
```

Probe-verified: digit-only account names (`@3000`) compile, multiple `send`
statements per script work, `reference`/`timestamp`/`metadata` ride alongside
`{"script": {"plain": "..."}}`, and `get_balances` then reports TRUE negative
balances (−200 probe confirmed — no floor at zero).

- Response: `{"data": [{...tx, "txid": N, "id": N, "reverted": false,
  "postCommitVolumes": {...}, "postCommitEffectiveVolumes": {...}}]}`.

### `GET /{ledger}/transactions` (list transactions)

```json
{"cursor":{"pageSize":15,"hasMore":true,"next":"<opaque-base64>",
  "data":[{"postings":[{"source":"3000","destination":"1000","amount":1000000,
  "asset":"GBP/2"}],"metadata":{"transaction_id":"...","description":"..."},
  "timestamp":"2026-10-10T...Z","reference":"JE-2026-0001","id":1,"txid":1,
  "reverted":false,"postCommitVolumes":{...}}]}}
```

- The data list is **inside `cursor`**, not top-level (the shape the client,
  parity and reports originally assumed).
- Amounts are minor-unit integers.
- **Pagination**: follow-up pages send `cursor=<cursor.next>` ALONE. The
  legacy `after` param — numeric or opaque — 500s with `INTERNAL` ("Internal
  error. Consult logs/traces to have more details."), so the client maps its
  `after` argument to `cursor=` and omits `pageSize` on follow-ups (the
  simplest correct form). `pageSize` is honoured on the first page (500
  verified) and values above 1000 clamp to 1000 (2000/5000/100000 all answer
  `pageSize: 1000`). F15 correction: the earlier claim that `pageSize` must
  not accompany `cursor` was wrong — a follow-up `cursor=<next>&pageSize=100`
  is accepted and the companion size overrides the encoded one.
- `metadata` passes through unchanged (parity keys off
  `metadata.transaction_id`).

### `GET /{ledger}/balances`

```json
{"cursor":{"pageSize":1000,"hasMore":false,
  "data":[{"1000":{"GBP/2":2000000}},{"3000":{"GBP/2":-2000000}}, ...]}}
```

- Data inside `cursor`; each entry is `{address: {asset: minor-int}}` (NOT
  `{"address": ..., "balances": {...}}`). Negative values appear for
  overdrawn (credit-nature) accounts — the F2 sign convention survives.
- The client normalises to `{"data": [{"address", "balances": {asset:
  major-str}}]}` and requests `pageSize=1000` (default 15 would truncate).

## I3 / I4 verdicts

**I3 — trailing-zero >2dp amount strings: NOT OBSERVED.** The real wire
carries minor-unit integers (zero decimal places); the client normalises to
exactly-2dp major strings. Capture: `i3_amounts_more_than_2dp: 0`,
`i3_strict_parse_errors: 0`. The F13 strict parser's >2dp rejection never
fires against real payloads — strict assumptions HOLD, no `ledger_parity`
change.

**I4 — zero-volume addresses omitted from balances: NOT OBSERVED.** Probe: a
£500 transfer (1000→1010) posted and then reversed nets 1010 to exactly zero;
the real `get_balances` still lists BOTH addresses (capture:
`i4_zero_volume_addresses_omitted: []`). Zero-balance addresses are retained,
not omitted — the F13 gap-1 "missing address is a finding" behaviour remains
correct, no `ledger_parity` change.

## Code changes made for the live run

1. `api/tests/live/test_live_formance_e2e.py` — the live suite (5 tests:
   create-ledger route, entity setup→approve→real mirror→parity, three
   constructor entries with parity after each, all five F9 reports +
   Postgres cross-check, payload shape capture with pagination walk + I3/I4
   probes), skipif-gated on env + one reachability probe per session.
2. `scripts/live-e2e.sh` — the runbook (venv, compose up, health poll, run,
   teardown instructions).
3. `docs/live-e2e.md` — this document.
4. `docker-compose.yml` — host port mapping for `formance-ledger` (see above).
5. `api/src/services/formance_client.py` — the wire-format adapter:
   Numscript + unbounded-overdraft submission with minor-unit amounts,
   `cursor=`-only pagination, data-inside-cursor envelope normalisation,
   minor→major amount conversion, 400-duplicate create-ledger tolerance,
   `pageSize=1000` balances. **F1 pairing, constructors, catalog,
   orchestrator, report engine, ledger_parity and all their tests are
   unchanged.**
6. `api/tests/unit/test_formance_client.py` — updated to the real wire
   contract (Numscript bodies, minor units, cursor envelope, normalisation,
   duplicate-400 create ledger).

## Contract F15 — Formance MCP wire fix + live MCP round-trip

The F14 run left `formance-mcp/src/formance_mcp/client.py` **out of F14
scope** (see the F14 contract boundaries); it shipped with the same
pre-existing wire bug as the pre-F14 API client — plain decimal-string
postings, which the real ledger rejects with 400 `VALIDATION` (and
credit-nature sources with 400 `INSUFFICIENT_FUND`). F15 removes that gap:
the MCP client now speaks the SAME wire contract as the F14 API client,
self-contained (no `api/` imports), while the MCP tools' public shapes are
unchanged (decimal strings in, decimal strings out — conversion happens on
the wire only).

What changed in `formance-mcp/`:

- `create_transaction` builds Numscript — one `send [<asset> <minor-int>]`
  per posting, `source = @<source> allowing unbounded overdraft` on every
  source — with amounts converted from decimal strings to minor-unit ints
  EXACTLY (`Decimal*100 -> int`, never floats). Idempotency-Key, reference,
  timestamp and metadata ride alongside `{"script": {"plain": ...}}`; the
  real response envelope `{"data": [{"txid": N, ...}]}` collapses to
  `{"txid": N}`.
- `list_transactions` normalises the cursor-embedded envelope
  (`{cursor:{pageSize,hasMore,next,data}}` → flat `{cursor, data}`) and
  converts minor-unit amounts back to exact major-unit decimal strings;
  follow-up pages send `cursor=<next>` alone.
- `get_balances` normalises `{address: {asset: minor-int}}` rows to
  `{"data": [{"address": ..., "balances": {asset: major-str}}]}` and
  requests `pageSize=1000`.
- `create_ledger` (new client method + new tool): tolerates 204 and 400
  `LEDGER_ALREADY_EXISTS` (and legacy 409) as idempotent success.
- Errors keep surfacing as structured tool errors (status + body).

**Result: the live MCP suite PASSES (1/1) against
`ghcr.io/formancehq/ledger` v2.3.21 on thebrick, 2026-10-10.** The
round-trip: create a fresh `mcp-live-<rand>` ledger, then
`create_transaction` source @4000 → destination @1000 for £100.00 through
the REAL tools; `get_balances` shows `+100.00` on 1000 and `-100.00` on
4000 (raw wire: minor-unit `+10000`/`-10000`); `list_transactions` returns
the transaction with decimal-string amounts and the flat cursor envelope.

Run the MCP live suite (on thebrick, from `/tmp/aa-live/formance-mcp`):

```bash
python3 -m venv .venv && .venv/bin/pip install -q -e '.[dev]' pytest-timeout
FORNANCE_LEDGER_URL=http://127.0.0.1:3068 .venv/bin/python -m pytest \
    tests/live -v --timeout=120
```

The F14 pagination section above carries the corrected `pageSize` findings
(pageSize accompanies cursor fine and clamps at 1000).

## Local behaviour

On hosts without a real ledger the live modules skip cleanly:
`FORNANCE_LEDGER_URL` unset → api's 5 live tests and the MCP live test all
skip, local suites unaffected.
Local full-suite runs after F15:

```
cd api && .venv/bin/python -m pytest tests/ -q --no-header -p no:cacheprovider --timeout=30
```

→ 0 failed, 1171 passed, live tests skipped.

```
cd formance-mcp && .venv/bin/python -m pytest -q
```

→ 0 failed, 60 passed, 1 skipped (the live MCP test skips on hosts without
a ledger).

## Fresh start (reset for a new test day)

One-shot full reset of every accounting datastore on thebrick (leaves the
live Katra containers untouched — they mount anonymous/bind volumes, not
the `agentic-accounting_*` named ones):

```bash
# from a synced repo tree on thebrick (e.g. /tmp/aa-live)
docker rm -f accounting-api accounting-gateway accounting-postgres \
             accounting-redis accounting-minio accounting-formance \
             accounting-formance-pg 2>/dev/null || true
docker volume rm agentic-accounting_postgres_data agentic-accounting_minio_data \
                  agentic-accounting_redis_data agentic-accounting_katra_minio_data \
                  agentic-accounting_katra_mongo_data agentic-accounting_katra_redis_data \
                  aa-live_formance_data 2>/dev/null || true
```

First-run sequence for a new test day (blank databases):

```bash
docker compose up -d postgres redis formance-postgres formance-ledger
# wait for health: curl -s http://127.0.0.1:3068/_info  (Formance) and
#   docker exec accounting-postgres pg_isready -U accounting
cd api && DATABASE_URL=postgresql+asyncpg://accounting:accounting@127.0.0.1:5432/accounting \
  .venv/bin/python -m alembic upgrade head
# then start the API (uvicorn src.index:app --port 8000), chat-ui, and use
# POST /api/v1/entities to create the first entity + setup its COA/ledger.
```

The publish working clone at `/tmp/aa-v3-push` is intentionally kept; every
other `/tmp/aa-*` artifact is transient.
