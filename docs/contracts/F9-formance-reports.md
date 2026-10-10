# CONTRACT F9 — Formance-Derived Report Engine

## Goal

`api/src/services/formance_reports.py` — computes all 5 reports with
**amounts from the Formance ledger and classification from the Postgres COA**,
following the F8 Construction rules. Every report enforces its identity in
code, and P&L/BS/TB are cross-checked against the existing Postgres
`ReportService` (they must agree exactly when ledger parity holds). This is
the second half of the thesis: reports derived from the immutable ledger,
not from LLM arithmetic.

## Data flow (exact)

- **Balance-based reports (BS, TB):** `formance_client.get_balances` →
  per-address/per-asset balances → convert decimal strings to pence exactly
  (Decimal × 100 → int, same as F2) → classify each address via a
  `code → (category, type)` map built from the COA (same classes as
  `resolve_from_coa`) → aggregate per the F8 recipes.
- **Flow-based reports (P&L, CF, funds):** `formance_client.list_transactions`
  (follow cursor pagination) → scope = posted + reversed mirrors only
  (metadata.transaction_id present in the Postgres posted/reversed set —
  same rule as F2) → accumulate net flows per account code over the period
  (Python-side timestamp filtering on `timestamp`) → apply the recipes.
  Working-capital movements and the change-in-bank are period NET FLOWS of
  the relevant accounts (no historical-balance query needed — a period net
  flow IS the balance change).
- **Sign conventions:** Formance balance = inflows − outflows; for credit
  accounts this is negative (correct credit balance). Recipes must state the
  exact sign handling per line (BS shows AP/equity as positive liabilities
  by negating the Formance credit-balance; P&L revenue positive, expenses
  positive-deductions).

## Deliverables

1. `api/src/services/formance_reports.py`:
   - `build_code_class_map(coa_accounts) -> dict[str, str]` (code →
     "Category (Type)")
   - `generate_pl(db, ledger, period_start, period_end) -> dict` — line
     items per F8 P&L recipe; identity: revenue − expenses == profit
   - `generate_bs(db, ledger, as_at) -> dict` — identity: Σassets ==
     Σliabilities + Σequity (1360 netted within assets)
   - `generate_tb(db, ledger, as_at) -> dict` — identity: Σdebits == Σcredits
   - `generate_cash_flow(db, ledger, period_start, period_end) -> dict` —
     indirect method; identity: operating + investing + financing ==
     change in bank (computed as period net bank flow)
   - `generate_funds(db, ledger, period_start, period_end) -> dict` —
     identity: Σsources == Σapplications
   - Each function raises `ReportEngineError` on identity violation or
     classification gaps (unknown account code) — never returns a broken
     report silently.
   - `verify_against_postgres(db, report_dict, report_type)` — where a
     Postgres-side equivalent exists (P&L, BS, TB via `ReportService`),
     compare totals per comparable line and return a diff list; empty =
     agreement.
2. Tests:
   - `api/tests/unit/test_formance_reports.py` — scripted fake Formance
     (balances + paged transactions), mocked DB COA: every report computed
     correctly from a known ledger state; identity-violation fixtures raise
     ReportEngineError; unknown code raises; decimal exactness (£0.01
     survives); posted/reversed scope filtering; pagination followed;
     sign conventions (credit accounts).
   - `api/tests/integration/test_formance_reports_e2e.py` — REAL aiosqlite
     ORM; seed a mini scenario THROUGH THE F6 ORCHESTRATOR + F4 constructors
     (a real sales-invoice, supplier-invoice, depreciation journal, loan
     drawdown, capital injection) with a scripted Formance recorder serving
     the mirrors back; then:
     (a) all 5 reports satisfy their identities;
     (b) P&L/BS/TB `verify_against_postgres` returns an empty diff
         (Formance-derived == Postgres-derived exactly);
     (c) sabotage one mirror amount → identity violation or cross-check
         diff flags it.
   - A static binding test (in the unit file): the engine's line-item
     class lists match the class lists parsed from the F8 SKILL.md
     Construction rules (the catalog stays the spec).
3. Full suite:
   `cd api && .venv/bin/python -m pytest tests/ -q --no-header -p no:cacheprovider --timeout=30`
   → 0 failed, 0 errors, count ≥ 1002.

## Boundaries

- ADDITIVE: new module + two test files + this contract. Read-only use of
  `formance_client`, `ledger_parity` helpers, `ReportService`,
  `CoaService`. No changes to existing services/modules (if a truly minimal
  helper is needed elsewhere, it must be justified in known_limitations —
  otherwise fail the contract).
- No float anywhere on the money path (Decimal × 100 → int conversions only).

## Success criteria

1. Suite ≥ 1002 green, 0 failed.
2. `git status --porcelain -uall` = only the new module, the two test files,
   and the contract doc.
3. Verifier spot-checks: P&L profit equals revenue − costs; BS has 1360 as
   contra-asset; CF starts at profit and adds back depreciation; funds
   splits long-term vs working capital; TB totals equal.
