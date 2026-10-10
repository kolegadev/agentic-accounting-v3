# CONTRACT F2 — Ledger Parity Verifier (Postgres ⇄ Formance)

## Goal

A verifier service that proves, at any point in time, that the Formance ledger
is a faithful double-entry image of the Postgres ledger: same transactions,
same legs, same per-account balances — and reports every discrepancy precisely.

## Background

F1 (committed 52e311c) fixed the mirror: every posted/reversed transaction in
Postgres is mirrored to Formance with paired postings where
`source = credit account code`, `destination = debit account code`, amount =
exact decimal string. Consequence: for any account, Formance balance
(inflows − outflows) equals the Postgres balance (debits − credits) exactly,
including the sign convention — this is what parity verifies.

## Boundaries

- MUST NOT change any existing service, route, model, validator, or the F1
  mirroring logic. This feature is additive: a new module + tests.
- MAY add: `api/src/services/ledger_parity.py`, tests under
  `api/tests/unit/test_ledger_parity.py` and
  `api/tests/integration/test_ledger_parity.py`, and (only if needed) small
  additions to `formance_client.py` such as a `delete_transaction` or
  `count_transactions` method used by tests — no signature changes to
  existing client methods.

## Semantics (must be exact)

1. **Scope:** Postgres transactions with status `posted` or `reversed` are
   mirrored; `draft` is not. Parity compares exactly that set.
2. **Transaction-level parity:** for every in-scope Postgres transaction,
   Formance has a transaction with matching `reference` (JE ref) and
   `metadata.transaction_id` (Postgres UUID), and the multiset of Formance
   postings equals what `build_formance_postings` produces for that
   transaction's legs. And conversely: every Formance transaction in the
   ledger under test carries a `metadata.transaction_id` that exists in
   Postgres (no orphans, no foreign rows).
3. **Balance-level parity:** for every active account, Formance
   `get_balances` value equals Postgres Σ(debit_amount) − Σ(credit_amount)
   over in-scope postings, in integer pence after converting the decimal
   string back exactly (use Decimal, never float).
4. **Drift detection:** missing mirror, extra Formance row, wrong leg, wrong
   amount, wrong account code, or balance mismatch must each produce a
   precise report entry (transaction id, expected vs actual).

## Deliverable

- `ParityReport` dataclass: `ok: bool`, `missing_in_formance: list`,
  `orphans_in_formance: list`, `leg_mismatches: list`, `balance_mismatches:
  list`, `summary: str`. Each entry carries transaction/reference ids and
  expected vs actual values.
- `async def verify_parity(db, ledger: str = "main") -> ParityReport` — reads
  Postgres via `db`, Formance via the existing `formance_client` singleton;
  Formance unreachable → report with `ok=False` and a distinct `error` field
  (do not raise).
- Amounts: convert Formance decimal strings back to pence via
  `Decimal(amount) * 100` → `int` (exact).

## Success criteria

1. New unit tests: every drift type above is detected; perfect-parity case
   reports `ok=True`; Formance-unreachable reports error (not raise).
2. New integration test (real sqlite+aiosqlite AsyncSession, same pattern as
   `test_transaction_orm_reversal.py`): post 2 transactions + 1 reversal
   through the real services with a scripted fake Formance client that
   records the mirror calls and serves them back through `list_transactions`
   / `get_balances`; verify_parity returns `ok=True`; then sabotage one
   mirror (drop a transaction, alter an amount, add an orphan) and prove the
   report flags exactly that.
3. Full suite green:
   `cd api && .venv/bin/python -m pytest tests/ -q --no-header -p no:cacheprovider --timeout=30`
   → 0 failed, 0 errors, count ≥ 860.
4. `git diff --name-only` shows only new/changed files listed above.
