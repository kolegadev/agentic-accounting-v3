---
id: cash-flow-statement
name: Cash flow statement (indirect method)
category: report
frameworks: [uk-gaap, ifrs, us-gaap]
version: 0.1.0
status: v0
---

# Cash flow statement (indirect method)

Reconciles profit to the movement in bank balances over a period:
operating + investing + financing = net change in cash, EXACT in pence.
Indirect method — starts from profit, adds back depreciation, adjusts
for working capital, then investing and financing flows.

## When to use

- "Show me the cash flow statement for 2025", "where did the cash go",
  "reconcile profit to the bank movement"
- Year-end statutory accounts, bank covenant reporting, board packs
- Do NOT use for: performance (profit-and-loss), position at a date
  (balance-sheet), debit=credit verification (trial-balance).

## Inputs

| field | type | required | notes |
|-------|------|----------|-------|
| period_start | date | yes | first day of the report period (ISO 8601) |
| period_end | date | yes | last day of the report period, inclusive |
| framework | enum | yes | `uk-gaap` or `ifrs` — classification differences only |
| method | enum | no | `indirect` only in v0 (`direct` is out of scope) |
| ledger | string | no | Formance ledger name, default `main` |

## Construction rules

Mix of PERIOD FLOWS (profit, bank movements) and BALANCES at
`period_start` / `period_end` (working-capital deltas). All amounts
integer pence; ΔX = closing balance − opening balance.

- 1. Start: profit for the period (the P&L recipe's single figure).
- 2. Add back non-cash: the `6500` Depreciation debit flow of the
  period.
- 3. Working capital (indirect adjustments):
  - Δ`1100` Trade Debtors > 0 → SUBTRACT (increase in debtors is an
    outflow: cash not yet collected)
  - Δ`1110` Prepayments > 0 → SUBTRACT (cash paid in advance)
  - Δ`2000` Trade Creditors > 0 → ADD (increase in creditors is an
    inflow: purchases not yet paid)
  - Δ`2010` Accruals > 0 → ADD
  - Δ`2020` Deferred Income > 0 → ADD
  - Δ`2040` Corporation Tax Payable > 0 → ADD (tax charged but not yet
    paid). The charge = the GROSS credit flow on `2040` (= closing −
    opening + tax paid); the provision entry MUST pair Dr `3020`
    (equity) / Cr `2040` — debiting an expense account for the
    provision double-counts in the P&L and breaks the cash-flow
    identity (the engine raises).
  - Δ`2030` PAYE / NI Payable > 0 → ADD (analogous)
  - VAT (`2210`/`2220`/`2200`/`2230`): quarterly settlements move VAT
    through the bank within the period — netted through the bank-driven
    reconciliation; only an UNSETTLED net VAT movement appears as an
    adjustment.
  - Decreases reverse each sign.
- 4. Investing: fixed asset purchases = the increase in `1300`–`1350`
  cost balances (debit flow, net of disposals) → SUBTRACT (outflow);
  disposal proceeds → ADD.
- 5. Financing: `2100` Bank Loan drawdowns ADD, repayments SUBTRACT;
  `3000` Share Capital introduced ADD; `3030` Dividends Paid (or `3010`
  Drawings, sole trader) SUBTRACT.
- 6. Reconciliation: operating + investing + financing == change in the
  bank balances `1000` + `1010`, EXACT. Cross-check against the
  bank-driven narrative: opening bank + receipts − payments == closing
  bank, and every non-bank movement must belong to one of the
  categories above.

## Framework variants

- IFRS (IAS 7): indirect or direct method; interest and dividends are
  classified per the entity's policy (operating, investing or
  financing). IFRS 18 amendments to IAS 7 (effective 1 Jan 2027): the
  indirect method starts from OPERATING profit; interest paid → financing;
  dividends paid → financing; interest and dividends received →
  investing.
- uk-gaap (FRS 102 §7): single statement; interest paid shown
  separately (policy: operating or financing). FRS 102 §1A small
  entities and FRS 105 micro-entities are NOT required to prepare a
  cash flow statement at all.
- us-gaap (ASC 230): operating/investing/financing classification is
  mandatory and the indirect method is near-universal in practice, though
  ASC 230-10-45-25 ENCOURAGES the direct method (gross major classes of
  operating receipts and payments). Unlike IAS 7's policy choice,
  interest paid, interest received, dividends received and income taxes
  paid are classified OPERATING (dividends paid are financing) — no
  discretion. Regardless of method, ASC 230-10-50-2 REQUIRES supplemental
  disclosure of interest paid and income taxes paid, and non-cash
  investing/financing activity must be disclosed (ASC 230-10-50-3) — an
  interest-paid mandate IAS 7 does not have. Public companies present
  three years of statements of cash flows; IFRS requires one comparative
  year.

