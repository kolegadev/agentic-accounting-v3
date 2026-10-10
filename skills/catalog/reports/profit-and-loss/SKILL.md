---
id: profit-and-loss
name: Income statement (profit and loss)
category: report
frameworks: [uk-gaap, ifrs]
version: 0.1.0
status: v0
---

# Income statement (profit and loss)

Derives profit for a period from the ledger's PERIOD FLOWS (never
balances): revenue (credit-nature) minus ALL costs and expenses
(debit-nature). Profit = revenue − direct costs − overheads − staff costs
− depreciation − finance costs − tax. Depreciation REDUCES profit.

## When to use

- "Show me the P&L for 2025", "income statement for Q1", "did we make a
  profit last month"
- Month-end, quarter-end and year-end performance reporting
- Do NOT use for: position at a single date (balance-sheet), cash
  movements (cash-flow-statement), debit=credit verification
  (trial-balance), where-the-money-went narrative (funds-statement).

## Inputs

| field | type | required | notes |
|-------|------|----------|-------|
| period_start | date | yes | first day of the report period (ISO 8601) |
| period_end | date | yes | last day of the report period, inclusive |
| framework | enum | yes | `uk-gaap` or `ifrs` — presentation only, amounts identical |
| ledger | string | no | Formance ledger name, default `main` |
| compare_periods | array | no | prior periods for comparison columns (v0: single period only) |

## Construction rules

Period flows only — sum postings whose transaction is in scope (see
Formance interaction). For each posting the DESTINATION account received
the debit flow and the SOURCE account the credit flow. Revenue and
expense accounts carry their balances through the period; the P&L reads
the FLOW, not the closing balance. All amounts integer pence.

- Revenue — credit flow, positive. Codes `4000` Sales - Standard Rated,
  `4010` Sales - Reduced Rated, `4020` Sales - Zero Rated, `4030` Sales -
  Exempt (class `Revenue (Revenue)`), plus `4050` Other Operating
  Income. `4060` Bank Interest Received is revenue but sits in the
  investing category under IFRS 18 (see Framework variants). Sum =
  turnover.
- Direct costs — debit flow, positive. Class `Expense (DirectCost)`:
  `5000` Cost of Sales, `5010` Materials & Consumables, `5020` Goods for
  Resale, `5100` Subcontractor Costs, `5110` Freight & Delivery.
- Gross profit (subtotal) = Σ revenue − Σ direct costs.
- Overheads — debit flow, positive. Class `Expense (Expense)`: `6000`
  Rent & Rates, `6010` Utilities, `6020` Water Rates, `6030` Insurance,
  `6040` Telephone, `6050` Office Supplies, `6100` Advertising, `6110`
  Website, `6200` Travel, `6210` Motor Fuel, `6220` Motor Repairs,
  `6300` Professional Fees, `6310` Accountancy, `6400` Bank Charges,
  `6510` Repairs & Maintenance, `6700` Sundry Expenses, `6990` Rounding.
- Staff costs — debit flow, positive: `6600` Staff Wages, `6610`
  Employer's NI, `6620` Staff Training, `6630` Pension Contributions.
- Depreciation — debit flow, positive: `6500` Depreciation. REDUCES
  profit; it is a non-cash charge, added back only in the cash flow
  statement.
- Operating profit (subtotal) = gross profit − overheads − staff costs −
  depreciation. Mandatory subtotal under IFRS 18; shown by convention
  under FRS 102.
- Finance costs — debit flow, positive: `6410` Loan Interest.
- Profit before tax (subtotal) = operating profit − finance costs.
- Corporation tax — charge for the period = closing balance of `2040`
  Corporation Tax Payable − opening balance + tax PAID in the period
  (debit postings to `2040`). Positive charge reduces profit.
- Profit for the period = revenue − ALL expense lines above. One single
  profit figure; it must equal the movement in `3020` Retained Earnings
  plus dividends paid (`3030` Dividends Paid).

Identity: Σ revenue − Σ expense = profit, exact in pence.

## Framework variants

- IFRS 18 (effective 1 Jan 2027): all income and expenses fall into five
  categories — operating (this COA's revenue, direct costs, overheads,
  staff costs, depreciation), investing (`4060` bank interest, gains on
  investments), financing (`6410` loan interest), income tax (the `2040`
  charge) and discontinued operations (no COA codes — nil). Mandatory
  subtotals: operating profit; profit before financing and income tax;
  profit or loss. Management-defined performance measures (MPMs) need
  disclosure when used publicly.
