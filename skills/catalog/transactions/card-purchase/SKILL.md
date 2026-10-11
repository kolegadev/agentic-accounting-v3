---
id: card-purchase
name: Purchase paid immediately by card or bank transfer (expense or asset)
category: transaction
frameworks: [uk-gaap, ifrs, us-gaap]
version: 0.1.0
status: v0
---

# Purchase paid immediately by card or bank transfer

Records a purchase paid at the point of sale — company debit card,
contactless payment, or an immediate bank transfer. Cash (debit) basis: the
expense or asset is recognised AND settled in the same entry, so no trade
creditor is ever created. Two VAT variants: standard/reduced-rated (input
VAT split out to `2220`) and no-VAT (zero-rated, exempt, or no separate VAT
rate — full net leaves the bank).

## When to use

- "I paid £X for Y with the company card", "bought Z on the debit card",
  "paid immediately from the Revolut/bank account"
- A purchase that leaves the bank account at the same moment it is
  recognised: card payments, contactless, online checkout, instant transfer
- Both expense purchases (office supplies, materials, travel) and asset
  purchases (computers, equipment, vehicles)
- Do NOT use for: purchases on credit (use `supplier-invoice`), paying an
  existing supplier balance (use `supplier-payment`), moving money between
  own accounts (use `bank-transfer`), sales or customer money in.

## Facts

| field | type | required | notes |
|-------|------|----------|-------|
| purchase_date | date | yes | posting date; the payment left the bank that day |
| bank_account_code | string | yes | the bank/debit account the money left, e.g. `1000` |
| account_code | string | yes | the expense or asset account hit, e.g. `6050` Office Supplies, `1320` Computer Equipment |
| net_pence | integer | yes | net amount in integer pence, > 0 |
| vat_rate | enum | no | `20%` \| `5%` \| `0%` \| `exempt`, or absent for a no-VAT purchase (engine enum `VALID_VAT_RATES`) |
| description | string | no | what was purchased (informational) |
| supplier_name | string | no | counterparty trade name, informational — the orchestrator resolves/creates the supplier contact |
| gross_pence | integer | derived | net + VAT — the amount that leaves the bank |

Money is ALWAYS integer pence. VAT = round_half_up(net × rate); for 20% this
is exactly net ÷ 5 in pence, for 5% exactly net ÷ 20.

## DR/CR construction

Standard / reduced-rated (20% / 5%) variant:

| leg | account class | amount | notes |
|-----|---------------|--------|-------|
| Dr | Expense (Expense) | net | the expense account hit, e.g. `6050` Office Supplies, `6000` Rent & Rates |
| Dr | Expense (DirectCost) | net | direct-cost purchases, e.g. `5010` Materials & Consumables, `5020` Goods for Resale |
| Dr | Asset (FixedAsset) | net | capitalised purchases, e.g. `1320` Computer Equipment — same shape, debit class Asset |
| Dr | Liability (CurrentLiability) | vat | `2220` Input VAT — contra-liability carrying a debit balance (reclaim from HMRC); standard/reduced-rated purchases only |
| Cr | Asset (Bank) | gross | the card/bank account the money left, e.g. `1000` Bank Current Account |

Zero-rated / exempt / no-VAT variant:

| leg | account class | amount | notes |
|-----|---------------|--------|-------|
| Dr | Expense (Expense) | net | e.g. `6030` Insurance (exempt), `6020` Water Rates (0%) |
| Cr | Asset (Bank) | net | the full net leaves the bank — no `2220` leg |

There is never a creditor leg: the payment is immediate, so the entry is
Dr expense/asset (+ Dr `2220` if rated) / Cr bank, and it always balances.
A flat-rate-scheme entity books the gross to the expense/asset account
(no input VAT reclaim) — one Dr leg, one Cr leg.

## VAT treatment

- Standard scheme: input VAT is recoverable in the period of the payment —
  it is the same day as the purchase. Dr `2220` Input VAT at purchase date.
- Reduced rate (`5%`): same shape, `2220` carries net ÷ 20 in pence.
- `0%` / `exempt` / no `vat_rate`: no input VAT leg at all — the full net
  is the cost and only the expense/asset leg and the bank credit exist.
- Cash scheme: identical ledger entry (the payment IS the cash event);
  only the VAT-return timing differs from a scheme where the invoice date
  governs, and the engine computes that from posting dates.
- Flat-rate scheme: no input VAT reclaim (box 4 = 0). Post the whole gross
  to the expense/asset account — no `2220` leg, and no `2000` leg either.
- Rates: `20%`, `5%`, `0%`, `exempt`. Zero-rated and exempt purchases both
  produce NO input VAT leg; there is nothing to reclaim.

## Formance interaction

