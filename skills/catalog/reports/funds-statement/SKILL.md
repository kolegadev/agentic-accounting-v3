---
id: funds-statement
name: Statement of sources and application of funds
category: report
frameworks: [uk-gaap, ifrs, us-gaap]
version: 0.1.0
status: v0
---

# Statement of sources and application of funds

Management report answering "where did the money come from and where
did it go": long-term SOURCES (profit, depreciation, borrowings,
capital) balanced against long-term APPLICATIONS (fixed assets, loan
repayments, drawings) plus the movement in working capital. The
long-term vs working-capital split is explicit and the statement
balances EXACTLY in pence.

## When to use

- "Where did our funds come from this year", "sources and uses of
  funds", "funds flow for the bank"
- Financing applications, year-end management commentary, explaining a
  profit-and-cash gap to directors
- Do NOT use for: performance (profit-and-loss), position at a date
  (balance-sheet), the statutory cash flow reconciliation
  (cash-flow-statement).

## Inputs

| field | type | required | notes |
|-------|------|----------|-------|
| period_start | date | yes | first day of the report period (ISO 8601) |
| period_end | date | yes | last day of the report period, inclusive |
| framework | enum | yes | `uk-gaap` or `ifrs` (labelling only — no statutory format) |
| ledger | string | no | Formance ledger name, default `main` |

## Construction rules

Mix of PERIOD FLOWS (long-term items) and BALANCES at `period_start` /
`period_end` (working-capital deltas). All amounts integer pence.

