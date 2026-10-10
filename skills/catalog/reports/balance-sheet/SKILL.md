---
id: balance-sheet
name: Statement of financial position (balance sheet)
category: report
frameworks: [uk-gaap, ifrs, us-gaap]
version: 0.1.0
status: v0
---

# Statement of financial position (balance sheet)

Snapshot of assets, liabilities and equity at a single date, from
BALANCES (never period flows). Identity: assets = liabilities + equity,
EXACT in pence. Accumulated depreciation is a contra-ASSET (it reduces
fixed assets, never equity).

## When to use

- "Show me the balance sheet as at 31 December", "what do we own and
  owe right now", "statement of financial position"
- Year-end statutory accounts, month-end position reporting, financing
  applications
- Do NOT use for: performance over a period (profit-and-loss), cash
  movements over a period (cash-flow-statement), debit=credit
  verification (trial-balance).

## Inputs

| field | type | required | notes |
|-------|------|----------|-------|
| as_at_date | date | yes | the reporting date (ISO 8601) |
| framework | enum | yes | `uk-gaap` or `ifrs` — presentation only, amounts identical |
| ledger | string | no | Formance ledger name, default `main` |
| comparatives | array | no | prior dates for comparison columns (v0: single date only) |

## Construction rules

BALANCES at `as_at_date` only. balance(account) = Σ debits − Σ credits
of posted/reversed postings, integer pence (see Formance interaction).

- ASSETS:
  - `Asset (Bank)`: `1000` Bank Current Account, `1010` Bank Deposit
    Account.
  - `Asset (CurrentAsset)`: `1100` Trade Debtors, `1110` Prepayments,
    `1120` Accrued Income, `1130` VAT Recoverable, `1200` Stock, `1210`
    Work in Progress.
  - `Asset (FixedAsset)` at cost: `1300` Land & Buildings, `1310` Office
    Equipment, `1320` Computer Equipment, `1330` Motor Vehicles, `1340`
    Plant & Machinery, `1350` Fixtures & Fittings — summed at cost.
  - Contra-asset: `1360` Accumulated Depreciation — SUBTRACTED from
    fixed assets (carried as a negative amount in the assets column).
    Reduces ASSETS, NEVER equity.
  - Net fixed assets = Σ(`1300`–`1350`) − `1360`.
- LIABILITIES:
  - `Liability (CurrentLiability)`: `2000` Trade Creditors, `2010`
    Accruals, `2020` Deferred Income, `2030` PAYE / NI Payable, `2040`
    Corporation Tax Payable.
  - VAT net: `2210` Output VAT − `2220` Input VAT (both
    `Liability (CurrentLiability)`; `2220` is a contra-liability
    carrying a debit balance); add `2200` VAT Control and `2230`
    Deferred VAT if in use. If the net is NEGATIVE (reclaimable),
    present it as the `1130` VAT Recoverable asset instead.
  - `Liability (LongTermLiability)`: `2100` Bank Loan, `2110`
    Director's Loan.
- EQUITY:
  - `Equity (Equity)`: `3000` Share Capital, `3010` Share Premium.
  - `3020` Retained Earnings — must equal cumulative profit less
    dividends; `3030` Dividends Paid is contra-equity (subtract).
- Sole-trader template: `3000` Capital Account, `3010` Drawings
  (contra-equity), `3020` Retained Profit; `1010` Cash in Hand; `1330`
  Accumulated Depreciation — identical recipe, same identity.
- Identity: Σ asset lines = Σ liability lines + Σ equity lines, EXACT.
  Any difference means an out-of-scope posting or a parity break.

## Framework variants

- IFRS (IAS 1, IFRS 18 amendments): current/non-current split required
  unless a liquidity presentation is more relevant; non-current-first
  ordering is permitted (increasing liquidity) unlike US GAAP; minimum
  line items including PPE. IFRS 16 leases: right-of-use asset and
  lease liability have no COA codes (client-created); leases currently
  post to `6000` Rent until lease accounts are added.
- uk-gaap (FRS 102 §4): Companies Act formats — fixed assets first,
  then current assets, with a "total assets less current liabilities"
  net-assets presentation and capital and reserves below; FRS 102 §20
  splits finance and operating leases; FRS 105 micro-entity abridged
  balance sheet (fewer line items, minimal notes); iXBRL tagging to the
  FRS 102 / FRS 105 taxonomies (HMRC CT600; Companies House structured
  filing from April 2027).
