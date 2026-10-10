---
id: prepayment
name: Prepaid expense & deferred income
category: transaction
frameworks: [uk-gaap, ifrs]
version: 0.1.0
status: v0
---

# Prepayments (prepaid expenses) and deferred income

Two mirror-image timing corrections:

- Prepaid expense: paid now for a future period — Dr Prepayments (asset),
  Cr Bank at payment; release Dr Expense / Cr Prepayments as the period is
  used up.
- Deferred income: received now for a future period — Dr Bank, Cr Deferred
  Income (liability) at receipt; release Dr Deferred Income / Cr Revenue as
  the service is delivered.

## When to use

- "Prepay annual insurance", "spread this bill over 12 months",
  "defer this subscription income"
- An expense is paid in advance of the period it covers (insurance, rent in
  advance, annual licences)
- A customer pays in advance of delivery (subscriptions, retainers,
  deposits for future work)
- Do NOT use for: invoices already in the correct period, accruals (see
  journal-accrual skill), VAT on the underlying supply (follows the
  supplier-invoice / sales-invoice skills).

## Facts

| field | type | required | notes |
|-------|------|----------|-------|
| direction | enum | yes | `prepaid_expense` \| `deferred_income` |
| account_code | string | yes | expense (e.g. `6030` Insurance) or revenue (e.g. `4000` Sales) account to release to |
| total_pence | integer | yes | total paid/received (the gross bank movement), integer pence — when a VAT invoice accompanies the payment the prepayment/deferred-income balance holds NET of VAT and the VAT element posts immediately to `2220` Input VAT / `2210` Output VAT (see VAT treatment) |
| start_date | date | yes | first day the prepayment covers |
| end_date | date | yes | last day the prepayment covers |
| release_schedule | enum | yes | `monthly` \| `per_period` — how the release is split |
| bank_account_code | string | yes | `1000` Bank Current Account |
| vat_rate | enum | no | `20%` \| `5%` \| `0%` \| `exempt` — only when a VAT invoice is issued with the payment |

## DR/CR construction

Prepaid expense — initial payment:

| leg | account class | amount | notes |
|-----|---------------|--------|-------|
| Dr | Asset (CurrentAsset) | total_pence | `1110` Prepayments — future economic benefit |
| Cr | Asset (Bank) | total_pence | `1000` Bank Current Account |

Prepaid expense — monthly release:

| leg | account class | amount | notes |
|-----|---------------|--------|-------|
| Dr | Expense (Expense) | month_share | e.g. `6030` Insurance — cost lands in the period it covers |
| Cr | Asset (CurrentAsset) | month_share | `1110` Prepayments — reduce the asset |

Deferred income — initial receipt:

| leg | account class | amount | notes |
|-----|---------------|--------|-------|
| Dr | Asset (Bank) | total_pence | `1000` Bank Current Account |
| Cr | Liability (CurrentLiability) | total_pence | `2020` Deferred Income — obligation to deliver |

Deferred income — monthly release:

| leg | account class | amount | notes |
|-----|---------------|--------|-------|
| Dr | Liability (CurrentLiability) | month_share | `2020` Deferred Income — reduce the obligation |
| Cr | Revenue (Revenue) | month_share | e.g. `4030` Sales - Exempt / `4020` Sales - Zero Rated (non-rated supply); for a standard-rated supply with a VAT invoice at receipt the release still credits `4000` Sales - Standard Rated NET — the output VAT was recognised in full at receipt |

## VAT treatment

- Prepaid expense: if the supplier's VAT invoice accompanies the payment,
  the input VAT is recoverable immediately (standard scheme) even though
  the expense is deferred: Dr Prepayments (net) + Dr `2220` Input VAT /
  Cr Bank (gross). The monthly release is then net only.
- Exempt or zero-rated prepayments (insurance, water): no VAT leg at all.
- Deferred income: if a VAT invoice is issued at receipt, output VAT is due
  at invoice date (standard scheme): Dr Bank (gross) / Cr Deferred Income
  (net) + Cr `2210` Output VAT. The monthly release is net only.