- LONG-TERM SOURCES (the capital side, not day-to-day trading):
  - profit for the period (the P&L recipe's single figure)
  - `6500` Depreciation — a non-fund charge, added back to profit
  - long-term borrowings: credit flow on `2100` Bank Loan (drawdowns)
  - capital introduced: credit flow on `3000` Share Capital (or `3000`
    Capital Account, sole trader)
- LONG-TERM APPLICATIONS:
  - fixed asset purchases: debit flow on `1300`–`1350` (cost accounts)
  - loan repayments: debit flow on `2100`
  - drawings / dividends: debit flow on `3010` Drawings (sole trader) or
    `3030` Dividends Paid (limited company)
- WORKING CAPITAL (the balancing movement):
  - increase in working capital = Δ current assets − Δ current
    liabilities, where current assets are `1000`+`1010` bank, `1100`
    Trade Debtors, `1110` Prepayments, `1120` Accrued Income, `1130`
    VAT Recoverable, `1200` Stock, `1210` Work in Progress and current
    liabilities are `2000` Trade Creditors, `2010` Accruals, `2020`
    Deferred Income, `2030` PAYE / NI Payable, `2040` Corporation Tax
    Payable and the VAT net (`2210`−`2220`).
  - the increase in working capital is the BALANCING figure: Σ
    long-term sources = Σ long-term applications + increase in working
    capital. A working-capital DECREASE is a source (flip the
    presentation, keep the arithmetic).
- Identity: Σ sources = Σ applications, EXACT in pence, with the
  working-capital increase shown as the final application line.

## Framework variants

- uk-gaap history: SSAP 10 "Statements of source and application of
  funds" was the UK standard until FRS 1 replaced it with cash flow
  statements in 1991. Today the funds statement is a voluntary internal
  report under FRS 102 — no prescribed format — still widely used in
  bank financing narratives.
- IFRS: no equivalent standard; IAS 7 cash flows is the required
  statement. An IFRS reporter preparing a funds statement does so as a
  management report, not a primary statement.
- us-gaap: no funds statement. SFAS 95 (FASB Statement of Financial
  Accounting Standards No. 95, issued November 1987, effective for
  fiscal years ending after 15 July 1988) superseded APB Opinion No. 19
  and eliminated the statement of changes in financial position from US
  GAAP; SFAS 95 was codified as ASC 230 in 2009. A US GAAP reporter
  issues the statement of cash flows instead. If a funds statement is
  prepared anyway (e.g. for a credit memorandum), it is a management
  report outside the GAAP financial statements — not a primary
  statement, never audited as GAAP.

## Formance interaction

Period flows for the long-term items: `list_transactions` on the
ledger with `page_size 500`, following the cursor; keep only
transactions whose `metadata.transaction_id` belongs to a Postgres
transaction with status `posted` or `reversed` (the F2 scope
convention). For each posting the `destination` account is debited
(+amount) and the `source` account credited; convert decimal-string
amounts to integer pence with Decimal(str(amount)) × 100 (the parity
verifier's conversion).

Balances for the working-capital split: `get_balances` at
`period_start` and `period_end` (address balance = debits − credits in
pence).

```json
[
  {"source": "2100", "destination": "1000", "amount": "1000.00", "asset": "GBP/2"}
]
```

That posting is a loan drawdown: `1000` bank debited, `2100` credited —
a long-term source of 100000 pence.

Classification (code → class) comes from the Postgres COA (`accounts`
table); ALL amounts come from Formance. Trust precondition: run
`verify_parity` first; if `ok=False`, refuse to produce the report.

## Worked example

Acme Trading Ltd, first year of trading, year ended 31 Dec 2025.
Profit £153,900; depreciation £50,000; bank loan drawn £100,000; share
capital introduced £150,000; fixed assets purchased £300,000; no loan
repayments, no drawings. Working capital moved by +£153,900: current
assets rose £315,000 (bank £191,000 + trade debtors £120,000 +
prepayments £4,000) while current liabilities rose £161,100 (trade
creditors £100,000 + accruals £25,000 + corporation tax payable
£36,100). Amounts in integer pence:

```lines
SOURCES
Profit for the period | 15390000
Depreciation (non-fund charge) 6500 | 5000000
Long-term borrowings 2100 | 10000000
Capital introduced 3000 | 15000000
APPLICATIONS
Fixed asset purchases 1300-1350 | 30000000
Loan repayments 2100 | 0
Drawings / dividends | 0
Increase in working capital | 15390000
WORKING CAPITAL SPLIT
Increase in current assets (bank + 1100 + 1110) | 31500000
Increase in current liabilities (2000 + 2010 + 2040) | 16110000
Net working capital movement | 15390000
```

Sources 45390000 pence (15390000 + 5000000 + 10000000 + 15000000) ==
applications 45390000 pence (30000000 + 0 + 0 + 15390000), exact.
Long-term funds of £453,900 financed £300,000 of fixed assets; the
remaining £153,900 increased working capital
(31500000 − 16110000 = 15390000).

## Verification checks

- Σ SOURCES == Σ APPLICATIONS, exact in pence
- the increase in working capital == Δ current assets − Δ current
  liabilities (the WORKING CAPITAL SPLIT arithmetic)
- sources' profit line == the P&L profit; sources' depreciation == the
  P&L `6500` line
- applications' fixed asset purchases == the cash-flow investing line;
  applications' borrowings/capital == the cash-flow financing lines
- the long-term vs working-capital split is explicit: no current-asset
  or current-liability movement (other than the balancing line) sits
  inside SOURCES or APPLICATIONS

## Edge cases

- Empty period: every line 0; 0 == 0 holds.
- Loss-making period: the profit source is NEGATIVE — funds then come
  only from borrowings, capital and the depreciation add-back.
- Working-capital DECREASE: the balancing line goes negative — present
  it as a source instead (Σ sources = Σ applications still holds).
- Overdrawn bank: bank enters the current-assets split negatively; the
  arithmetic is unchanged.
- Zero-cash period: bank delta 0; working-capital movements net out.
- First period: no opening balances; deltas equal closing balances.
- VAT-only movement: settled VAT nets inside the bank line; only
  unsettled net VAT movements affect the working-capital split.
- Negative equity: irrelevant to sources/uses — the statement is about
  funds, not net worth.
- Multi-currency: out of scope (asset `GBP/2` only).