## Formance interaction

Period flows: `list_transactions` on the ledger with `page_size 500`,
following the cursor; keep only transactions whose
`metadata.transaction_id` belongs to a Postgres transaction with status
`posted` or `reversed` (the F2 scope convention). For each posting the
`destination` account is debited (+amount) and the `source` account
credited; convert decimal-string amounts to integer pence with
Decimal(str(amount)) × 100 (the parity verifier's conversion).

Balances: `get_balances` at `period_start` and `period_end` for every
working-capital delta (address balance = debits − credits in pence).

```json
[
  {"source": "3000", "destination": "1000", "amount": "1500.00", "asset": "GBP/2"}
]
```

That posting is share capital introduced: `1000` bank debited, `3000`
credited — financing inflow 150000 pence.

Classification (code → class) comes from the Postgres COA (`accounts`
table); ALL amounts come from Formance. Trust precondition: run
`verify_parity` first; if `ok=False`, refuse to produce the report.

## Worked example

Acme Trading Ltd, first year of trading, year ended 31 Dec 2025.
Profit £153,900; depreciation £50,000; trade debtors rose £120,000;
prepayments rose £4,000; trade creditors rose £100,000; accruals rose
£25,000; corporation tax payable rose £36,100 (tax charged, unpaid);
fixed assets purchased £300,000; bank loan drawn £100,000; share
capital introduced £150,000. Bank-driven narrative: receipts from
customers £1,080,000 + loan £100,000 + capital £150,000 = £1,330,000
in; supplier payments £600,000 + overhead payments £229,000 + interest
£10,000 + fixed assets £300,000 = £1,139,000 out → closing bank
£191,000. Amounts in integer pence:

```lines
OPERATING
Profit for the period | 15390000
Add back: Depreciation 6500 | 5000000
Increase in Trade Debtors 1100 | -12000000
Increase in Prepayments 1110 | -400000
Increase in Trade Creditors 2000 | 10000000
Increase in Accruals 2010 | 2500000
Increase in Deferred Income 2020 | 0
Increase in Corporation Tax Payable 2040 | 3610000
Net cash from operating activities | 24100000
INVESTING
Fixed asset purchases 1300-1350 | -30000000
Net cash from investing activities | -30000000
FINANCING
Bank Loan drawdown 2100 | 10000000
Share Capital introduced 3000 | 15000000
Net cash from financing activities | 25000000
RECONCILIATION
Net change in cash | 19100000
Bank opening balance (1000+1010) | 0
Bank closing balance (1000+1010) | 19100000
```

Operating: 15390000 + 5000000 − 12000000 − 400000 + 10000000 +
2500000 + 0 + 3610000 = 24100000. Reconciliation: 24100000 +
(−30000000) + 25000000 = 19100000 == closing bank 19100000 − opening 0,
exact.

## Verification checks

- operating + investing + financing == net change in cash == closing
  bank − opening bank, exact in pence
- each section's subtotal line == the sum of that section's other lines
- the depreciation add-back == the P&L's `6500` line; the profit line
  == the P&L's profit figure
- net change in cash == the balance-sheet bank movement (Δ`1000`+`1010`)
- sign rules: increases in assets (`1100`, `1110`) negative; increases
  in liabilities (`2000`, `2010`, `2020`, `2040`) positive

## Edge cases

- Empty period: every line 0; net change 0.
- Zero-cash period: net change 0 — but composition may differ (e.g.
  profit absorbed by a debtor increase); the statement still balances.
- Loss-making period: profit is NEGATIVE — operating cash can still be
  positive after add-backs and working-capital inflows.
- Overdrawn bank: bank balances negative; the change is still
  closing − opening.
- First period: opening bank 0; no opening balances for deltas.
- VAT-only movement: settled VAT nets through the bank reconciliation;
  only unsettled net VAT movements appear as adjustments.
- Tax provision convention: the corporation-tax charge is the gross
  credit flow on `2040` (= closing − opening + paid); the provision
  entry MUST pair Dr `3020` (equity) / Cr `2040`. Debiting an expense
  account for the provision double-counts the charge in the P&L and
  breaks the operating + investing + financing == change-in-bank
  identity — the engine raises.
- Negative equity: irrelevant here (the statement is about cash, not
  net worth).
- Direct method: out of scope v0 (indirect only).
- Multi-currency: out of scope (asset `GBP/2` only).
