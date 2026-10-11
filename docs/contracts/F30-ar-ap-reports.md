# CONTRACT F30 — Aged Receivables / Aged Payables in the Formance Reports Pane

## Goal

John's manual test: "There is no Accounts Receivable Report or Accounts
Payable Report — these need to be built and added to the Formance Reports
function."  Add two new report types — `aged-receivables` and
`aged-payables` — to the Formance reports route + pane, combining the
existing Postgres aging services (ReportService.run_ar_aging /
run_ap_aging) with an entity-scoped LEDGER BALANCE line from the engine so
the aging total can be reconciled against the debtors/creditors accounts.

## Existing pieces (do not re-invent)

- `ReportService.run_ar_aging(db, data: ReportRunRequest) -> AgedARReport`
  and `run_ap_aging(...)` — bucket unpaid invoices / unmatched expense
  postings into 0-30 / 31-60 / 61-90 / 90+ buckets (pence).  NOT
  entity-scoped (invoices have no entity_id) — state this honestly.
- `AgedARReport` / `AgedAPReport` (api/src/validators/report.py):
  as_of_date, lines, buckets, total_outstanding (pence), generated_at.
- `ReportRunRequest` (same validators module) — read its exact required
  fields before constructing it (the aging services take `data.end_date`
  as the as-of date).
- `generate_tb(db, ledger, as_at=..., entity_id=...)` — the engine TB; the
  debtors (1100) / creditors (2000) row is the ledger balance.
- Route `api/src/routes/formance_reports.py`: `VALID_REPORT_TYPES`,
  engine branch, cross-check block, JSON date encoding — mirror the
  existing balance-type pattern (as_at only, no period requirement).

## Changes (exact)

### A. Route (`api/src/routes/formance_reports.py`)

1. New constant `AGING_TYPES = ("aged-receivables", "aged-payables")`;
   `VALID_REPORT_TYPES` includes them.  The invalid-type 422 message then
   lists them automatically.
2. New branch BEFORE the engine dispatch: for `report_type in AGING_TYPES`
   → build `ReportRunRequest(report_type=..., end_date=as_at or today)`
   (fill whatever other fields the model requires — READ it), call the
   matching ReportService runner, then compute the entity-scoped ledger
   balance via `generate_tb(db, ledger, as_at=as_at or today,
   entity_id=scope_id)` — take the 1100 row for aged-receivables / the
   2000 row for aged-payables (missing row → `balance_pence: 0` with a
   `ledger_account_missing: true` flag, not an error).  Response 200:

   ```json
   {
     "report_type": "aged-receivables",
     "as_at": "YYYY-MM-DD",
     "report": {
       "buckets": [{"bucket": "0-30", "count": n, "total_pence": n}, ...],
       "lines": [{"contact_name": ..., "ref": ..., "due_date": ..., "amount_pence": ..., "days_overdue": ...}],
       "total_outstanding_pence": n,
       "ledger_balance": {"code": "1100", "name": "Trade Debtors", "balance_pence": n},
       "note": "Aging covers all invoices (invoices are not entity-scoped in this release); the ledger balance is scoped to the selected entity."
     },
     "cross_check": {"ok": null, "diffs": [], "note": "aging is invoice-derived (Postgres); ledger balance is Formance-derived — the difference is unpaid-draft timing, not a defect"}
   }
   ```

   Map the AgedAR/AgedAPReport fields into that shape; amounts stay
   integer pence; dates ISO.  Aging service exceptions → 422
   `{type: "report_engine_error"}` (same style as the engine branch).
3. Aging types must NOT demand period_start/period_end (balance-type
   behaviour) and must NOT flow into the engine branch.

### B. UI (`chat-ui/index.html`)

4. Report-type select gains "Aged Receivables" + "Aged Payables"
   (existing select + renderer switch — find them, extend them).
5. Renderer for the aging shape: buckets table (Bucket | Count | Total
   with fmtMoney), a total row, the ledger-balance line ("Ledger balance —
   1100 Trade Debtors: £x.xx"), and the note as a muted caption.  Reuse
   esc()/fmtMoney/renderTable helpers; never inject raw values.
6. Period inputs: hide/disable for aging types (match the existing
   balance-type behaviour in the pane).

### C. Tests

7. Route tests (find the existing reports-route test file): each aging
   type returns 200 with the shape above (mock the runner + engine TB);
   entity scope reaches generate_tb; missing ledger account → zero +
   ledger_account_missing; runner exception → 422; invalid type still 422
   and the message lists the aging types.
8. E2E (extend the existing Formance-reports e2e file): seed one unpaid
   invoice and assert the AR buckets/total via the route.  NOTE: that
   fixture may lack a `contacts` table — if the AR join needs it, add the
   table to the fixture DDL INSIDE the test file (allowed); if AP e2e is
   simpler (no contact join), cover AP in e2e and AR in route tests.
9. Full suite bars: api ≥ 1345, chat-ui ≥ 40, formance-mcp 60 — all green.

## Boundaries

- ALLOWED: `api/src/routes/formance_reports.py`, `chat-ui/index.html`,
  the reports route/e2e test files, this contract.
- NOT: `report_service.py`, validators, the report engine
  (`formance_reports.py`), transaction/contact services, Formance MCP,
  the orchestrator (F29 files are done — do not touch them).

## Success criteria

1. Suites green (above).
2. Live check after deploy: Formance pane runs both reports for Acme —
   buckets render, the ledger-balance line matches the debtors/creditors
   row of the trial balance, note visible.
