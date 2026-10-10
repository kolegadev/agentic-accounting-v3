---
id: supplier-payment
name: Pay an AP balance from bank
category: transaction
frameworks: [uk-gaap, ifrs]
version: 0.1.0
status: v0
---

# Pay a supplier (settle Accounts Payable)

Pays an outstanding supplier balance from a bank account. This settles the
Trade Creditors balance raised by the supplier-invoice skill; it recognises
no expense — the expense was recognised when the invoice was posted.

## When to use

- "Pay this supplier", "mark invoice as paid", "supplier payment run"
- A bank payment matches a posted supplier invoice (fully or partially)
- Bank-feed reconciliation of an outgoing payment to an open AP balance
- Do NOT use for: payments with no prior invoice (see journal-accrual or a
  direct Dr Expense / Cr Bank entry), paying HMRC for VAT or PAYE (those
  settle the VAT Control / PAYE liabilities instead).

## Facts

| field | type | required | notes |
|-------|------|----------|-------|
| supplier_name | string | yes | trade name, informational |
| payment_date | date | yes | posting date (VAT point under the cash scheme) |
| bank_account_code | string | yes | paying account, e.g. `1000` Bank Current Account |
| amount_pence | integer | yes | cash leaving the bank, integer pence |
| invoice_refs | list | no | invoices being settled, for allocation |
| allocated_pence | list | no | per-invoice amounts when paying several invoices at once |

## DR/CR construction

| leg | account class | amount | notes |
|-----|---------------|--------|-------|
| Dr | Liability (CurrentLiability) | amount_pence | `2000` Trade Creditors — reduces what the entity owes |
| Cr | Asset (Bank) | amount_pence | `1000` Bank Current Account — cash leaves the bank |

One Dr / Cr pair per payment, even when several invoices are settled; use
the `allocated_pence` facts only to update the sub-ledger allocation.

## VAT treatment

- Standard scheme: no VAT effect — VAT was recognised on the invoice.
- Cash scheme: payment is the tax point; the input VAT already posted to
  `2220` at invoice date becomes claimable now. The ledger entry is
  unchanged; VatService computes box 4 from payment dates.
- Flat-rate scheme: no VAT effect on payment.
- Early-settlement (prompt-payment) discount: when a discount is taken,
  the input VAT claimed on the invoice is reduced in line with the
  discount. The payment entry carries a Cr `2220` Input VAT adjustment for
  the VAT element of the discount and the discount income credited to
  `4050` is net of VAT — the same prompt-payment-discount rule the
  sales-invoice skill applies to output VAT (Cr `2210` recalculated when
  the discount is taken).
- Paying HMRC (VAT return, PAYE/NIC) is NOT this skill: those settle
  liability accounts such as `2200` VAT Control and `2030` PAYE / NI Payable.

## Formance interaction

Balanced 2-leg entry [Dr 2000 60000, Cr 1000 60000] produces exactly one
posting — source is the credit (bank) account, destination the debit (AP):

```json
[
  {"source": "1000", "destination": "2000", "amount": "600.00", "asset": "GBP/2"}
]
```

Carry `reference` (JE ref) and `metadata.transaction_id` unchanged (F1 §6).

## Worked example

Pay the £600.00 supplier invoice from the current account:

```dr-cr
Dr  2000 Trade Creditors     60000
Cr  1000 Bank Current Account  60000
```

Debits 60000 == credits 60000. One Formance posting:
`{source: 1000 → destination: 2000, "600.00", asset: GBP/2}`.

## Verification checks

- Sum of Dr legs == sum of Cr legs in integer pence
- exactly two legs: Dr on a Liability class, Cr on Asset (Bank) class
- the AP account is the same code the original invoice credited (`2000`)
- payment amount never exceeds the open AP balance by more than the stated
  credit-balance tolerance (see Edge cases)

## Edge cases

- Partial payment: Dr `2000` for the partial amount, Cr bank for the same —
  the invoice stays partially open in the sub-ledger
- Overpayment / credit balance: the excess leaves a debit balance on `2000`
  to offset against future invoices, or is refunded
- Early-settlement discount: Dr `2000` full invoice, Cr bank for the
  discounted amount, Cr `2220` Input VAT for the VAT element of the
  discount (prompt-payment-discount rule — the input VAT claimed is
  reduced in line with the discount), and Cr the remaining difference to
  Other Operating Income (`4050`)
- Bounced / returned payment: exact reverse (Dr bank / Cr `2000`)
- Payment on account (no invoice): Dr `2000` / Cr bank, then apply against
  the invoice when it arrives
- Foreign-currency payment: out of scope for v0 (asset `GBP/2` only)

## Framework variants

- uk-gaap (FRS 102 §11 / FRS 105): settlement of a financial liability —
  derecognition when the obligation is discharged; posting identical.
- IFRS (IFRS 9 §3.2): same derecognition outcome for a settled trade
  payable; no measurement gain/loss arises on a GBP-to-GBP settlement.
- us-gaap: TODO — posting mechanics identical (ASC 405), but US entities
  track sales/use tax rather than VAT; check the liability account mapped
  in the chart of accounts.