Balanced 3-leg entry [Dr 1320 250000, Dr 2220 50000, Cr 1000 300000] is
mirrored by `build_formance_postings` (greedy pairing: the credit leg is
the source, the debit legs are the destinations; integer pence become exact
decimal strings):

```json
[
  {"source": "1000", "destination": "1320", "amount": "2500.00", "asset": "GBP/2"},
  {"source": "1000", "destination": "2220", "amount": "500.00", "asset": "GBP/2"}
]
```

Reduced-rate expense [Dr 5000 20000, Dr 2220 1000, Cr 1000 21000]:

```json
[
  {"source": "1000", "destination": "5000", "amount": "200.00", "asset": "GBP/2"},
  {"source": "1000", "destination": "2220", "amount": "10.00", "asset": "GBP/2"}
]
```

No-VAT variant [Dr 6030 25000, Cr 1000 25000]:

```json
[
  {"source": "1000", "destination": "6030", "amount": "250.00", "asset": "GBP/2"}
]
```

Carry `reference` (JE ref) and `metadata.transaction_id` unchanged (F1 §6).

## Worked example

A laptop bought for £2,500.00 net plus 20% VAT on the company debit card
(capitalised as computer equipment). VAT = 250000 ÷ 5 = 50000 pence;
gross = 300000 pence. Journal in pence:

```dr-cr
Dr  1320 Computer Equipment  250000
Dr  2220 Input VAT            50000
Cr  1000 Bank Current Account 300000
```

Debits 300000 == credits 300000. Formance postings produced:
`{source: 1000 → destination: 1320, "2500.00"}` and
`{source: 1000 → destination: 2220, "500.00"}`, both `asset: GBP/2`.

Reduced-rate expense — £200.00 of materials (5% VAT) paid by card:
VAT = 20000 ÷ 20 = 1000 pence; gross = 21000 pence.

```dr-cr
Dr  5000 Cost of Sales  20000
Dr  2220 Input VAT       1000
Cr  1000 Bank Current Account 21000
```

Debits 21000 == credits 21000. Formance postings:
`{source: 1000 → destination: 5000, "200.00"}` and
`{source: 1000 → destination: 2220, "10.00"}`.

No-VAT variant — £250.00 exempt insurance paid by immediate transfer:

```dr-cr
Dr  6030 Insurance            25000
Cr  1000 Bank Current Account 25000
```

Debits 25000 == credits 25000. One Formance posting:
`{source: 1000 → destination: 6030, "250.00"}`.

## Verification checks

- Sum of Dr legs == sum of Cr legs in integer pence (engine rejects
  otherwise with 422 UnbalancedTransactionError)
- gross == net + VAT — exact pence, no float arithmetic
- the `2220` Input VAT Dr leg exists only for `20%` / `5%`; `0%`, `exempt`
  and absent rates contribute none
- every account code exists in the COA and is active; `account_code` must
  be an Expense (Expense / DirectCost) or Asset (FixedAsset) account and
  `bank_account_code` an Asset (Bank) account
- no creditor (`2000`) leg — the purchase is paid at the point of sale

## Edge cases

- Rounding: VAT half-up; at 20% VAT = net ÷ 5 is exact only when net is a
  multiple of 5 pence — otherwise round half-up
- Capitalised purchase (asset): debit `Asset (FixedAsset)` (1300–1350)
  instead of Expense; the VAT split is unchanged
- A personal card used for a business purchase: the engine has no
  card-liability account in v0 — record the purchase against the bank
  account when the company reimburses (out of scope)
- Mixed VAT on one receipt: this skill takes ONE `account_code` and ONE
  rate — split into one card-purchase per rate group rather than guessing
- Foreign currency: out of scope for v0 (asset `GBP/2` only)
- Refund of a card purchase: exact reverse of this entry

## Framework variants

- uk-gaap (FRS 102 / FRS 105): asset purchases are capitalised at cost NET
  of recoverable input VAT (FRS 102 §17); expense purchases go to the P&L
  net of recoverable input VAT. Irrecoverable VAT (exempt/flat-rate) is
  part of the cost. With an immediate card payment there is no payable and
  no discounting question.
- IFRS (IAS 16 / IAS 2): identical day-one entry — cost excludes
  recoverable input VAT; the debit leg is Property, plant and equipment
  versus inventory/expense depending on the entity's policy.
- us-gaap: there is no VAT input/output split and no `2220` recovery
  account. Sales tax charged at the point of sale is generally part of the
  asset's or inventory's cost (ASC 330-10-30 / ASC 360-10-30) unless a
  resale or direct-use exemption applies; where the seller collects none,
  the buyer accrues use tax (ASC 450-20) and debits the asset or expense
  instead of recovering a VAT credit.
