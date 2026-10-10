# CONTRACT F16 — US GAAP Catalog Content (replace all us-gaap TODOs)

## Goal

Replace every `us-gaap: TODO` marker across the 15 catalog SKILL.md files
(10 transaction + 5 report) with real, substantive US GAAP treatment —
ASC citations and US-specific mechanics — bringing the catalog to three
frameworks: uk-gaap, ifrs, us-gaap. Registry + validation tests updated
accordingly.

## Source material (read; do not modify)

- `documents/Research/accounting_system_wide06.md` and the dim docs — the
  June-2025 corpus already carries the ASC references (ASC 230, 205/220,
  210-10, 740, 606, 360-10, 310, 340-10, 405, 450, 710, 250, 210-20) and
  the US/UK/IFRS differences (sales tax vs VAT, FICA/FUTA/SUTA vs PAYE/NIC,
  direct-method cash flow preference, liquidity ordering, deferred tax).
- The existing `## Framework variants` sections in the 15 SKILL.md files —
  match the depth of the uk-gaap/ifrs bullets (F3/F8 set the bar: each
  framework gets substantive, non-boilerplate content).

## Per-file requirements

For EACH of the 15 files, the `## Framework variants` section must gain a
complete US GAAP bullet set (comparable depth to uk-gaap/ifrs) covering,
as applicable to that skill:

| Skill | Required US GAAP content (minimum) |
|---|---|
| supplier-invoice | sales tax (state/local) NOT VAT — recoverable only if resale/direct-use; ASC 330 inventory costing when capitalized; use tax note |
| supplier-payment | prompt-payment discount ASC 605-50 / ASC 606 consideration; sales-tax element treatment mirrors output side |
| sales-invoice | ASC 606 five-step revenue recognition (identify contract, performance obligation, price, allocation, recognise); sales tax liability (Cr sales tax payable, not revenue); ASC 310 receivables |
| customer-receipt | ASC 310-10 bad debt allowance/aging; sales tax remittance cycle note |
| bank-transfer | framework-agnostic (state explicitly, like the uk-gaap/ifrs merged bullet) |
| journal-accrual | ASC 450 loss contingencies (probable+estimable) vs FRS 102 §21; ASC 720 expense accrual |
| prepayment | ASC 340-10 prepaid expenses; deferred revenue under ASC 606 contract liabilities |
| depreciation | MACRS (tax) vs book: ASC 360-10 PP&E, component depreciation, impairment ASC 360 vs IAS 36 (US no-reversal rule) |
| payroll | FICA (Social Security 6.2% + Medicare 1.45%) employee + employer; FUTA/SUTA; federal/state income tax withholding; 941/940 filings; no PAYE/NIC concept |
| opening-balances | ASC 250 error corrections (restatement vs current-period) for opening adjustments; ASC 805/ASC 830 (functional currency) only as pointers |
| profit-and-loss | ASC 220 single/multi-step; SEC S-X 5-03 line items; discontinued operations ASC 205-20; no IFRS-18-style five-category mandate |
| balance-sheet | ASC 210-10 current vs non-current ordering (US: assets then liabilities then equity); ASC 740 deferred tax assets/liabilities classification |
| trial-balance | framework-agnostic internal-control report (state explicitly; post-closing vs unadjusted vs adjusted, US audit workpaper role) |
| cash-flow-statement | ASC 230 indirect method universal in practice; direct method ENCOURAGED; interest paid/income tax paid disclosure REQUIRED regardless of method; operating/investing/financing classification of interest differs from IAS 7 policy choice |
| funds-statement | FASB 95 (1987) REPLACED the statement of changes in financial position — US GAAP has NO funds-statement requirement; if requested, it is a management report, not GAAP financials (cite the history) |

Every US GAAP bullet must carry its ASC citation (regex-testable) and at
least one US-specific mechanical difference (not a restatement of the
uk-gaap bullet). No bullet may contain `TODO` or placeholder text.

## Registry + validation updates

1. `skills/catalog/registry.yaml`: every entry's `frameworks` gains
   `us-gaap` (15 entries).
2. `api/tests/test_skill_catalog.py` + `api/tests/test_report_catalog.py`:
   the valid-framework sets accept `us-gaap`; ADD a new check (one test
   function per file, or a shared helper): every catalog SKILL.md contains
   NO `us-gaap: TODO` / `TODO` literal, and its Framework variants section
   contains at least one `ASC \d{3}` reference; frontmatter `frameworks`
   includes `us-gaap` and matches the registry entry.

## Boundaries

- ONLY `skills/catalog/**` (15 SKILL.md + registry.yaml) + the two
  validation test files + this contract.
- NO production code changes (constructors/engine/parity/client all
  untouched — the constructors' us-gaap TODO notes in facts/entry code are
  out of scope).

## Success criteria

1. `grep -rn "us-gaap: TODO\|TODO" skills/catalog/` → empty.
2. `grep -rln "ASC [0-9]" skills/catalog/` → all 15 files.
3. Full suite:
   `cd api && .venv/bin/python -m pytest tests/ -q --no-header -p no:cacheprovider --timeout=30`
   → 0 failed, 0 errors, ≥ 1171 (new framework checks add a few tests).
4. `git status --porcelain -uall` = allowed paths only.
5. Verifier spot-check: 5 skills' US GAAP bullets are accounting-correct
   (FICA rates, ASC 606 steps, MACRS vs book, ASC 230 interest disclosure,
   FASB 95 history).
