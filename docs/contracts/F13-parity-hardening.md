# CONTRACT F13 — Parity Hardening (the 7 F2 gaps)

## Goal

Make `verify_parity` fail-closed and complete. Today it can report `ok=True`
while drift hides in corners the F2 verifier proved: malformed balance
payloads with zero expected nets, inactive accounts, duplicate mirror rows,
foreign ledger addresses, sub-penny amounts, mixed assets. After F13, any
of these makes the report `ok=False` with a precise entry.

## The 7 gaps and their fixes (all in `api/src/services/ledger_parity.py`)

1. **False ok on malformed balances** — `_safe_pence` degrades garbage to 0
   and missing addresses read as 0, so an all-zero-net ledger + garbage
   payload reports ok. FIX: strict parsing — malformed/unparseable balance
   values become `parse_errors` entries and `ok=False`; a Formance address
   expected from the mirror set but ABSENT from the balances payload is
   reported (missing_balance_addresses), not silently 0.
2. **Inactive-account drift invisible** — the balance loop iterates only
   `is_active` accounts. FIX: the balance universe = every COA account with
   any in-scope posting (posted+reversed), regardless of `is_active`, PLUS
   every Formance address that holds a nonzero balance. Inactive accounts
   with postings are compared like any other.
3. **Duplicate Formance rows sharing a valid transaction_id unflagged at tx
   level** — FIX: group Formance rows by `metadata.transaction_id`;
   >1 rows for one id → `duplicate_rows` entries (both txids listed).
4. **Only the first candidate row is diffed when several claim an id** —
   FIX: diff EVERY row claiming an id; each mismatch gets its own entry.
5. **Foreign ledger addresses never examined** — FIX: every Formance
   balance address not resolvable to a COA account (unknown code) → a
   `foreign_addresses` entry with its value → `ok=False`.
6. **Sub-penny truncation** — `_decimal_to_pence` truncates (1.005 → 100).
   FIX: in strict mode, a balance string with >2 decimal places is a
   `parse_error` (reject), never silently truncated.
7. **Mixed-asset conflation** — `_parse_balances` sums all assets per
   address. FIX: strict mode parses per-asset; the asset `GBP/2` (or the
   COA currency + "/2") is compared; any other asset with a nonzero value →
   `foreign_assets` entry → `ok=False`.

## Implementation shape

- `ParityReport` gains: `parse_errors: list`, `duplicate_rows: list`,
  `foreign_addresses: list`, `foreign_assets: list`,
  `missing_balance_addresses: list` (all empty in a clean ledger).
- `_parse_balances(raw, strict: bool = False)` — current tolerant behavior
  is the default (F9's engine and existing callers keep working unchanged);
  `verify_parity` calls it with `strict=True`.
- `_decimal_to_pence(raw, strict: bool = False)` — strict rejects >2dp.
- `ok` = all lists empty AND no drift AND no error. Any new finding flips
  `ok=False` and appears in `summary`.
- Existing behavior for well-formed ledgers is IDENTICAL (all existing
  parity tests stay green; e2e sabotage tests still pass).

## Tests

- `api/tests/unit/test_ledger_parity.py` additions (mocked client):
  - gap 1: garbage balance payload + zero-net ledger → ok=False with
    parse_errors; expected address missing from balances →
    missing_balance_addresses.
  - gap 2: inactive account with postings and Formance drift → flagged
    (previously invisible).
  - gap 3: two Formance rows with the same transaction_id → duplicate_rows.
  - gap 4: two rows same id, one wrong legs → leg_mismatch for BOTH rows'
    diff (not only first).
  - gap 5: Formance balance address "9999" (not in COA) → foreign_addresses.
  - gap 6: balance "1.005" → parse_error (strict), not truncation.
  - gap 7: address holding "USD/2 50" alongside GBP → foreign_assets.
  - perfect-ledger case still ok=True with all new lists empty.
- `api/tests/integration/test_ledger_parity.py` additions: duplicate-mirror
  sabotage and foreign-address sabotage on the real-ORM + recorded-mirror
  setup → ok=False with the right entries; well-formed e2e → ok=True,
  empty new lists.
- Full suite:
  `cd api && .venv/bin/python -m pytest tests/ -q --no-header -p no:cacheprovider --timeout=30`
  → 0 failed, 0 errors, count ≥ 1139.

## Boundaries

- ONLY `api/src/services/ledger_parity.py` + its test files + this contract.
- F9's engine, F6's approve flow, F11/F12 status endpoint, and every other
  consumer must keep working unchanged (strict mode is opt-in inside
  verify_parity; default parsing behavior preserved).
