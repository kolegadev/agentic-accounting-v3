---
id: trial-balance
name: Trial balance (debits equal credits)
category: report
frameworks: [uk-gaap, ifrs, us-gaap]
version: 0.1.0
status: v0
---

# Trial balance (debits equal credits)

Lists every active account's balance at a date in two columns — debit
balances and credit balances — and proves the totals are EQUAL. The
foundational verification report: no financial statement is trustworthy
unless the trial balance balances.

## When to use

- "Give me a trial balance as at 31 December", "does the ledger
  balance", "list all account balances"
- Before preparing the P&L and balance sheet; after posting period-end
  adjustments (accruals, prepayments, depreciation)
- Do NOT use for: performance over a period (profit-and-loss), position
  presentation (balance-sheet), cash movements (cash-flow-statement).

## Inputs

| field | type | required | notes |
|-------|------|----------|-------|
| as_at_date | date | yes | the date balances are read at (ISO 8601) |
| period_start | date | no | only for flow columns of a period TB |
| period_end | date | no | only for flow columns of a period TB |
| tb_type | enum | no | `unadjusted` (default) \| `adjusted` \| `post_closing` |
| framework | enum | yes | `uk-gaap` or `ifrs` (presentation only) |
| ledger | string | no | Formance ledger name, default `main` |

## Construction rules

BALANCES at `as_at_date`. For EVERY active account: balance = Σ debits −
Σ credits of posted/reversed postings, integer pence. The SIGN of the
balance picks the column: balance > 0 → debit column (as a positive
amount); balance < 0 → credit column (as a positive amount); balance 0
→ listed as zero or suppressed. Two columns only, plus a totals row.

Debit-nature classes: `Asset (Bank)`, `Asset (CurrentAsset)`,
`Asset (FixedAsset)` — including the `1360` contra which lands in the
credit column — and every `Expense (Expense)` / `Expense (DirectCost)`.
Credit-nature classes: `Liability (CurrentLiability)` (including `2220`
Input VAT, a contra-liability carrying a debit balance),
`Liability (LongTermLiability)`, `Equity (Equity)` and
`Revenue (Revenue)`.

Three types (per period-end procedure):
- unadjusted — before accruals (`2010`), prepayments (`1110`) and
  depreciation (`6500`/`1360`);
- adjusted — after those adjustments;
- post_closing — after P&L accounts (`4000`–`6990` revenue and expense)
  are closed to `3020` Retained Earnings, leaving only balance-sheet
  accounts with non-zero balances.

Identity: Σ debit column = Σ credit column, EXACT. Any difference means
an unbalanced posting exists or an out-of-scope status leaked in — the
ledger is broken and no other report may be produced.

## Framework variants

- The trial balance is a framework-agnostic internal control report:
  neither FRS 102, IFRS nor US GAAP prescribes its format (no ASC topic
  governs it). It feeds the financial statements and the filing layer —
  the iXBRL tagging layer (FRS 102 / FRS 105 taxonomies for HMRC and
  Companies House filings) or SEC XBRL for US registrants.
- us-gaap: the same artifact, named the working trial balance in US audit
  practice, which distinguishes unadjusted, adjusted and post-closing
  columns: the unadjusted TB comes straight from the ledger, adjustment
  columns carry audit/AJE entries, and the post-closing TB (P&L accounts
  closed to retained earnings, as in the worked example) is the tie-out
  to the final ASC 210-10 balance sheet and ASC 220 income statement and
  to the audit lead schedules. None of that changes the debits-equal-
  credits identity.

## Formance interaction

BALANCES: `get_balances` on the ledger returns per-address balances
across assets; the address balance for an account code equals debits −
credits in integer pence (the same expectation `ledger_parity` checks
against Postgres). `list_transactions` is NOT used to build the trial
balance (balances, not flows) — but the posted/reversed scope still
applies: with parity, `get_balances` reflects exactly the posted and
reversed set.

```json
{"balances": [{"address": "1000", "balances": {"GBP/2": "1910.00"}}]}
```

Classification (code → class) comes from the Postgres COA (`accounts`
table); ALL amounts come from Formance. Trust precondition: run
`verify_parity` first; if `ok=False`, refuse to produce the report (or
mark every figure unverified).

## Worked example

Acme Trading Ltd as at 31 Dec 2025 — post_closing trial balance (the
P&L accounts have been closed to `3020` Retained Earnings: revenue
credit 120000000 pence and expense debits 104610000 pence net to a
15390000 credit on `3020`). Amounts in integer pence:

```lines
1000 Bank Current Account | 19100000 | 0
1100 Trade Debtors | 12000000 | 0
1110 Prepayments | 400000 | 0
1310 Office Equipment | 5000000 | 0
1320 Computer Equipment | 25000000 | 0
1360 Accumulated Depreciation | 0 | 5000000
2000 Trade Creditors | 0 | 10000000
2010 Accruals | 0 | 2500000
2040 Corporation Tax Payable | 0 | 3610000
2100 Bank Loan | 0 | 10000000
3000 Share Capital | 0 | 15000000
3020 Retained Earnings | 0 | 15390000
TOTALS | 61500000 | 61500000
```

Debit column 61500000 pence == credit column 61500000 pence, exact.
Each balance is debits − credits: e.g. `1000` bank = 61500000 − 42400000
= 19100000; `1360` accumulated depreciation = 0 − 5000000 = −5000000,
shown as a positive 5000000 in the credit column.

## Verification checks

- Σ debit column == Σ credit column, exact in pence (totals row
  excluded from the sums and must equal them)
- every row has exactly two columns; at most one is non-zero
- the `1360` balance sits in the CREDIT column (contra-asset)
- the bank row == the balance-sheet bank line == the cash-flow closing
  bank line
- balance = debits − credits; the sign picks the column

## Edge cases

- Empty ledger: every row 0; totals 0 = 0 still holds.
- Loss-making period: `3020` Retained Earnings goes NEGATIVE → its
  balance lands in the DEBIT column; totals still equal.
- Negative equity: same as the loss case — debit-column equity balances
  are legal and must not be "corrected".
- Overdrawn bank: `1000` negative → credit column; totals still equal.
- Zero-cash period: bank row 0; identity unaffected.
- First period: no opening balances — the TB still balances if every
  posting balanced.
- VAT-only movement: `2210`/`2220` rows change (equal and opposite to
  bank until settled); totals still equal.
- Zero balances: list or suppress (presentation choice); inactive
  accounts are always excluded.
- Multi-currency: out of scope (asset `GBP/2` only).
