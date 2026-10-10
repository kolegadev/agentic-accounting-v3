---
id: supplier-invoice
name: Purchase on credit (with & without VAT variants)
category: transaction
frameworks: [uk-gaap, ifrs]
version: 0.1.0
status: v0
---

# Purchase on credit (supplier invoice)

Records a supplier invoice for goods or services bought on credit: the
expense (or asset) is recognised now, payment happens later. Two VAT
variants: standard-rated (VAT split out to input VAT) and no-VAT
(zero-rated, exempt, or out-of-scope — full net to the expense account).

## When to use

- "Record this supplier invoice", "enter a purchase invoice", "post a bill"
- A supplier has invoiced for goods or services that have not been paid yet
- Utility bills, rent, professional fees, materials, stock purchases on credit
- Do NOT use for: cash purchases paid immediately (post from the bank feed
  instead), fixed-asset capitalisation (same DR/CR shape but the debit class
  is Asset, not Expense), supplier refunds (exact reverse of this skill).

## Facts

| field | type | required | notes |
|-------|------|----------|-------|
| supplier_name | string | yes | trade name, informational |
| invoice_ref | string | yes | supplier's invoice number — part of the idempotency key |
| invoice_date | date | yes | posting date under the standard VAT scheme |
| due_date | date | no | payment terms, informational |
| lines[].description | string | yes | what was purchased |
| lines[].net_pence | integer | yes | line net amount in integer pence |
| lines[].vat_rate | enum | yes | `20%` \| `5%` \| `0%` \| `exempt` (engine enum `VALID_VAT_RATES`) |
| lines[].expense_account_code | string | yes | e.g. `5010` Materials & Consumables, `6000` Rent & Rates |
| gross_pence | integer | derived | sum of (net + VAT) per line |

Money is ALWAYS integer pence. VAT per line = round_half_up(net × rate);
for 20% this is exactly net ÷ 5 in pence.

## DR/CR construction

Standard-rated (20% / 5%) variant:

| leg | account class | amount | notes |
|-----|---------------|--------|-------|
| Dr | Expense (Expense) | net | one row per line per expense account, category Expense — type Expense (e.g. `6000` Rent & Rates, `6010` Utilities) or type DirectCost (e.g. `5010` Materials & Consumables, `5020` Goods for Resale); use `Asset (FixedAsset)` class if the purchase is capitalised (codes `1300`–`1350`) |
| Dr | Liability (CurrentLiability) | vat | `2220` Input VAT — contra-liability carrying a debit balance (reclaim from HMRC). Standard-rated lines only |
| Cr | Liability (CurrentLiability) | gross | `2000` Trade Creditors — the supplier is owed the full gross |

Zero-rated / exempt / out-of-scope variant (no VAT):

| leg | account class | amount | notes |
|-----|---------------|--------|-------|
| Dr | Expense (Expense) | net | e.g. `6030` Insurance (exempt), `6020` Water Rates (0%) |
| Cr | Liability (CurrentLiability) | net | `2000` Trade Creditors |

Flat-rate scheme variant (no input VAT reclaim):

| leg | account class | amount | notes |
|-----|---------------|--------|-------|
| Dr | Expense (Expense) | gross | input VAT is not recoverable — it becomes part of the cost |
| Cr | Liability (CurrentLiability) | gross | `2000` Trade Creditors |

Mixed rates on one invoice: post one Dr per expense account and one Dr to
`2220` Input VAT per rate group, so each VATLine records its own rate.

## VAT treatment

- Standard scheme: input VAT is claimable in the period the invoice is
  dated. Dr `2220` Input VAT at invoice date.
- Cash scheme: input VAT is claimable only when the supplier is PAID. The
  day-one ledger entry is identical (Dr `2220` at invoice date); the VAT
  return's box 4 is computed by VatService from payment dates, not posting
  dates. No separate suspense account in this engine.
- Flat-rate scheme: no input VAT reclaim (box 4 = 0). Post the whole gross
  to the expense account — no `2220` leg at all.
- Rates: `20%`, `5%`, `0%`, `exempt`. Zero-rated and exempt lines both
  produce NO input VAT leg; only the expense leg and the credit leg exist.

## Formance interaction

Balanced 3-leg entry [Dr 5010 50000, Dr 2220 10000, Cr 2000 60000] is
mirrored by `build_formance_postings` (greedy pairing: credit leg is the
source, debit leg is the destination, integer pence become exact decimal
strings):

```json
[
  {"source": "2000", "destination": "5010", "amount": "500.00", "asset": "GBP/2"},
  {"source": "2000", "destination": "2220", "amount": "100.00", "asset": "GBP/2"}
]
```

No-VAT variant [Dr 6030 25000, Cr 2000 25000]:

```json
[
  {"source": "2000", "destination": "6030", "amount": "250.00", "asset": "GBP/2"}
]
```

Carry `reference` (JE ref) and `metadata.transaction_id` unchanged (F1 §6).

## Worked example

Supplier invoices £600.00 gross including 20% VAT for materials, on credit.
Net = 60000 ÷ 1.20 = 50000 pence; VAT = 10000 pence. Journal in pence:

```dr-cr
Dr  5010 Materials & Consumables  50000
Dr  2220 Input VAT                10000
Cr  2000 Trade Creditors          60000
```

Debits 60000 == credits 60000. Formance postings produced:
`{source: 2000 → destination: 5010, "500.00"}` and
`{source: 2000 → destination: 2220, "100.00"}`, both `asset: GBP/2`.

No-VAT variant: £250.00 exempt insurance invoice on credit:

```dr-cr
Dr  6030 Insurance     25000
Cr  2000 Trade Creditors  25000
```

Debits 25000 == credits 25000. One Formance posting:
`{source: 2000 → destination: 6030, "250.00"}`.

## Verification checks

- Sum of Dr legs == sum of Cr legs in integer pence (engine rejects
  otherwise with 422 UnbalancedTransactionError)
- gross == net + vat for every line — exact pence, no float arithmetic
- exactly one `2220` Input VAT Dr leg per rate group; zero/exempt lines
  contribute none
- every account code exists in the COA template and is active
- the supplier-facing credit leg class is Liability; the debit leg class is
  Expense, or Asset (FixedAsset) for capitalised purchases

## Edge cases

- Rounding: VAT per line, half-up; at 20% VAT = net ÷ 5 is exact only when
  net is a multiple of 5 pence — otherwise round half-up
- Mixed rates on one invoice: split the `2220` leg per rate group
- Credit note: exact reverse — Dr 2000 / Cr expense + Cr 2220
- Fixed-asset purchase: debit `Asset (FixedAsset)` (1300–1350) instead of
  Expense; the VAT split is unchanged
- Cash scheme: ledger entry identical; only the VAT-return timing differs
- Flat-rate: gross to expense, no input VAT leg
- Foreign-currency invoice: out of scope for v0 (asset `GBP/2` only)

## Framework variants

- uk-gaap (FRS 102 §11 / FRS 105): day-to-day posting identical. Trade
  payables are financial liabilities measured at amortised cost; short-term
  trade credit is held at its undiscounted invoice amount in practice
  (FRS 102 §11.14).
- IFRS (IFRS 9): identical day-one entry; short-term payables are not
  discounted (IFRS 9 §5.1.3). Inventory purchases follow IAS 2 versus
  FRS 102 §13 — the debit leg class (Expense vs Asset) must match the
  entity's inventory policy.
- us-gaap: TODO — US sales/use tax on purchases may be non-recoverable and
  capitalised into cost; there is no input/output VAT split.