- uk-gaap (FRS 102 §5): no five-category structure. Companies Act 2006
  format 1 (by function: cost of sales → gross profit → distribution and
  admin) or format 2 (by nature: raw materials, staff costs and
  depreciation as separate lines — this COA's layout is by nature).
  FRS 102 §1A small entities may file abbreviated accounts; FRS 105
  micro-entities do not file a P&L at all.
- us-gaap: TODO — ASC 205/220 prescribe no single format: single-step or
  multiple-step; SEC Regulation S-X 5-03 line items apply to public
  filers; three years of income statements vs two under IFRS.

## Formance interaction

Period flows: `list_transactions` on the ledger with `page_size 500`,
following the cursor (the same pagination `ledger_parity` uses). Keep
only transactions whose `metadata.transaction_id` belongs to a Postgres
transaction with status `posted` or `reversed` — the F2 scope
convention; `draft` and `rejected` never appear.

For each posting `{source, destination, amount, asset}`: `destination` is
the account debited → debit flow +amount; `source` is the account
credited → credit flow +amount. Convert the decimal-string amount to
integer pence exactly (Decimal(str(amount)) × 100 — the same
`_decimal_to_pence` conversion the parity verifier uses). The credit leg
is the source because `build_formance_postings` pairs credit→source and
debit→destination.

```json
[
  {"source": "4000", "destination": "1100", "amount": "7200.00", "asset": "GBP/2"}
]
```

That posting is a sale on credit: `1100` debited (gross), `4000`
credited — revenue flow 720000 pence.

Classification (code → class) comes from the Postgres COA (`accounts`
table); ALL amounts come from Formance. `get_balances` is NOT used for
the P&L (flows, not balances) — it enters only through `verify_parity`.
Trust precondition: run `verify_parity` first; if `ok=False`, refuse to
produce the report (or mark every figure unverified).

## Worked example

Acme Trading Ltd, first year of trading, year ended 31 Dec 2025. Revenue
£1,200,000 (`4000` £720,000 standard + `4020` £480,000 zero-rated),
direct costs £700,000 (`5010` £420,000 + `5020` £280,000), overheads
£250,000, depreciation £50,000, no staff in year 1, loan interest
£10,000, corporation tax 19% × £190,000 = £36,100. VAT is settled
quarterly (net nil at the period end) and never touches the P&L.
Amounts in integer pence:

```lines
REVENUE
Sales - Standard Rated 4000 | 72000000
Sales - Reduced Rated 4010 | 0
Sales - Zero Rated 4020 | 48000000
Sales - Exempt 4030 | 0
DIRECT COSTS
Materials & Consumables 5010 | 42000000
Goods for Resale 5020 | 28000000
OVERHEADS
Rent & Rates 6000 | 8000000
Utilities 6010 | 5000000
Water Rates 6020 | 1000000
Insurance 6030 | 2000000
Professional Fees 6300 | 6000000
Accountancy 6310 | 3000000
DEPRECIATION
Depreciation 6500 | 5000000
STAFF COSTS
Staff Wages 6600 | 0
Employer's NI 6610 | 0
FINANCE AND TAX
Loan Interest 6410 | 1000000
Corporation Tax 2040 | 3610000
PROFIT
Profit for the period | 15390000
```

Subtotals in pence: gross profit 50000000 (120000000 − 70000000);
operating profit 20000000 (50000000 − 25000000 − 0 − 5000000); profit
before tax 19000000 (20000000 − 1000000); profit 15390000 (19000000 −
3610000). Identity: Σ revenue 120000000 − Σ expense 104610000 =
15390000 exactly.

## Verification checks

- Σ REVENUE section − Σ every other section except PROFIT == the single
  PROFIT line, exact in pence
- exactly one profit figure (the PROFIT section holds one line)
- every revenue line ≥ 0; every expense line ≥ 0 (debit-nature positive)
- depreciation `6500` is a positive expense and REDUCES profit: profit
  < revenue − direct costs − overheads whenever depreciation > 0
- corporation tax line == Δ`2040` + tax paid in the period
- profit == movement in `3020` Retained Earnings + `3030` dividends paid

## Edge cases

- Empty period: every line 0, profit 0 — the report is still valid.
- Loss-making period: expenses exceed revenue → profit NEGATIVE (the
  single profit figure is negative; retained earnings will fall).
- Depreciation-only month: negative profit equal to the charge.
- First period: no comparatives; opening retained earnings is 0.
- VAT-only movement: output/input VAT live on `2210`/`2220`/`2230` and
  never enter the P&L — a VAT settlement month shows no P&L change.
- Zero-rated/exempt sales (`4020`/`4030`): revenue recognised in full;
  no VAT leg exists for them.
- Negative equity (cumulative losses) is a balance-sheet consequence of
  negative profits, not a P&L input.
- Overdrawn bank and zero-cash periods do not change the recipe (bank
  never appears in the P&L).
- Multi-currency: out of scope (asset `GBP/2` only).
