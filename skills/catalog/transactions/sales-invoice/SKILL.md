---
id: sales-invoice
name: Sale on credit (with & without VAT variants)
category: transaction
frameworks: [uk-gaap, ifrs, us-gaap]
version: 0.1.0
status: v0
---

# Sale on credit (sales invoice)

Records a sale of goods or services on credit: revenue is recognised now,
the customer pays later. Two VAT variants: standard-rated (VAT split out to
output VAT) and no-VAT (zero-rated, exempt, or out-of-scope — full net to
revenue).

## When to use

- "Record this sales invoice", "raise an invoice", "bill the customer"
- Goods or services have been delivered/performed and an invoice is issued
- Do NOT use for: cash sales (bank feed directly), customer receipts
  (customer-receipt skill settles this invoice), credit notes (exact
  reverse), deferred income (see prepayment skill for the deferred-income
  variant).

## Facts

| field | type | required | notes |
|-------|------|----------|-------|
| customer_name | string | yes | trade name, informational |
| invoice_ref | string | yes | entity's invoice number — part of the idempotency key |
| invoice_date | date | yes | posting date (tax point under the standard scheme) |
| due_date | date | no | payment terms, informational |
| lines[].description | string | yes | what was sold |
| lines[].net_pence | integer | yes | line net amount in integer pence |
| lines[].vat_rate | enum | yes | `20%` \| `5%` \| `0%` \| `exempt` (engine enum `VALID_VAT_RATES`) |
| lines[].revenue_account_code | string | yes | e.g. `4000` Sales - Standard Rated, `4020` Sales - Zero Rated, `4030` Sales - Exempt |
| gross_pence | integer | derived | sum of (net + VAT) per line |

Money is ALWAYS integer pence. VAT per line = round_half_up(net × rate);
for 20% this is exactly net ÷ 5 in pence.

## DR/CR construction

Standard-rated (20% / 5%) variant:

| leg | account class | amount | notes |
|-----|---------------|--------|-------|
| Dr | Asset (CurrentAsset) | gross | `1100` Trade Debtors — the customer owes the full gross |
| Cr | Revenue (Revenue) | net | `4000` Sales - Standard Rated (20%), `4010` Sales - Reduced Rated (5%) |
| Cr | Liability (CurrentLiability) | vat | `2210` Output VAT — owed to HMRC |

Zero-rated / exempt / out-of-scope variant (no VAT):

| leg | account class | amount | notes |
|-----|---------------|--------|-------|
| Dr | Asset (CurrentAsset) | net | `1100` Trade Debtors |
| Cr | Revenue (Revenue) | net | `4020` Sales - Zero Rated, `4030` Sales - Exempt, `4040` Sales - Outside Scope |

Flat-rate scheme variant:

| leg | account class | amount | notes |
|-----|---------------|--------|-------|
| Dr | Asset (CurrentAsset) | gross | `1100` Trade Debtors |
| Cr | Revenue (Revenue) | gross − vat | revenue keeps the difference between 20% and the flat rate |
| Cr | Liability (CurrentLiability) | vat | `2210` Output VAT = gross × flat_rate% (sector-dependent, e.g. 16.5% limited-cost) |

Mixed rates on one invoice: post one Cr per revenue account and one Cr to
`2210` Output VAT per rate group, so each VATLine records its own rate.

## VAT treatment

- Standard scheme: output VAT is due in the period the invoice is dated.
  Cr `2210` Output VAT at invoice date.
- Cash scheme: output VAT is due only when the customer PAYS. The day-one
  ledger entry is identical (Cr `2210` at invoice date); VatService
  computes box 1 from payment dates, not posting dates.
- Flat-rate scheme: output VAT = gross × flat-rate percentage applied to
  the VAT-inclusive turnover; revenue is the residual (box 1 uses the flat
  rate, not the 20% fraction).
- Rates: `20%`, `5%`, `0%`, `exempt`. Zero-rated and exempt sales produce
  NO output VAT leg.

## Formance interaction

