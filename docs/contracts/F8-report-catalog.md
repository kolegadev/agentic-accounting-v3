# CONTRACT F8 — Report-Type Skill Catalog (5 report SKILL.md files)

## Goal

Extend the skills catalog with 5 report-type SKILL.md files — the derivation
recipes for the financial statements the thesis requires — so the F9 report
engine can be generated from them the same way F4's constructors were
generated from the transaction skills. Every report has exact construction
rules, framework variants, a numeric worked example, and machine-checkable
verification identities.

## The 5 report skills

| id | name |
|----|------|
| profit-and-loss | Income statement (revenue − costs = profit) |
| balance-sheet | Statement of financial position (A = L + E) |
| trial-balance | All accounts, debits == credits |
| cash-flow-statement | Indirect method (operating/investing/financing) |
| funds-statement | Statement of sources & application of funds |

## File layout

```
skills/catalog/reports/<id>/SKILL.md
```
plus 5 new entries in `skills/catalog/registry.yaml`
(`category: report`, same fields as the transaction entries).

## REQUIRED sections (validated by tests)

1. YAML frontmatter: `id`, `name`, `category: report`, `frameworks`
   (subset of `[uk-gaap, ifrs]`), `version: 0.1.0`, `status: v0`
2. `## When to use`
3. `## Inputs` — table: field | type | required | notes. Every report takes
   `period_start`/`period_end` (P&L, TB, CF, funds) and/or `as_at_date`
   (BS, TB). Framework selector `framework: uk-gaap | ifrs`.
4. `## Construction rules` — the derivation recipe. For EACH section and
   line item: which account classes/codes flow in, with what SIGN, and the
   subtotal structure. Must reference the REAL UK COA account classes and
   codes from `api/src/coa_templates/*.json` (Revenue 4000/4010/4020/4030,
   DirectCost 5010/5020, Expense 6000/6010/6020/6030/6300/6310/6500/6600/6610,
   Bank 1000/1010, AR 1100, AP 2000, Accruals 2010, Deferred 2020,
   VAT 2210/2220, Equity 3000/3020, Loan 2100, Prepayments 1110, FixedAssets
   1300-1350, AccDep 1360). For cash-flow-statement: the indirect-method
   adjustments (add back depreciation 6500, working-capital movements from
   1100/2000/1110/2010/2020, investing from 1300-1350, financing from
   2100/3000/3020) and the reconciliation identity.
5. `## Framework variants` — real uk-gaap (FRS 102 §§5-7, FRS 105) vs IFRS
   differences: IFRS 18's five P&L categories (operating/investing/financing/
   income tax/discontinued) and mandatory subtotals vs UK GAAP format;
   FRS 102 §7 cash flow layout vs IAS 7; balance sheet liquidity ordering
   differences. `us-gaap: TODO` where US treatment differs.
6. `## Formance interaction` — the exact ledger reads: `get_balances`
   (per-address balances for BS/TB), `list_transactions` with the F1
   metadata (posted+reversed scope only, same as F2) for P&L/CF/funds
   period flows; note that classification (code → class) comes from the
   Postgres COA while ALL amounts come from Formance; reference
   `verify_parity` as the trust precondition.
7. `## Worked example` — a complete numeric example in integer pence using
   a fenced ```lines block (report line item | amount pence) that the
   validation test parses; MUST satisfy the report's identities:
   - P&L: Σrevenue − Σexpense = profit (single profit figure present)
   - BS: Σasset lines = Σliability lines + Σequity lines (exact)
   - TB: Σdebit balances = Σcredit balances (exact)
   - CF: operating + investing + financing = net change in cash = change
     in the Bank line item shown (exact)
   - funds: Σsources = Σapplications (exact)
8. `## Verification checks` — the identities above as executable assertions
   plus sign checks (expenses debit-nature positive in P&L, etc.)
9. `## Edge cases` — empty period, loss-making period (negative profit),
   negative equity, overdrawn bank, zero-cash period, first period (no
   opening balances), VAT-only movement, multi-currency out of scope.

## Validation tests

`api/tests/test_report_catalog.py` (new) verifies:
1. 5 report skills present; registry lists them (category: report),
   bidirectional consistency (all 5 paths exist and are listed).
2. All 9 required section headings present per file.
3. Frontmatter constraints (category: report, frameworks valid, status v0).
4. Every worked example parses and satisfies its identity EXACTLY in pence
   (the identities above are computed by the test, not just asserted as
   text).
5. Construction rules reference account classes/codes that exist in the COA
   templates (like F3's check).
6. Formance interaction sections mention get_balances/list_transactions and
   the posted+reversed scope; never mention `world`.

## Boundaries

- NO changes to api/src/**, existing catalog files, existing tests.
- Additive: `skills/catalog/reports/**`, registry.yaml additions,
  `api/tests/test_report_catalog.py`, this contract.

## Success criteria

1. `cd api && .venv/bin/python -m pytest tests/ -q --no-header -p no:cacheprovider --timeout=30`
   → 0 failed, 0 errors, count ≥ 991.
2. `git status --porcelain -uall` shows only the allowed paths.
3. Accounting spot-checks (verifier): P&L profit figure is revenue −
   direct costs − expenses; BS contains accumulated depreciation as a
   contra-asset (reducing fixed assets, not equity); CF starts from profit
   and adds back depreciation; funds statement distinguishes long-term
   sources from working-capital movements.
