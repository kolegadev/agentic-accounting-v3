# CONTRACT F24 — Balance Sheet includes current-year profit/loss in equity

## Goal

`generate_bs` (F9 engine) computes equity from the equity ACCOUNTS only, so
the moment any income or expense is posted the accounting identity
A = L + E fails and the report 422s (live-observed: after a £60 supplier
invoice, assets 1,001,000p vs liabilities 6,000p + equity 1,000,000p —
the missing 5,000p is exactly the expense). Real balance sheets include
**current-year profit or loss** in equity. Fix: the BS equity section gains
a "Profit for the period" line (flow-derived), and the identity becomes
assets == liabilities + equity + current_profit.

## Changes (exact)

1. `api/src/services/formance_reports.py::generate_bs`:
   - Accept an optional `period_start: date | None = None` (the route will
     pass the entity's fiscal-year start when known; None = inception,
     i.e. `date.min`).
   - Compute `current_profit_pence` = net flow of all P&L accounts
     (Revenue 4000-4030 + DirectCost 5010/5020 + Expense 6000-6630 +
     the engine's tax convention 2040) from `period_start` to `as_at`,
     using the SAME flow machinery as `generate_pl` (postings with
     `metadata.transaction_id` in the posted+reversed scope; exact
     integer pence; sign: revenue positive, expenses negative).
   - Add the line to the equity section: `{line: "profit_for_period",
     name: "Profit for the period", amount: current_profit_pence}` and
     include it in the equity subtotal AND in the identity check
     (assets == liabilities + equity_with_profit).
   - Identity failure still raises `ReportEngineError` (unchanged
     behavior for genuinely broken ledgers).
2. `api/src/routes/formance_reports.py::formance_report` — for
   balance-sheet/trial-balance (balance types), resolve the entity's
   fiscal-year start (reuse the existing entity lookup path; no entity →
   `None`) and pass `period_start` to `generate_bs` (trial-balance is
   unaffected — pass nothing). The fiscal-window helper from F23b is
   reused to derive the start date.
3. Books pane: no changes (it already consumes the fixed report).

## Tests

- Unit (`test_formance_reports.py`): a scripted ledger with capital +
  a posted expense → generate_bs identity holds, equity section contains
  `profit_for_period` = −expense (exact pence), assets == liabilities +
  equity; income-only case; expense + VAT-input case (the live £60 shape:
  Dr 5000 expense, Dr 1000 VAT input, Cr 6000 creditors → profit −5000,
  VAT recoverable 1000 in assets, identity exact). `period_start` filtering
  honored (expense before the window excluded).
- Route test: balance-sheet passes the fiscal-year-derived period_start to
  the engine (mock assertion) when the entity resolves; None when no
  entity.
- e2e (`test_formance_reports_e2e.py`): extend the seeded scenario with a
  supplier-invoice posted and approved → generate_bs identity ok, P&L
  profit == BS profit_for_period (exact), verify_against_postgres empty
  for P&L/BS (ReportService.run_bs has no profit line — the cross-check
  compares section TOTALS excluding the new line, or documents the delta;
  state which).
- Full suite: api ≥ 1291, chat-ui ≥ 25, formance-mcp 60.

## Boundaries

- ONLY: `formance_reports.py` (generate_bs + shared flow helper reuse),
  `routes/formance_reports.py` (pass period_start), the three test files,
  this contract.
- NOT: generate_pl/cash_flow/funds semantics, constructors, catalog,
  parity, chat-ui, clients.

## Success criteria

1. Suites green (above).
2. Live re-check on thebrick: after redeploy, `/api/books` for the demo
   entity returns 200 with a real explanation; the Formance balance sheet
   identity holds with the posted £60 expense (profit_for_period = −5000p).
3. `git status --porcelain -uall` = allowed paths only.