- Cash scheme: the tax point for both sides moves to the payment/receipt
  date; ledger postings are unchanged, VatService handles box timing.

## Formance interaction

Initial prepayment [Dr 1110 120000, Cr 1000 120000]:

```json
[
  {"source": "1000", "destination": "1110", "amount": "1200.00", "asset": "GBP/2"}
]
```

Monthly release [Dr 6030 10000, Cr 1110 10000]:

```json
[
  {"source": "1110", "destination": "6030", "amount": "100.00", "asset": "GBP/2"}
]
```

Deferred income initial [Dr 1000 120000, Cr 2020 120000]:

```json
[
  {"source": "2020", "destination": "1000", "amount": "1200.00", "asset": "GBP/2"}
]
```

Deferred income release [Dr 2020 10000, Cr 4030 10000]:

```json
[
  {"source": "4030", "destination": "2020", "amount": "100.00", "asset": "GBP/2"}
]
```

Carry `reference` (JE ref) and `metadata.transaction_id` unchanged (F1 §6).

## Worked example

Annual insurance premium £1,200.00 (exempt) paid 1 January, released
£100.00 per month.

Initial payment:

```dr-cr
Dr  1110 Prepayments          120000
Cr  1000 Bank Current Account  120000
```

Debits 120000 == credits 120000. One Formance posting:
`{source: 1000 → destination: 1110, "1200.00", asset: GBP/2}`.

January release:

```dr-cr
Dr  6030 Insurance   10000
Cr  1110 Prepayments  10000
```

Debits 10000 == credits 10000. One Formance posting:
`{source: 1110 → destination: 6030, "100.00", asset: GBP/2}`.

Deferred income variant: £1,200.00 annual subscription received 1 January
(non-rated supply, no VAT invoice), released £100.00 per month.

```dr-cr
Dr  1000 Bank Current Account  120000
Cr  2020 Deferred Income        120000
```

Debits 120000 == credits 120000. One Formance posting:
`{source: 2020 → destination: 1000, "1200.00", asset: GBP/2}`.

```dr-cr
Dr  2020 Deferred Income   10000
Cr  4030 Sales - Exempt     10000
```

Debits 10000 == credits 10000. One Formance posting:
`{source: 4030 → destination: 2020, "100.00", asset: GBP/2}`.

## Verification checks

- Sum of Dr legs == sum of Cr legs in integer pence, at every stage
- initial entry and every release each balance independently
- releases sum to exactly total_pence; the final release absorbs rounding
  (never release more than the balance remaining)
- prepayment legs are Asset classes only; deferred income legs are
  Liability/Revenue classes only
- no VAT leg unless the invoice-accompanying variant is used, in which case
  the VAT leg must equal net × rate exactly

## Edge cases

- Rounding: split total by whole months; the last month absorbs the
  remainder (e.g. 120000 over 7 months → six at 17143 and one at 17142)
- Part-month coverage: prorate by days for mid-month starts
- Cancellation/refund: reverse the unreleased balance — Dr Bank / Cr
  Prepayments (or Dr Deferred Income / Cr Bank)
- VAT accompanying payment: prepayment asset holds NET only; VAT goes to
  `2220`/`2210` immediately (standard scheme)
- Renewals: a new prepayment starts when the old one is fully released —
  never double-count an overlap month

## Framework variants

- uk-gaap (FRS 102 §23 / FRS 105): matching of expense/income to the
  period it relates to; "deferred income" terminology.
- IFRS (IFRS 15): customer prepayments are contract liabilities (revenue
  recognised as performance obligations are satisfied); expense prepayments
  follow the matching principle — day-to-day postings are identical, but
  IFRS 15 requires assessing distinct performance obligations for the
  deferred-income release pattern.
- us-gaap: TODO — ASC 340-10 governs prepaid expenses; ASC 606 contract
  liabilities mirror IFRS 15; the DR/CR shape is the same but the release
  pattern follows US GAAP's performance-obligation analysis.
