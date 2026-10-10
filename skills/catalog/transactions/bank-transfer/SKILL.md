---
id: bank-transfer
name: Transfer between own bank accounts
category: transaction
frameworks: [uk-gaap, ifrs]
version: 0.1.0
status: v0
---

# Transfer between own bank accounts

Moves money between two bank accounts owned by the same entity: Dr the
destination bank, Cr the source bank. This is a balance-sheet-only entry —
no P&L impact, no VAT, no third party.

## When to use

- "Transfer £X from current to deposit", "sweep to savings", "move money
  between accounts"
- Moving working capital between the entity's own accounts (e.g. `1000`
  Bank Current Account ↔ `1010` Bank Deposit Account)
- Do NOT use for: payments to/from third parties (supplier-payment /
  customer-receipt), transfers between different legal entities (that is an
  intercompany loan — out of scope v0), FX transfers (out of scope v0).

## Facts

| field | type | required | notes |
|-------|------|----------|-------|
| source_account_code | string | yes | account the money leaves, e.g. `1000` Bank Current Account |
| destination_account_code | string | yes | account the money arrives in, e.g. `1010` Bank Deposit Account |
| amount_pence | integer | yes | integer pence, must be > 0 |
| transfer_date | date | yes | posting date (the date the funds move) |
| reference | string | no | bank reference / sweep id |

## DR/CR construction

| leg | account class | amount | notes |
|-----|---------------|--------|-------|
| Dr | Asset (Bank) | amount_pence | destination bank account — balance increases |
| Cr | Asset (Bank) | amount_pence | source bank account — balance decreases |

Both legs are Asset (Bank) class. If source and destination are the same
code the entry is a no-op and must be rejected.

## VAT treatment

None. Transfers between the entity's own GBP accounts are outside the scope
of VAT — no input or output VAT leg exists. (A transfer does not create a
supply.)

## Formance interaction

Balanced 2-leg entry [Dr 1010 50000, Cr 1000 50000] produces exactly one
posting — source is the credit (source) account, destination the debit
(destination) account:

```json
[
  {"source": "1000", "destination": "1010", "amount": "500.00", "asset": "GBP/2"}
]
```

Carry `reference` (JE ref) and `metadata.transaction_id` unchanged (F1 §6).

## Worked example

Move £500.00 from the current account to the deposit account:

```dr-cr
Dr  1010 Bank Deposit Account  50000
Cr  1000 Bank Current Account  50000
```

Debits 50000 == credits 50000. One Formance posting:
`{source: 1000 → destination: 1010, "500.00", asset: GBP/2}`.

## Verification checks

- Sum of Dr legs == sum of Cr legs in integer pence
- exactly two legs, both Asset (Bank) class, with different codes
- amount > 0; zero or negative transfers are rejected
- no Expense, Revenue, or VAT leg is present

## Edge cases

- Same-account transfer (source == destination): reject — no-op
- Failed transfer: if the bank reverses the transfer, post the exact
  reverse (Dr source / Cr destination)
- Clearing delay: post on the value date the bank reports, so the bank
  balance reconciles with the feed
- Overdraft source account: still allowed — the source bank goes negative
  (an overdraft is a Liability in presentation, but the bank account keeps
  its Asset class; the engine does not reclassify)
- FX transfer between different-currency accounts: out of scope for v0 —
  the engine posts a single `GBP/2` asset

## Framework variants

- uk-gaap (FRS 102) and IFRS (IAS 1): the posting is framework-neutral.
  Presentation-only difference: where an entity has a legally enforceable
  right of offset, a bank overdraft may be netted against a positive
  balance with the same bank under IAS 1.71 / FRS 102 equivalent — this
  affects balance-sheet presentation, not the journal.
- us-gaap: TODO — posting identical; US practice around offsetting (ASC
  210-20) is stricter than IAS 1.71 in some cases.