- us-gaap (ASC 210-10): classified balance sheet with current assets
  FIRST in decreasing liquidity, then non-current assets, then
  liabilities, then equity — the mirror image of IFRS's permitted
  non-current-first ordering, and no Companies-Act-style "net assets"
  presentation (A = L + E is the format). Current classification turns on
  the one-year/operating-cycle rule (ASC 210-10-45-5). Deferred tax
  assets and liabilities are ALWAYS non-current under ASC 740-10-45-4
  regardless of expected reversal timing — a classification IAS 1 does
  not force. Leases keep ASC 842's operating/finance dual model (no
  IFRS 16 single model), so lease accounts remain a client-created COA
  concern, not a framework difference in the line recipe.

## Formance interaction

BALANCES: `get_balances` on the ledger returns per-address balances
across assets; the address balance for an account code equals debits −
credits in integer pence (the same expectation `ledger_parity` checks
against Postgres). `list_transactions` is NOT used to build the balance
sheet (balances, not flows) — but the posted/reversed scope still
applies: with parity, `get_balances` reflects exactly the posted and
reversed set.

```json
{"balances": [{"address": "1360", "balances": {"GBP/2": "-5000.00"}}]}
```

Classification (code → class) comes from the Postgres COA (`accounts`
table); ALL amounts come from Formance. Trust precondition: run
`verify_parity` first; if `ok=False`, refuse to produce the report (or
mark every figure unverified).

## Worked example

Acme Trading Ltd, first year of trading, as at 31 Dec 2025. Sales
£1,200,000, direct costs £700,000, overheads £250,000, depreciation
£50,000, loan interest £10,000, corporation tax £36,100 → profit
£153,900 to `3020`. Bank `1000` £191,000 (`1010` £0), trade debtors
£120,000, prepayments £4,000, fixed assets at cost £300,000 (`1310`
£50,000 + `1320` £250,000) with accumulated depreciation £50,000, trade
creditors £100,000, accruals £25,000, bank loan £100,000, corporation
tax payable £36,100, share capital £150,000. VAT settled (net nil).
Amounts in integer pence:

```lines
ASSETS
Bank Current Account 1000 | 19100000
Bank Deposit Account 1010 | 0
Trade Debtors 1100 | 12000000
Prepayments 1110 | 400000
Office Equipment 1310 | 5000000
Computer Equipment 1320 | 25000000
Accumulated Depreciation 1360 | -5000000
LIABILITIES
Trade Creditors 2000 | 10000000
Accruals 2010 | 2500000
Deferred Income 2020 | 0
VAT net (2210 - 2220) | 0
Bank Loan 2100 | 10000000
Corporation Tax Payable 2040 | 3610000
EQUITY
Share Capital 3000 | 15000000
Retained Earnings 3020 | 15390000
```

Totals in pence: assets 56500000 (19100000 + 12000000 + 400000 +
30000000 − 5000000); liabilities 26110000 (10000000 + 2500000 +
10000000 + 3610000); equity 30390000 (15000000 + 15390000).
Identity: 56500000 = 26110000 + 30390000, exact.

## Verification checks

- Σ ASSETS == Σ LIABILITIES + Σ EQUITY, exact in pence
- the `1360` line is NEGATIVE (contra-asset, subtracted from assets —
  never a credit to equity)
- `3020` Retained Earnings == the P&L profit for the period (first
  period, no dividends)
- bank lines (`1000`+`1010`) == the cash-flow closing bank line
- every asset line ≥ 0 except `1360`; liability and equity lines carry
  their natural signs (negative `3020` after losses is legal)

## Edge cases

- Negative equity: cumulative losses push `3020` negative — A = L + E
  still holds exactly.
- Overdrawn bank: `1000` negative — still an asset line; current
  liabilities may include the overdraft if reclassified.
- Zero-cash period: bank lines are 0; identity unaffected.
- First period: no opening comparatives; equity starts from capital
  introduced (`3000`) plus first-period profit.
- VAT-only movement: the net VAT line (`2210`−`2220`) can flip sign —
  present a negative net as the `1130` VAT Recoverable asset.
- Zero balances: list or suppress inactive/zero accounts (presentation
  choice); inactive accounts are always excluded.
- Loss-making period: `3020` falls by the loss; assets fall with it.
- Empty ledger: all lines 0; 0 = 0 + 0 holds.
- Multi-currency: out of scope (asset `GBP/2` only).
