# CONTRACT F37 — Ledger-Derived AR/AP Aging + Account Drill-Down

## Goal

Two findings from John's manual test:

1. The Aged Receivables / Aged Payables reports "do not calculate and do
   not align with the Balance Sheet."  Root cause (already known): F30's
   aging reads the ORPHANED Postgres invoice tables — chat-approved
   transactions never create Invoice records — so the buckets are always
   empty while the Balance Sheet (Formance) shows the real debtors 1100 /
   creditors 2000 balances.  The aging must become LEDGER-DERIVED so its
   total equals the trial-balance row by construction.
2. Clicking an account line in any Formance report must open that
   account's transactions (date, reference, description, DR/CR for that
   account, running balance).

## A. Ledger-derived aging (replaces the F30 invoice-based aging)

### Engine (`api/src/services/formance_reports.py` — now ALLOWED)

1. New `generate_ar_aging(db, ledger, as_at, entity_id)` and
   `generate_ap_aging(...)`:
   - Pull the POSTED transaction set from Postgres (posted+reversed
     scope — mirror the F9/F2 scope discipline; entity_id filter; join
     postings).
   - AR: items = transactions with a DEBIT posting to 1100 (the invoice
     moment; amount = that posting's debit), receipts = transactions with
     a CREDIT posting to 1100, both ordered by effective_date asc.
     Allocate receipts FIFO against the oldest outstanding invoice
     remainder (exact pence, partial allocations allowed).  Each item:
     {ref (transaction.reference or id-prefix), date (effective_date),
     original_pence, paid_pence, outstanding_pence, days_overdue
     (as_at − date)}.  Buckets 0-30 / 31-60 / 61-90 / 90+ from
     outstanding items (paid-fully items appear in `lines` with
     outstanding 0).
   - AP: identical on 2000 (credits = supplier-invoice moments, debits =
     supplier-payment moments).
   - `ledger_balance` = the TB row for 1100 / 2000 via `_ledger_balances`
     (+ COA name); `total_outstanding_pence` MUST equal the absolute TB
     balance — assert it (ReportEngineError on violation); expose
     `reconciled: true` in the payload (the TB equality IS the
     reconciliation).
   - Return shape (route-facing): {report: "aged_receivables", as_at,
     items: [...], buckets: [{bucket, count, total_pence}],
     total_outstanding_pence, ledger_balance: {code, name, balance_pence},
     reconciled: bool, note}.
2. `__all__`/exports updated.

### Route (`api/src/routes/formance_reports.py`)

3. The `AGING_TYPES` branch now calls the ENGINE functions (drop the
   ReportService.run_ar_aging / run_ap_aging calls).  Response keeps the
   F30 shape: {report_type, as_at, report{items, buckets,
   total_outstanding_pence, ledger_balance, reconciled, note},
   cross_check{ok: null, diffs: [], note}}.  ReportEngineError → 422 as
   today.  The note drops the "aging is invoice-derived" wording —
   aging is now derived from the SAME ledger the Balance Sheet reads.

### UI (`chat-ui/index.html`)

4. Aging renderer: add an items table (Date | Reference | Original |
   Paid | Outstanding | Days overdue, fmtMoney everywhere), the buckets
   table, total row, and the ledger-balance line with a "✓ reconciles to
   the Balance Sheet" badge when `reconciled` is true (red note when
   false).  All values through esc()/fmtMoney.

## B. Account drill-down

### API

5. `TransactionService.list_for_account(db, code, *, entity_id=None,
   date_from=None, date_to=None, limit=100)` (NEW method in
   `api/src/services/transaction_service.py` — allowed to ADD this one
   method only): posted+reversed-scope transactions whose postings
   reference an account with that code (entity-scoped); returns items
   {transaction_id, reference, description, effective_date,
   debit_pence, credit_pence (THIS account's leg), status} ordered by
   effective_date DESC, id DESC; plus `running_balance_pence` computed
   forward from the oldest (balance = Σ debits − Σ credits, in the
   returned order show the cumulative running balance).
6. New route GET `/api/v1/accounts/{code}/transactions` (put it in
   `api/src/routes/transactions.py`; check the router prefix): params
   entity_id, date_from, date_to, limit(≤200); unknown/inactive code →
   404 {type: account_not_found}; returns {code, name, items,
   running_balance_pence, count}.

### chat-ui proxy + UI

7. `chat-ui/server.py`: proxy GET `/api/accounts/{code}/transactions`
   → `/api/v1/accounts/{code}/transactions` (query passthrough,
   502 pattern like the others; use the standard 10s client).
8. `chat-ui/index.html`: new account-detail modal (`#account-modal`,
   esc-pattern like the others): title "Account 1100 — Trade Debtors",
   a table of the transactions (Date | Ref | Description | Debit |
   Credit | Balance) with fmtMoney, close button.  Wire the EXISTING
   clickable-cell handler: `sendTableCellQuery` no longer fills the chat
   input — clicking a 4-digit code cell (Formance report tables AND
   answer tables, both flow through renderTable) opens the modal via
   `openAccount(code)` (fetch the proxy route with the current
   entity_id).  The chat-input query behaviour is REMOVED (the modal
   replaces it).
9. Guard: only cells matching a 4-digit code open the modal (the F31
   renderTable guard already limits clickable cells to those).

### Tests

10. Engine unit tests (scripted data): AR invoice+partial+full receipt
    FIFO allocation exact pence, aging buckets, AP mirror, TB-equality
    assert fires on sabotage, reconciled flag.
11. Service/route tests: list_for_account filters (status, entity,
    date), running balance, 404 unknown code, limit clamp; route shape.
12. e2e (test_formance_reports_e2e.py): seed sales-invoice +
    customer-receipt (partial) through the REAL orchestrator/constructors
    → aged-receivables route → items/buckets/total == TB 1100 balance,
    reconciled true; drill-down route returns the two postings with a
    correct running balance.
13. chat-ui contract test additions: account-modal ids + proxy path
    string presence.
14. Full suite: api ≥ 1372, chat-ui ≥ 46, formance-mcp 60 — all green.

## Boundaries

- ALLOWED: `api/src/services/formance_reports.py` (aging generators),
  `api/src/services/transaction_service.py` (list_for_account ONLY),
  `api/src/routes/formance_reports.py`, `api/src/routes/transactions.py`,
  `chat-ui/server.py`, `chat-ui/index.html`, the test files, this
  contract.
- NOT: constructors, catalog, orchestrator, contact/coa services,
  `report_service.py` (the legacy run_ar_aging/run_ap_aging stay for the
  legacy /reports/run surface but are no longer used by the Formance
  pane), formance-mcp, validators.

## Success criteria

1. Suites green (above).
2. Live check: with Acme's real posted data the AR/AP totals equal the
   Balance Sheet debtors/creditors exactly (reconciled badge); clicking
   the 1100 line in any report opens its transactions.