Balanced 3-leg entry [Dr 1100 60000, Cr 4000 50000, Cr 2210 10000] is
mirrored by `build_formance_postings` (greedy pairing: credit leg is the
source, debit leg is the destination):

```json
[
  {"source": "4000", "destination": "1100", "amount": "500.00", "asset": "GBP/2"},
  {"source": "2210", "destination": "1100", "amount": "100.00", "asset": "GBP/2"}
]
```

No-VAT variant [Dr 1100 25000, Cr 4030 25000]:

```json
[
  {"source": "4030", "destination": "1100", "amount": "250.00", "asset": "GBP/2"}
]
```

Carry `reference` (JE ref) and `metadata.transaction_id` unchanged (F1 §6).

## Worked example

Invoice a customer £600.00 gross including 20% VAT for standard-rated
services. Net = 60000 ÷ 1.20 = 50000 pence; VAT = 10000 pence:

```dr-cr
Dr  1100 Trade Debtors       60000
Cr  4000 Sales - Standard Rated  50000
Cr  2210 Output VAT          10000
```

Debits 60000 == credits 60000. Formance postings produced:
`{source: 4000 → destination: 1100, "500.00"}` and
`{source: 2210 → destination: 1100, "100.00"}`, both `asset: GBP/2`.

No-VAT variant: £250.00 exempt sale on credit:

```dr-cr
Dr  1100 Trade Debtors   25000
Cr  4030 Sales - Exempt   25000
```

Debits 25000 == credits 25000. One Formance posting:
`{source: 4030 → destination: 1100, "250.00"}`.

## Verification checks

- Sum of Dr legs == sum of Cr legs in integer pence (engine rejects
  otherwise with 422 UnbalancedTransactionError)
- gross == net + vat for every line — exact pence, no float arithmetic
- exactly one `2210` Output VAT Cr leg per rate group; zero/exempt lines
  contribute none
- every account code exists in the COA template and is active
- the customer-facing debit leg class is Asset; revenue legs are Revenue;
  VAT legs are Liability

## Edge cases

- Rounding: VAT per line, half-up; at 20% VAT = net ÷ 5 is exact only when
  net is a multiple of 5 pence — otherwise round half-up
- Mixed rates on one invoice: split the `2210` leg per rate group
- Credit note: exact reverse — Dr revenue + Dr `2210` / Cr `1100`
- Bad debt: write-off is Dr a bad-debt expense account (e.g. `6700` Sundry
  Expenses or a dedicated bad-debt code) / Cr `1100`, and (standard scheme)
  reclaim the output VAT in the period the debt is written off
- Early-settlement discount: agreed discount may reduce the VAT due —
  recalculate the `2210` leg when the discount is taken
- Cash scheme: ledger identical; only the VAT-return timing differs
- Foreign-currency invoice: out of scope for v0 (asset `GBP/2` only)

## Framework variants

- uk-gaap (FRS 102 §23): revenue when the significant risks and rewards of
  ownership transfer / services are performed; single-element day-to-day
  sales post identically to IFRS.
- IFRS (IFRS 15): five-step model — identify the contract and performance
  obligations, recognise when control transfers. Point-in-time goods sales
  coincide with FRS 102; multi-element contracts or distinct service
  obligations may defer revenue where FRS 102 would not — for v0 the
  day-to-day posting is identical, but flag multi-element invoices.
- us-gaap (ASC 606): five-step model — (1) identify the contract with a
  customer, (2) identify the performance obligations, (3) determine the
  transaction price, (4) allocate the price to the obligations, and
  (5) recognise revenue when (or as) each obligation is satisfied.
  Point-in-time goods sales post identically to IFRS 15, but US GAAP sets
  the "probable" collectibility bar higher (~75–80%) and offers a policy
  election to exclude sales taxes from the transaction price. Sales tax
  charged to the customer is collected on behalf of the state (ASC
  606-10-32-2A): Cr sales-tax payable, never revenue — the `2210` output
  VAT account is a UK construct. The receivable is an ASC 310-10
  financial asset at the invoice amount.
