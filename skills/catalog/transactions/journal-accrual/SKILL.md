---
id: journal-accrual
name: Accrued expense / provision journal
category: transaction
frameworks: [uk-gaap, ifrs, us-gaap]
version: 0.1.0
status: v0
---

# Accrued expense journal (and provisions)

Recognises an expense in the period it is incurred when no supplier invoice
has arrived yet: Dr Expense / Cr Accruals (a liability). The accrual is
reversed (or replaced by the real invoice) when the invoice lands. A
provision uses the same shape where the amount is estimated and uncertain.

## When to use

- "Accrue December rent", "accrue utility bill", "period-end accrual"
- An expense belongs to this period but the invoice has not been received
  (e.g. rent, utilities, accountancy fees, bonuses)
- A provision is needed for an uncertain obligation (e.g. dilapidations,
  warranty — estimate, best judgement)
- Do NOT use for: invoices already received (supplier-invoice), expenses
  paid in advance (prepayment skill), depreciation (depreciation skill).

## Facts

| field | type | required | notes |
|-------|------|----------|-------|
| expense_account_code | string | yes | e.g. `6000` Rent & Rates, `6010` Utilities - Gas & Electric |
| amount_pence | integer | yes | best estimate of the expense, integer pence |
| period_end_date | date | yes | the period the expense belongs to |
| description | string | yes | what is being accrued |
| reversal_date | date | no | when to auto-reverse (next period start) |
| is_provision | boolean | no | true when the amount is an uncertain estimate |

## DR/CR construction

Accrued expense:

| leg | account class | amount | notes |
|-----|---------------|--------|-------|
| Dr | Expense (Expense) | amount_pence | the expense account the cost belongs to |
| Cr | Liability (CurrentLiability) | amount_pence | `2010` Accruals — owed but not yet invoiced |

Provision (uncertain amount):

| leg | account class | amount | notes |
|-----|---------------|--------|-------|
| Dr | Expense (Expense) | best estimate | charge to the relevant expense account |
| Cr | Liability (CurrentLiability) | best estimate | `2010` Accruals — engine has no separate provision account |

Reversal at the start of the next period (or when the invoice arrives):

| leg | account class | amount | notes |
|-----|---------------|--------|-------|
| Dr | Liability (CurrentLiability) | amount_pence | `2010` Accruals — clear the liability |
| Cr | Expense (Expense) | amount_pence | offset the next period's charge |

## VAT treatment

- The accrual is posted NET of VAT. Input VAT is recognised only when the
  supplier's VAT invoice is received (standard scheme): on arrival, reverse
  the accrual and post the real supplier-invoice entry with its `2220`
  Input VAT leg.
- Cash scheme: input VAT is claimable on payment; the accrual remains net
  until the invoice, then the VAT leg follows payment timing.
- An accrual itself never carries a VAT leg.

## Formance interaction

Balanced 2-leg entry [Dr 6000 120000, Cr 2010 120000] produces exactly one
posting — source is the credit (accruals) account, destination the debit
(expense):

```json
[
  {"source": "2010", "destination": "6000", "amount": "1200.00", "asset": "GBP/2"}
]
```

Reversal [Dr 2010 120000, Cr 6000 120000] mirrors it:

```json
[
  {"source": "6000", "destination": "2010", "amount": "1200.00", "asset": "GBP/2"}
]
```

Carry `reference` (JE ref) and `metadata.transaction_id` unchanged (F1 §6).

## Worked example

December rent of £1,200.00 has not been invoiced at 31 December:

```dr-cr
Dr  6000 Rent & Rates  120000
Cr  2010 Accruals       120000
```

Debits 120000 == credits 120000. One Formance posting:
`{source: 2010 → destination: 6000, "1200.00", asset: GBP/2}`.

Reversal on 1 January:

```dr-cr
Dr  2010 Accruals     120000
Cr  6000 Rent & Rates  120000
```

Debits 120000 == credits 120000. One Formance posting:
`{source: 6000 → destination: 2010, "1200.00", asset: GBP/2}`.
When the January invoice arrives it is posted normally via the
supplier-invoice skill.

## Verification checks

- Sum of Dr legs == sum of Cr legs in integer pence
- exactly two legs: Dr Expense class, Cr Liability (CurrentLiability) class
- the reversal is the exact mirror with sides swapped
- no VAT leg on the accrual itself; VAT enters only via the invoice entry
- at period end, no accrual for the same expense+period exists twice

## Edge cases

- Estimate true-up: when the invoice differs from the accrual, reverse the
  accrual and post the actual — the difference lands in the invoice period
- Accrual vs prepayment: an invoice covering both periods may need one
  accrual and one prepayment (see prepayment skill)
- Provision measurement: use the best estimate of the outflow; where the
  effect of the time value of money is material the liability is discounted
  (IAS 37) — out of scope for v0 engine, note in Framework variants
- Year-end cut-off: post accruals in the closing period, reverse on the
  first day of the next period
- Immaterial amounts: FRS 105 micro-entities may omit accruals that are
  immaterial — do not force an accrual below a materiality threshold

## Framework variants

- uk-gaap (FRS 102 §21 / FRS 105): provisions recognised when a present
  obligation from a past event exists and a reliable estimate can be made;
  discounting is rare for short-term accruals.
- IFRS (IAS 37): provisions are discounted when material; recognition and
  measurement thresholds can differ from FRS 102 §21 (e.g. constructive
  obligations, restructuring provisions). The accrual shape itself is
  framework-neutral.
- us-gaap (ASC 450 / ASC 720): two different accrual families. Ordinary
  expense accruals (utilities, fees, wages owed) follow ASC 720 — expensed
  as incurred once the amount is estimable. Loss contingencies follow
  ASC 450-20: accrue only when the loss is PROBABLE (likely to occur) AND
  reasonably estimable; disclose when reasonably possible; no accrual for
  remote or merely possible losses. "Probable" is a higher bar than
  IAS 37's "more likely than not", so some provisions a UK/IFRS reporter
  recognises are disclosure-only in the US — and the US model has no
  constructive-obligation concept. The DR/CR shape is identical; the
  recognition trigger is not.
