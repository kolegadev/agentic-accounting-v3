---
id: customer-receipt
name: Receive an AR balance into bank
category: transaction
frameworks: [uk-gaap, ifrs]
version: 0.1.0
status: v0
---

# Receive payment from a customer (settle Accounts Receivable)

Records money received into a bank account against an outstanding customer
balance. This settles the Trade Debtors balance raised by the sales-invoice
skill; it recognises no revenue — revenue was recognised when the invoice
was posted.

## When to use

- "Customer paid", "mark invoice as paid", "allocate this receipt"
- A bank receipt matches a posted sales invoice (fully or partially)
- Bank-feed reconciliation of an incoming payment to an open AR balance
- Do NOT use for: sales with no prior invoice (post directly from the bank
  feed), customer deposits received in advance (see prepayment skill,
  deferred-income variant), refunds to customers (exact reverse).

## Facts

| field | type | required | notes |
|-------|------|----------|-------|
| customer_name | string | yes | trade name, informational |
| receipt_date | date | yes | posting date (VAT point under the cash scheme) |
| bank_account_code | string | yes | receiving account, e.g. `1000` Bank Current Account |
| amount_pence | integer | yes | cash entering the bank, integer pence |
| invoice_refs | list | no | invoices being settled, for allocation |
| allocated_pence | list | no | per-invoice amounts when paying several invoices at once |

## DR/CR construction

| leg | account class | amount | notes |
|-----|---------------|--------|-------|
| Dr | Asset (Bank) | amount_pence | `1000` Bank Current Account — cash arrives |
| Cr | Asset (CurrentAsset) | amount_pence | `1100` Trade Debtors — reduces what customers owe |

One Dr / Cr pair per receipt, even when several invoices are settled; use
the `allocated_pence` facts only to update the sub-ledger allocation.

## VAT treatment

- Standard scheme: no VAT effect — VAT was recognised on the invoice.
- Cash scheme: receipt is the tax point; the output VAT already posted to
  `2210` at invoice date becomes due now. The ledger entry is unchanged;
  VatService computes box 1 from payment dates.
- Flat-rate scheme: VAT becomes due on receipt; ledger entry unchanged.

## Formance interaction

Balanced 2-leg entry [Dr 1000 60000, Cr 1100 60000] produces exactly one
posting — source is the credit (AR) account, destination the debit (bank):

```json
[
  {"source": "1100", "destination": "1000", "amount": "600.00", "asset": "GBP/2"}
]
```

Carry `reference` (JE ref) and `metadata.transaction_id` unchanged (F1 §6).

## Worked example

Customer pays the £600.00 invoice into the current account:

```dr-cr
Dr  1000 Bank Current Account  60000
Cr  1100 Trade Debtors          60000
```

Debits 60000 == credits 60000. One Formance posting:
`{source: 1100 → destination: 1000, "600.00", asset: GBP/2}`.

## Verification checks

- Sum of Dr legs == sum of Cr legs in integer pence
- exactly two legs: Dr on Asset (Bank) class, Cr on Asset (CurrentAsset)
  class — the receipt must never credit a Revenue account
- the AR account is the same code the original invoice debited (`1100`)
- receipt amount never exceeds the open AR balance by more than the stated
  credit-balance tolerance (see Edge cases)

## Edge cases

- Partial payment: Dr bank for the partial amount, Cr `1100` for the same —
  the invoice stays partially open in the sub-ledger
- Overpayment: the excess leaves a credit balance on `1100` to offset
  against future invoices, or is refunded
- Early-settlement discount: Dr bank for the discounted amount, Dr a
  discount-allowed expense (e.g. `6700` Sundry Expenses), Cr `1100` full
- Bounced / returned receipt: exact reverse (Dr `1100` / Cr bank)
- Unallocated receipt (no invoice): Dr bank / Cr a customer-deposit
  liability, then apply against the invoice when it arrives
- Foreign-currency receipt: out of scope for v0 (asset `GBP/2` only)

## Framework variants

- uk-gaap (FRS 102 §11 / FRS 105): settlement of a financial asset —
  derecognition when the contractual rights expire or transfer; posting
  identical.
- IFRS (IFRS 9 §3.2): same derecognition outcome for a settled trade
  receivable; no gain/loss arises on a GBP-to-GBP settlement.
- us-gaap: TODO — posting mechanics identical (ASC 310), but note the US
  allowance-for-credit-losses model (CECL) affects the receivable's
  carrying value, not the receipt entry itself.
