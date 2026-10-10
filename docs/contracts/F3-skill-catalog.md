# CONTRACT F3 — Transaction-Type Skill Catalog v0 (SKILL.md per GAAP/IFRS)

## Goal

A catalog of 10 transaction-type SKILL.md files — each defining the full
workflow: facts required, exact DR/CR construction, VAT treatment, Formance
posting shape, worked example, verification checks, and framework variant
notes (UK GAAP vs IFRS) — plus a machine-checkable registry, validated by
pytest so the catalog cannot silently rot.

## Why (context)

v2 gave the LLM tool schemas with zero accounting knowledge, so it
constructed DR/CR from nothing (the documented failure). These SKILL.md files
are the knowledge layer the deterministic constructors (F4) will be generated
from. The DR/CR tables in these files ARE the contract for F4.

## Boundaries

- NO production code changes (`api/src/**` untouched).
- Existing `skills/registry.yaml` (MCP tool registry) MUST NOT change.
- Additive only: everything under `skills/catalog/` + one test file
  `api/tests/test_skill_catalog.py` + `docs/contracts/F3-*.md`.
- Source material (read, do not modify): `documents/Research/*.md`
  (wide01-06, dim01-12 — June 2025 IFRS18/GAAP/Numscript corpus),
  `api/src/coa_templates/*.json` (UK account codes/categories),
  `docs/contracts/F1-formance-mirror.md` (posting shape).

## The 10 transaction skills

| id | name |
|----|------|
| supplier-invoice | Purchase on credit (with & without VAT variants) |
| supplier-payment | Pay an AP balance from bank |
| sales-invoice | Sale on credit (with & without VAT variants) |
| customer-receipt | Receive an AR balance into bank |
| bank-transfer | Transfer between own bank accounts |
| journal-accrual | Accrued expense / provision journal |
| prepayment | Prepaid expense & deferred income |
| depreciation | Monthly straight-line depreciation |
| payroll | Net pay + PAYE/NIC liabilities |
| opening-balances | Initial capital, assets, liabilities |

## Required file layout

```
skills/catalog/
  registry.yaml                      # index: id, name, category, frameworks, status, path
  transactions/<id>/SKILL.md         # one per skill
```

## REQUIRED SKILL.md sections (validated by tests — exact heading names)

1. YAML frontmatter: `id`, `name`, `category: transaction`, `frameworks`
   (subset of `[uk-gaap, ifrs]`), `version: 0.1.0`, `status: v0`
2. `## When to use` — trigger phrases / situations
3. `## Facts` — the input schema the LLM extracts, as a table:
   field | type | required | notes (use integer pence for money)
4. `## DR/CR construction` — table: leg | account class | amount | notes,
   with separate rows per VAT variant and per framework where they differ
5. `## VAT treatment` — uk-gaap input/output VAT handling per scheme
   (standard/flat-rate notes where relevant)
6. `## Formance interaction` — the posting dicts per the F1 contract
   (`source: credit account code`, `destination: debit account code`,
   amount: exact decimal string, asset: `GBP/2`), plus reference/metadata
7. `## Worked example` — full numeric example, EVERY amount in pence, and the
   example MUST balance (sum(debits) == sum(credits)); state the Formance
   postings it produces
8. `## Verification checks` — invariants the F4 constructor will enforce
   (sum-to-zero, VAT split exactness, account-class checks)
9. `## Edge cases` — partial payments, roundings, zero-VAT, mixed VAT rates
10. `## Framework variants` — uk-gaap vs IFRS differences for this type;
    `us-gaap: TODO` where the US treatment differs (explicitly marked)

## Registry format (`skills/catalog/registry.yaml`)

A list of entries: `id`, `name`, `category`, `frameworks`, `status`,
`path` (relative to skills/catalog/). Every file must be listed; every
listed path must exist.

## Validation (the test)

`api/tests/test_skill_catalog.py` (pytest, no DB/network) must verify:
1. 10 skills present, registry complete and consistent (bidirectional).
2. Every SKILL.md has all 10 required section headings.
3. Frontmatter parses and constraints hold (id matches directory, frameworks
   valid, status v0).
4. Worked examples balance: parse the example's debit/credit amounts (they
   must be extractable — use a fenced code block `\`\`\`dr-cr` in the worked
   example section) and assert exact equality in pence.
5. DR/CR tables parse and each leg's account class appears in the UK COA
   template categories (Asset/Liability/Equity/Revenue/Expense + account
   types used by the templates).
6. Every skill's Formance section contains at least one posting dict with
   source/destination/amount/asset keys and no `world`.

## Success criteria

1. Test file passes; full suite:
   `cd api && .venv/bin/python -m pytest tests/ -q --no-header -p no:cacheprovider --timeout=30`
   → 0 failed, 0 errors, count ≥ 878.
2. `git diff --name-only` shows only files under `skills/catalog/`,
   `api/tests/test_skill_catalog.py`, and the F3 contract doc.
3. Accounting spot-check (verifier): depreciation = Dr Depreciation Expense /
   Cr Accumulated Depreciation; supplier invoice VAT = Dr Expense net +
   Dr VAT Input / Cr AP gross; sales VAT = Dr AR gross / Cr Revenue net +
   Cr VAT Output; payroll = Dr Salaries gross / Cr Bank net + Cr PAYE +
   Cr NIC; opening balances = Dr assets / Cr equity+liabilities. At minimum
   these five must be correct.
